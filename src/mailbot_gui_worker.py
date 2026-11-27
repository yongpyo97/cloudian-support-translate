import imaplib
import logging
import queue
import sqlite3
import subprocess
import threading
import time
from contextlib import closing

import mailbot_reply


ACCOUNT_FIELDS = ("imap_user", "imap_host", "imap_port", "mailbox")


NETWORK_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, imaplib.IMAP4.error, sqlite3.Error)


def account_key(account):
    return tuple(account[field] for field in ACCOUNT_FIELDS)


def inherit_delivery_history(engine, database, previous, current):
    old_chat = previous.get("telegram_chat_id")
    new_chat = current["telegram_chat_id"]
    if not old_chat or str(old_chat) == str(new_chat):
        return
    with database:
        for account in engine.imap_accounts(current):
            old = engine.mailbox_key(account | {"telegram_chat_id": old_chat})
            new = engine.mailbox_key(account | {"telegram_chat_id": new_chat})
            database.execute(
                """INSERT INTO cursor SELECT ?, validity, uid FROM cursor WHERE mailbox = ?
                ON CONFLICT(mailbox) DO UPDATE SET uid = MAX(cursor.uid, excluded.uid)
                WHERE cursor.validity = excluded.validity""",
                (new, old),
            )
            database.execute(
                "INSERT OR IGNORE INTO handled SELECT ?, validity, uid FROM handled WHERE mailbox = ?",
                (new, old),
            )
            database.execute(
                "INSERT OR IGNORE INTO acknowledgement SELECT ?, case_id FROM acknowledgement WHERE mailbox = ?",
                (new, old),
            )


class MailWorker:
    def __init__(self, engine, config, events):
        self.engine = engine
        self.config = config
        self.events = events
        self.commands = queue.Queue()
        self.interrupt = threading.Event()
        self.telegram_settings = queue.Queue()
        self.telegram_stop = threading.Event()
        self.reply_settings = queue.Queue()
        self.reply_work = threading.Event()
        self.telegram_thread = threading.Thread(target=self.run_telegram, name="telegram-worker")
        self.reply_thread = threading.Thread(target=self.run_telegram_replies, name="telegram-reply-worker")
        self.thread = threading.Thread(target=self.run, name="mail-worker")
        self.telegram_thread.start()
        self.reply_thread.start()
        self.thread.start()

    def request(self, command, value=None):
        if command in {"apply", "pause", "exit"}:
            self.interrupt.set()
        if command == "exit":
            self.telegram_stop.set()
            self.set_telegram_settings(None)
        self.commands.put((command, value))

    def set_telegram_settings(self, current):
        self.telegram_settings.put(current)
        self.reply_settings.put(current)
        self.reply_work.set()

    def run_telegram_replies(self):
        current = None
        with closing(sqlite3.connect(self.config.with_suffix(".sqlite3"))) as database:
            while not self.telegram_stop.is_set():
                self.reply_work.wait(1)
                self.reply_work.clear()
                try:
                    while True:
                        current = self.reply_settings.get_nowait()
                except queue.Empty:
                    pass
                if current is None or self.telegram_stop.is_set():
                    continue
                settings, accounts, token, codex = current
                try:
                    mailbot_reply.notify_interrupted_sends(self.engine, database, settings, token)
                    mailbot_reply.process_reply_jobs(
                        self.engine, database, settings, accounts, token, codex,
                        interrupted=lambda: self.telegram_stop.is_set() or not self.reply_settings.empty(),
                    )
                    if not self.telegram_stop.is_set() and self.reply_settings.empty():
                        mailbot_reply.retry_sent_copies(self.engine, database, settings, accounts, token)
                except NETWORK_ERRORS as error:
                    logging.warning("Telegram 회신 작업 실패 (%s)", type(error).__name__)

    def run_telegram(self):
        current = None
        with closing(sqlite3.connect(self.config.with_suffix(".sqlite3"))) as database:
            while not self.telegram_stop.is_set():
                try:
                    if current is None:
                        current = self.telegram_settings.get()
                    while True:
                        current = self.telegram_settings.get_nowait()
                except queue.Empty:
                    pass
                if current is None or self.telegram_stop.is_set():
                    continue
                try:
                    mailbot_reply.check_updates(
                        self.engine, database, *current,
                        interrupted=lambda: self.telegram_stop.is_set() or not self.telegram_settings.empty(),
                        wake_replies=self.reply_work.set,
                    )
                except NETWORK_ERRORS as error:
                    logging.warning("Telegram 업데이트 확인 실패 (%s)", type(error).__name__)
                    try:
                        current = self.telegram_settings.get(timeout=3)
                    except queue.Empty:
                        pass

    def run(self):
        try:
            database = self.engine.open_state(self.config)
        except sqlite3.Error:
            logging.error("메일 처리 기록을 열 수 없습니다.")
            self.events.put(("notice", "메일 처리 기록을 열 수 없습니다."))
            self.events.put(("worker-status", "처리 기록 오류"))
            return
        try:
            with database:
                database.execute("UPDATE reply_drafts SET status='interrupted' WHERE status='sending'")
            self.loop(database)
        finally:
            database.close()

    def loop(self, database):
        settings = None
        passwords = {}
        token = codex = None
        paused = False
        first_checks = set()
        previous = None
        mail_due = None
        telegram_settings = None
        while True:
            delay = max(0, mail_due - time.monotonic()) if mail_due is not None else None
            try:
                command, value = self.commands.get(timeout=delay)
            except queue.Empty:
                command = "poll"
                value = None
            if command == "exit":
                return
            if command == "apply":
                settings, passwords, token, codex = value
                history = previous or {"telegram_chat_id": settings.get("previous_telegram_chat_id")}
                try:
                    inherit_delivery_history(self.engine, database, history, settings)
                except sqlite3.Error:
                    logging.error("채팅 변경 시 기존 메일 처리 기록을 이어받지 못했습니다.")
                    self.events.put(("notice", "기존 메일 처리 기록을 이어받지 못했습니다. 로그를 확인하세요."))
                    paused = True
                previous_keys = {account_key(a) for a in self.engine.imap_accounts(previous)} if previous else set()
                active_keys = {account_key(a) for a in self.engine.imap_accounts(settings)}
                first_checks = (first_checks & active_keys) | (active_keys - previous_keys)
                previous = settings
                reply_accounts = {
                    self.engine.mailbox_key(settings | account): (account, passwords[account_key(account)])
                    for account in self.engine.imap_accounts(settings) if account.get("smtp_host")
                }
                self.interrupt.clear()
                mail_due = None if paused else time.monotonic()
                telegram_settings = (settings, reply_accounts, token, codex)
                self.set_telegram_settings(None if paused else telegram_settings)
                self.events.put(("worker-status", "일시정지" if paused else "메일 확인 대기"))
                continue
            if command == "pause":
                paused = True
                mail_due = None
                self.set_telegram_settings(None)
                self.events.put(("worker-status", "일시정지"))
                continue
            if command == "resume":
                paused = False
                self.interrupt.clear()
                mail_due = time.monotonic() if settings else None
                self.set_telegram_settings(telegram_settings)
                self.events.put(("worker-status", "메일 확인 대기" if settings else "설정 필요"))
                continue
            if not settings or (paused and command == "poll"):
                continue
            self.events.put(("worker-status", "메일 확인 중" if command == "poll" else "메일 재번역 중"))
            accounts = self.engine.imap_accounts(settings)
            failed = False
            try:
                if command == "retranslate":
                    account = next((account for account in accounts if account_key(account) == value[0]), None)
                    if account is None:
                        raise RuntimeError("선택한 메일 계정을 찾을 수 없습니다.")
                    self.engine.poll(settings | account, passwords[value[0]], token, codex, database,
                                     retranslate=value[1], retranslate_validity=value[2], interactive=True)
                else:
                    for account in accounts:
                        if self.interrupt.is_set():
                            break
                        account_settings = settings | account
                        key = self.engine.mailbox_key(account_settings)
                        jobs = database.execute(
                            "SELECT uid, validity FROM manual_retranslation WHERE mailbox = ? ORDER BY rowid", (key,),
                        ).fetchall()
                        for uid, validity in jobs:
                            if self.interrupt.is_set():
                                break
                            try:
                                self.engine.poll(account_settings, passwords[account_key(account)], token, codex, database,
                                                 retranslate=uid, retranslate_validity=validity, interactive=True)
                            except NETWORK_ERRORS as error:
                                logging.warning("저장된 메일 재번역 확인 실패 (%s)", type(error).__name__)
                    self.engine.poll_accounts([
                        (settings | account, passwords[account_key(account)], account_key(account) in first_checks)
                        for account in accounts
                    ], token, codex, database, interrupted=self.interrupt.is_set, interactive=True)
                    if not self.interrupt.is_set():
                        first_checks.clear()
            except NETWORK_ERRORS as error:
                failed = True
                detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                logging.error("메일 확인 실패 (%s)", detail)
                self.events.put(("notice", f"메일 확인 실패 · {detail}"))
            if command == "retranslate" and not failed:
                self.events.put(("notice", f"메일 UID {value[1]} 재번역·전송 완료"))
            mail_due = None if paused else time.monotonic() + self.engine.runtime_options(settings)["poll_seconds"]
            self.events.put(("worker-status", "일시정지" if paused else ("확인 오류 · 로그 참조" if failed else "메일 확인 대기")))

import hashlib
import json
import logging
import re
import secrets
import smtplib
import ssl
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from subprocess import TimeoutExpired


REPLY_MODEL = "gpt-5.6-sol"


def scope_for(token):
    return hashlib.sha256(token.encode()).hexdigest()


def initialize(database):
    database.executescript("""
        CREATE TABLE IF NOT EXISTS reply_targets (
            scope TEXT, chat INTEGER, message INTEGER, mailbox TEXT, metadata TEXT,
            PRIMARY KEY (scope, chat, message)
        );
        CREATE TABLE IF NOT EXISTS mail_previews (
            scope TEXT, chat INTEGER, message INTEGER, heading TEXT, body TEXT, filename TEXT, file_message INTEGER,
            PRIMARY KEY (scope, chat, message)
        );
        CREATE TABLE IF NOT EXISTS reply_drafts (
            id TEXT PRIMARY KEY, scope TEXT, chat INTEGER, target INTEGER,
            body TEXT, status TEXT, preview INTEGER, message_id TEXT, author INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS reply_preview ON reply_drafts(scope, chat, preview);
        CREATE TABLE IF NOT EXISTS reply_offsets (scope TEXT PRIMARY KEY, next_update INTEGER);
        CREATE TABLE IF NOT EXISTS reply_edit_sessions (
            scope TEXT, chat INTEGER, author INTEGER, draft TEXT, prompt INTEGER, expires REAL,
            PRIMARY KEY (scope, chat, author)
        );
        CREATE TABLE IF NOT EXISTS reply_group_members (
            scope TEXT, chat INTEGER, author INTEGER, name TEXT,
            PRIMARY KEY (scope, chat, author)
        );
        CREATE TABLE IF NOT EXISTS telegram_known_chats (
            scope TEXT, chat INTEGER, name TEXT, kind TEXT, welcomed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scope, chat)
        );
    """)
    if "author" not in {row[1] for row in database.execute("PRAGMA table_info(reply_drafts)")}:
        with database:
            database.execute("ALTER TABLE reply_drafts ADD COLUMN author INTEGER")


def parse_reply_users(value):
    if isinstance(value, str):
        parts = re.split(r"[\s,]+", value.strip()) if value.strip() else []
    else:
        parts = value or []
    if not isinstance(parts, (list, tuple)):
        raise ValueError("허용 사용자 ID는 숫자를 쉼표로 구분해 입력하세요.")
    ids = []
    for item in parts:
        if not re.fullmatch(r"[1-9][0-9]{0,15}", str(item)) or int(item) >= 2**52:
            raise ValueError("허용 사용자 ID는 숫자를 쉼표로 구분해 입력하세요.")
        ids.append(int(item))
    return sorted(set(ids))


def reply_format_options(settings):
    greeting = settings.get("reply_greeting", "")
    signature = settings.get("reply_signature", "")
    if not isinstance(greeting, str) or len(greeting) > 120 or not all(char.isprintable() for char in greeting):
        raise ValueError("인사말은 한 줄, 120자 이내로 입력하세요.")
    if not isinstance(signature, str):
        raise ValueError("회신 서명은 텍스트로 입력하세요.")
    signature = signature.replace("\r\n", "\n").replace("\r", "\n")
    if len(signature) > 500 or any(not char.isprintable() and char not in "\n\t" for char in signature):
        raise ValueError("회신 서명은 500자 이내로 입력하세요.")
    return {"reply_greeting": greeting.strip(), "reply_signature": signature.strip()}


def compose_reply(body, *, reply_greeting, reply_signature):
    body = body.strip()
    first_line = body.partition("\n")[0].strip()
    if reply_greeting and first_line.casefold() != reply_greeting.casefold():
        body = f"{reply_greeting}\n\n{body}"
    if reply_signature and body != reply_signature and not body.endswith(f"\n{reply_signature}"):
        body = f"{body}\n\n{reply_signature}"
    return body


def target_metadata(raw, body):
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    subject = str(message.get("Subject", "")).strip()
    if re.match(r"(?i)^\[Case Closed\]|^Your Cloudian Case\s+\d+$", subject):
        return None
    if re.search(r"(?i)\bdo\s+not\s+(?:reply|respond)\s+to\s+this\s+(?:e-?mail|message)\b|"
                 r"회신하지\s*마(?:세요|십시오)", body):
        return None
    recipient_header = message.get("Reply-To") or message.get("From")
    recipients = getattr(recipient_header, "addresses", ())
    if len(recipients) != 1:
        return None
    address = recipients[0]
    recipient = address.addr_spec
    if (not address.username or not address.domain
            or re.search(r"[\x00-\x1f\x7f]", recipient)
            or re.fullmatch(r"(?i)(?:do[._-]?not[._-]?reply|no[._-]?reply)(?:[+._-].*)?", address.username)):
        return None
    if not re.search(r"(?i)\[\s*ref:![^\]\r\n]+:ref\s*\]", subject) and not re.match(r"(?i)^re\s*:", subject):
        subject = f"Re: {subject}" if subject else "Re: (제목 없음)"
    message_id = str(message.get("Message-ID", "")).strip()
    if not re.fullmatch(r"<[^\s<>]+@[^\s<>]+>", message_id):
        message_id = ""
    references = re.findall(r"<[^\s<>]+@[^\s<>]+>", str(message.get("References", "")))[-15:]
    return {"recipient": recipient, "subject": subject, "in_reply_to": message_id, "references": references}


def remember_target(database, token, chat, mailbox, result, metadata):
    if metadata and isinstance(result, dict) and isinstance(result.get("message_id"), int):
        database.execute(
            "INSERT OR IGNORE INTO reply_targets VALUES (?, ?, ?, ?, ?)",
            (scope_for(token), int(chat), result["message_id"], mailbox, json.dumps(metadata)),
        )


def smtp_login(account, password):
    connection = smtplib.SMTP_SSL(
        account["smtp_host"], account.get("smtp_port", 465), timeout=20, context=ssl.create_default_context(),
    )
    try:
        connection.login(account["imap_user"], password)
    except (OSError, smtplib.SMTPException):
        connection.close()
        raise
    return connection


def verify_smtp(account, password):
    try:
        connection = smtp_login(account, password)
        connection.close()
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError(f"SMTP 로그인 실패 · {account['imap_user']} · {type(error).__name__}") from None


def send_smtp(account, password, metadata, body, message_id):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = account["imap_user"]
    message["To"] = metadata["recipient"]
    message["Subject"] = metadata["subject"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    if metadata["in_reply_to"]:
        message["In-Reply-To"] = metadata["in_reply_to"]
    references = list(dict.fromkeys(ref for ref in [*metadata["references"], metadata["in_reply_to"]] if ref))
    if references:
        message["References"] = " ".join(references)
    message.set_content(body)
    try:
        connection = smtp_login(account, password)
    except (OSError, smtplib.SMTPException) as error:
        return "failed", type(error).__name__
    try:
        connection.send_message(message, from_addr=account["imap_user"], to_addrs=[metadata["recipient"]])
        return "sent", ""
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
        return "failed", type(error).__name__
    except (OSError, smtplib.SMTPException) as error:
        return "uncertain", type(error).__name__
    finally:
        with suppress(OSError):
            connection.close()


def check_updates(engine, database, settings, accounts, token, codex):
    chat = int(settings["telegram_chat_id"])
    scope = scope_for(token)
    offset = database.execute("SELECT next_update FROM reply_offsets WHERE scope = ?", (scope,)).fetchone()
    updates = engine.telegram(token, "getUpdates", quiet=True, offset=offset[0] if offset else 0,
                              timeout=0, limit=20, allowed_updates=["message", "callback_query", "my_chat_member"])
    for update in updates:
        try:
            handle_update(engine, database, scope, chat, accounts, token, codex, settings, update)
        except (OSError, RuntimeError, ValueError) as error:
            logging.warning("Telegram 회신 처리 실패 (%s)", type(error).__name__)
            if not update.get("my_chat_member"):
                with suppress(OSError, RuntimeError):
                    engine.telegram(token, "sendMessage", chat_id=chat,
                                    text="회신 요청을 처리하지 못했습니다. 로그를 확인하세요.")
        with database:
            database.execute("REPLACE INTO reply_offsets VALUES (?, ?)", (scope, update["update_id"] + 1))


def remember_chat(database, scope, chat):
    name = chat.get("title") or " ".join(
        value for value in (chat.get("first_name"), chat.get("last_name")) if isinstance(value, str)
    )
    with database:
        database.execute(
            """INSERT INTO telegram_known_chats(scope,chat,name,kind) VALUES (?, ?, ?, ?)
               ON CONFLICT(scope,chat) DO UPDATE SET name=excluded.name, kind=excluded.kind""",
            (scope, chat["id"], " ".join(name.split())[:120] or str(chat["id"]), chat["type"]),
        )


def group_help(username=""):
    suffix = f"@{username}" if re.fullmatch(r"[A-Za-z0-9_]+", username or "") else ""
    return (
        "Cloudian Support Translate\n\n"
        f"/help{suffix} — 사용 방법 보기\n"
        f"/myid{suffix} — 이 채팅 ID와 내 사용자 ID 확인\n\n"
        "앱에서 Telegram → 그룹 채팅 → 그룹 찾기로 이 방을 선택하세요. "
        "회신 담당자는 /myid를 보낸 뒤 앱의 ‘회신 허용 사용자 선택’에서 등록합니다.\n"
        "허용된 팀원은 메일 알림에 답장해 초안을 만들고, 작성자가 ‘메일 보내기’를 눌러 발송합니다."
    )


def handle_membership(engine, database, scope, token, change):
    chat = change.get("chat", {})
    if chat.get("type") not in {"group", "supergroup"}:
        return
    member = change.get("new_chat_member", {})
    status = member.get("status")
    if status in {"left", "kicked"} or (status == "restricted" and not member.get("is_member")):
        with database:
            database.execute("DELETE FROM telegram_known_chats WHERE scope=? AND chat=?", (scope, chat["id"]))
        return
    if status not in {"member", "administrator", "creator", "restricted"}:
        return
    remember_chat(database, scope, chat)
    welcomed = database.execute(
        "SELECT welcomed FROM telegram_known_chats WHERE scope=? AND chat=?", (scope, chat["id"]),
    ).fetchone()[0]
    if not welcomed:
        engine.telegram(token, "sendMessage", chat_id=chat["id"],
                        text="그룹에 초대해 주셔서 감사합니다.\n\n" + group_help(member.get("user", {}).get("username", "")))
        with database:
            database.execute("UPDATE telegram_known_chats SET welcomed=1 WHERE scope=? AND chat=?", (scope, chat["id"]))


def handle_update(engine, database, scope, chat, accounts, token, codex, settings, update):
    if update.get("my_chat_member"):
        handle_membership(engine, database, scope, token, update["my_chat_member"])
        return
    callback = update.get("callback_query")
    message = callback.get("message", {}) if callback else update.get("message", {})
    sender = callback.get("from", {}) if callback else message.get("from", {})
    chat_info = message.get("chat", {})
    private = chat_info.get("type") == "private"
    group = chat_info.get("type") in {"group", "supergroup"}
    author = sender.get("id")
    if not isinstance(author, int) or sender.get("is_bot"):
        return
    if (group or private and author == chat_info.get("id")) and not callback and re.fullmatch(
        r"/help(?:@[A-Za-z0-9_]+)?", message.get("text", "").strip(), re.I
    ):
        remember_chat(database, scope, chat_info)
        engine.telegram(token, "sendMessage", chat_id=chat_info["id"], text=group_help(),
                        reply_parameters={"message_id": message["message_id"]})
        return
    if (group or private and author == chat_info.get("id")) and not callback and re.fullmatch(
        r"/myid(?:@[A-Za-z0-9_]+)?", message.get("text", "").strip(), re.I
    ):
        remember_chat(database, scope, chat_info)
        if group:
            name = " ".join(
                part for part in (sender.get("first_name"), sender.get("last_name")) if isinstance(part, str)
            )
            username = sender.get("username")
            if username:
                name = f"{name} (@{username})" if name else f"@{username}"
            with database:
                database.execute("REPLACE INTO reply_group_members VALUES (?, ?, ?, ?)",
                                 (scope, chat_info["id"], author, " ".join(name.split())[:100]))
        engine.telegram(token, "sendMessage", chat_id=chat_info["id"],
                        text=f"내 Telegram 사용자 ID: {author}\nChat ID: {chat_info['id']}",
                        reply_parameters={"message_id": message["message_id"]})
        return
    if chat_info.get("id") != chat:
        return
    if (callback and (group or private and author == chat)
            and callback.get("data") in {"mailview:open", "mailview:close", "mailview:file"}):
        engine.handle_mail_preview(database, scope, token, chat, callback)
        return
    if not ((private and author == chat) or (group and author in settings.get("reply_allowed_users", []))):
        return
    if callback:
        handle_callback(engine, database, scope, chat, accounts, token, callback, author, private)
        return

    parent = message.get("reply_to_message", {}).get("message_id")
    text = message.get("text", "").strip()
    if not text or text.startswith("/"):
        return
    revised = database.execute(
        """SELECT id, target, preview FROM reply_drafts WHERE scope=? AND chat=? AND preview=? AND status='editing'
           AND (author=? OR (? AND author IS NULL))""",
        (scope, chat, parent, author, private),
    ).fetchone()
    if not revised:
        revised = database.execute(
            """SELECT d.id, d.target, d.preview FROM reply_edit_sessions e JOIN reply_drafts d ON d.id=e.draft
               WHERE e.scope=? AND e.chat=? AND e.author=? AND e.expires>=? AND d.status='editing'
                 AND ((? AND ? IS NULL) OR e.prompt=?)""",
            (scope, chat, author, time.time(), private, parent, parent),
        ).fetchone()
    if not parent and not revised:
        return
    target = revised[1] if revised else parent
    row = database.execute(
        "SELECT mailbox, metadata FROM reply_targets WHERE scope=? AND chat=? AND message=?",
        (scope, chat, target),
    ).fetchone()
    if not row and not revised:
        parent_message = message.get("reply_to_message", {})
        legacy_text = parent_message.get("text") or parent_message.get("caption", "")
        if re.match(r"^새 이메일 도착(?:[ \t·]|\n)", legacy_text):
            bot = engine.telegram(token, "getMe", quiet=True)
            if parent_message.get("from", {}).get("id") != bot.get("id"):
                return
            row = resolve_legacy_target(engine, accounts, legacy_text)
            if row:
                with database:
                    remember_target(database, token, chat, row[0], {"message_id": target}, json.loads(row[1]))
            else:
                engine.telegram(token, "sendMessage", chat_id=chat,
                                text="회신 가능한 원본 메일을 하나로 확인하지 못했습니다. 번역봇에서 UID 재번역으로 새 알림을 만든 뒤 답장하세요.")
    if not row:
        return
    if row[0] not in accounts:
        engine.telegram(token, "sendMessage", chat_id=chat, text="이 메일 계정의 SMTP 회신이 꺼져 있습니다.")
        return
    if len(text) > 2300:
        engine.telegram(token, "sendMessage", chat_id=chat, text="회신 초안은 2,300자 이하로 입력하세요.")
        return
    metadata = json.loads(row[1])
    account, _ = accounts[row[0]]
    progress_text = "수정한 회신으로 새 초안을 만드는 중입니다…" if revised else "회신 초안을 만드는 중입니다…"
    progress_id = engine.post_progress(token, chat, progress_text, reply_to=message.get("message_id"))
    try:
        body, preview_body = prepare_draft_text(engine, codex, settings, metadata["subject"], text)
    except (OSError, RuntimeError, ValueError, TimeoutExpired) as error:
        logging.warning("회신 초안 작성 실패 (%s)", type(error).__name__)
        notice = "회신 초안 작성 시간이 초과됐습니다." if isinstance(error, TimeoutExpired) else "회신 초안을 만들지 못했습니다."
        if not engine.update_progress(token, chat, progress_id, notice):
            engine.telegram(token, "sendMessage", chat_id=chat, text=notice)
        return
    heading = (f"메일 회신 초안\n보내는 사람: {account['imap_user']}\n받는 사람: {metadata['recipient']}\n"
               f"제목: {metadata['subject']}")
    preview = f"{heading}\n\n{preview_body}"
    draft = secrets.token_hex(12)
    with database:
        database.execute("INSERT INTO reply_drafts VALUES (?, ?, ?, ?, ?, 'ready', NULL, ?, ?)",
                         (draft, scope, chat, target, body, make_msgid(domain=account["imap_user"].split("@")[-1]), author))
    markup = reply_keyboard(draft)
    try:
        if len(preview.encode("utf-16-le")) // 2 <= 4096:
            result = engine.publish_progress(token, chat, preview, progress_id=progress_id, reply_markup=markup)
        else:
            result = engine.publish_progress(token, chat, heading[:440] + "\n첨부에서 발송할 본문을 확인하세요.",
                                             progress_id=progress_id, reply_markup=markup,
                                             document=("reply-preview.txt", preview.encode("utf-8-sig")))
    except (OSError, RuntimeError):
        with database:
            database.execute("DELETE FROM reply_drafts WHERE id=? AND preview IS NULL", (draft,))
        if engine.update_progress(token, chat, progress_id, "회신 초안을 텔레그램에 표시하지 못했습니다."):
            return
        raise
    with database:
        database.execute("UPDATE reply_drafts SET preview=? WHERE id=?", (result["message_id"], draft))
        if revised:
            database.execute("UPDATE reply_drafts SET status='replaced', body='' WHERE id=?", (revised[0],))
            database.execute("DELETE FROM reply_edit_sessions WHERE scope=? AND chat=? AND author=? AND draft=?",
                             (scope, chat, author, revised[0]))
    if revised and revised[2]:
        with suppress(OSError, RuntimeError):
            engine.telegram(token, "editMessageReplyMarkup", chat_id=chat, message_id=revised[2],
                            quiet=True, request_timeout=5, reply_markup={"inline_keyboard": []})


def reply_keyboard(draft, *, editing=False):
    actions = (("수정 취소", "editback"), ("초안 취소", "cancel")) if editing else (
        ("메일 보내기", "send"), ("수정", "edit"), ("취소", "cancel"),
    )
    return {"inline_keyboard": [[{"text": label, "callback_data": f"mr:{action}:{draft}"}
                                for label, action in actions]]}


def prepare_draft_text(engine, codex, settings, subject, text):
    reply_format = reply_format_options(settings)
    if not re.search(r"[가-힣]", text):
        body = compose_reply(text, **reply_format)
        return body, body
    started = time.monotonic()
    model = settings.get("reply_codex_model", REPLY_MODEL)
    options = {
        "custom_prompt": settings.get("reply_translation_prompt", engine.DEFAULT_REPLY_PROMPT),
        "app_adds_greeting": bool(reply_format["reply_greeting"]),
        "app_adds_signature": bool(reply_format["reply_signature"]),
    }
    try:
        korean, body, back_translation = engine.prepare_reply(codex, subject, text, model=model, **options)
    except RuntimeError as error:
        fallback = settings.get("codex_model")
        if fallback == model or not str(error).startswith("Codex exec 종료 코드 "):
            raise
        logging.warning("회신용 번역 모델 사용 실패 · 수신 번역 모델로 재시도")
        model = fallback
        korean, body, back_translation = engine.prepare_reply(codex, subject, text, model=model, **options)
    logging.info("회신 번역 완료 · %s · %.2f초", model or "Codex 기본 모델", time.monotonic() - started)
    body = compose_reply(body, **reply_format)
    back_label = "영문 본문의 한국어 역번역" if any(reply_format.values()) else "영문의 한국어 역번역"
    preview = (f"[다듬은 한국어]\n{korean}\n\n[발송할 영어]\n{body}\n\n"
               f"[{back_label}]\n{back_translation}")
    return body, preview


def resolve_legacy_target(engine, accounts, alert):
    heading = alert.split("\n\n", 1)[0]
    title = re.search(r"(?m)^제목:[ \t]*(.+)$", heading)
    arrived = re.search(r"(?m)^수신:[ \t]*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) KST$", heading)
    if not title or not arrived:
        return None
    timestamp = datetime.fromisoformat(arrived[1]).replace(tzinfo=timezone(timedelta(hours=9)))
    return engine.find_legacy_reply(accounts, title[1], timestamp)


def handle_callback(engine, database, scope, chat, accounts, token, callback, author, private):
    match = re.fullmatch(r"mr:(send|edit|editback|cancel):([0-9a-f]{24})", callback.get("data", ""))
    if not match:
        return
    action, draft = match.groups()
    row = database.execute(
        """SELECT d.status, d.body, d.message_id, t.mailbox, t.metadata FROM reply_drafts d
           JOIN reply_targets t ON t.scope=d.scope AND t.chat=d.chat AND t.message=d.target
           WHERE d.id=? AND d.scope=? AND d.chat=? AND d.preview=?
             AND (d.author=? OR (? AND d.author IS NULL))""",
        (draft, scope, chat, callback["message"].get("message_id"), author, private),
    ).fetchone()
    def answer(text):
        engine.telegram(token, "answerCallbackQuery", callback_query_id=callback["id"], text=text)
    if not row or row[3] not in accounts:
        answer("이 회신 초안을 사용할 수 없습니다.")
        return
    status, body, message_id, mailbox, metadata = row
    ready_action = status == "ready" and action != "editback"
    edit_action = status == "editing" and action in {"edit", "editback", "cancel"}
    if not (ready_action or edit_action):
        answer("이미 처리했거나 수정 중입니다. 발송 상태가 불확실하면 메일함에서 확인하세요.")
        return
    account, password = accounts[mailbox]
    metadata = json.loads(metadata)
    next_status = {"send": "sending", "edit": "editing", "editback": "ready", "cancel": "canceled"}[action]
    with database:
        # Keep only the in-memory copy once the user cancels or claims an SMTP send.
        retained_body = body if action in {"edit", "editback"} else ""
        changed = database.execute(
            "UPDATE reply_drafts SET status=?, body=? WHERE id=? AND status=?",
            (next_status, retained_body, draft, status),
        ).rowcount
        if changed and action == "edit":
            database.execute("REPLACE INTO reply_edit_sessions VALUES (?, ?, ?, ?, NULL, ?)",
                             (scope, chat, author, draft, time.time() + 600))
        elif changed and action in {"editback", "cancel"}:
            database.execute("DELETE FROM reply_edit_sessions WHERE scope=? AND chat=? AND author=? AND draft=?",
                             (scope, chat, author, draft))
    if not changed:
        answer("이미 처리된 요청입니다.")
        return
    if action == "edit":
        with suppress(OSError, RuntimeError):
            answer("수정할 내용을 입력하세요.")
        instruction = "수정할 회신 본문 전체를 이 메시지에 답장하면 새 초안을 만듭니다."
        if private:
            prompt = engine.telegram(token, "sendMessage", chat_id=chat,
                                     text=instruction,
                                     reply_markup={"force_reply": True, "input_field_placeholder": "수정할 내용 입력"})
        else:
            prompt = engine.telegram(token, "sendMessage", chat_id=chat,
                                     text=instruction,
                                     reply_parameters={"message_id": callback["message"]["message_id"]})
        with database:
            database.execute("UPDATE reply_edit_sessions SET prompt=? WHERE scope=? AND chat=? AND author=? AND draft=?",
                             (prompt["message_id"], scope, chat, author, draft))
        with suppress(OSError, RuntimeError):
            engine.telegram(token, "editMessageReplyMarkup", chat_id=chat, message_id=callback["message"]["message_id"],
                            quiet=True, request_timeout=5,
                            reply_markup=reply_keyboard(draft, editing=True))
        return
    if action == "editback":
        with suppress(OSError, RuntimeError):
            answer("수정을 취소했습니다. 기존 초안을 사용할 수 있습니다.")
        engine.telegram(token, "editMessageReplyMarkup", chat_id=chat, message_id=callback["message"]["message_id"],
                        reply_markup=reply_keyboard(draft))
        return
    progress_id = callback["message"]["message_id"]
    is_attachment = "document" in callback["message"]
    status_text = "회신 초안을 취소했습니다." if action == "cancel" else "회신 메일을 보내는 중입니다…"
    with suppress(OSError, RuntimeError):
        answer(status_text)
    if not engine.update_progress(token, chat, progress_id, status_text, caption=is_attachment, clear_buttons=True):
        with suppress(OSError, RuntimeError):
            engine.telegram(token, "editMessageReplyMarkup", chat_id=chat, message_id=progress_id,
                            quiet=True, request_timeout=5, reply_markup={"inline_keyboard": []})
        progress_id = engine.post_progress(token, chat, status_text)
        is_attachment = False
    if action == "cancel":
        return
    try:
        status, reason = send_smtp(account, password, metadata, body, message_id)
    except Exception as error:
        status, reason = "uncertain", type(error).__name__
    with database:
        database.execute("UPDATE reply_drafts SET status=? WHERE id=?", (status, draft))
    logging.info("SMTP 회신 결과 · %s · %s", status, reason or "접수")
    failure_detail = {
        "SMTPRecipientsRefused": "받는 주소가 서버에서 거절됐습니다.",
        "SMTPSenderRefused": "보내는 주소가 서버에서 거절됐습니다.",
        "SMTPAuthenticationError": "SMTP 로그인에 실패했습니다.",
        "SMTPDataError": "SMTP 서버가 메일을 거절했습니다.",
        "TimeoutError": "SMTP 서버 응답 시간이 초과됐습니다.",
    }.get(reason, "SMTP 연결 또는 서버 응답을 확인하세요.")
    result = {
        "sent": "SMTP 서버가 회신 메일을 접수했습니다.",
        "failed": f"회신 메일을 보내지 못했습니다. {failure_detail}",
        "uncertain": "SMTP 접수 여부가 불명확합니다. 메일함을 확인하세요. 자동 재발송은 하지 않습니다.",
    }[status]
    if not engine.update_progress(token, chat, progress_id, result, caption=is_attachment):
        engine.telegram(token, "sendMessage", chat_id=chat, text=result)

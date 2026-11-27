import imaplib
import json
import logging
import re
import subprocess
from contextlib import ExitStack
from datetime import datetime, timezone

import mailbot_reply

from mailbot_message import (
    clean_cloudian_case_confirmation,
    clean_cloudian_metadata,
    cloudian_acknowledgement,
    cloudian_comment,
    CLOUDIAN_COPY_WAIT,
    CLOUDIAN_STAMP,
    cloudian_support_comment_key,
    compact_mail_text,
    mail_content,
    mail_source_language,
    remove_cloudian_reply_notice,
    remove_quoted_mail_headers,
)
from mailbot_imap import (
    checked,
    imap_received_at,
    mail_uid,
    mailbox_number,
    MailboxOperationError,
    open_mailbox,
)
from mailbot_notifications import (
    case_feedback_notification,
    closed_case_notification,
    format_mail_notification,
    send_mail,
)
from mailbot_state import mailbox_key, mark_handled, remember_mail_uids, uid_subject
from mailbot_telegram import post_progress, telegram, update_progress
from mailbot_codex import translate


class MailBodyUnavailable(RuntimeError):
    pass


def mailbox_candidates(connection, settings, database, *, unread, include_unread, retranslate):
    validity = mailbox_number(connection, "UIDVALIDITY")
    key = mailbox_key(settings)
    state = database.execute(
        "SELECT validity, uid FROM cursor WHERE mailbox = ?", (key,)
    ).fetchone()
    if retranslate is not None:
        start = 0
    elif state is None or state[0] != validity:
        start = mailbox_number(connection, "UIDNEXT") - 1
        with database:
            database.execute("REPLACE INTO cursor VALUES (?, ?, ?)", (key, validity, start))
            database.execute("DELETE FROM pending WHERE mailbox = ?", (key,))
            database.execute("DELETE FROM cloudian_fallback WHERE mailbox = ? AND validity != ?", (key, validity))
            database.execute("DELETE FROM mail_body_retry WHERE mailbox = ? AND validity != ?", (key, validity))
        logging.info("메일함의 초기 수신 기준을 설정했습니다.")
        if not unread and not include_unread:
            return key, validity, None, {}
    else:
        start = state[1]
    uids = set()
    if retranslate is not None:
        uids.add(retranslate)
    elif not unread:
        data = checked(connection.uid("SEARCH", None, "UID", f"{start + 1}:*"), "새 메일 검색")
        uids.update(uid for uid in map(int, (data[0] or b"").split()) if uid > start)
    if unread or include_unread:
        data = checked(connection.uid("SEARCH", None, "UNSEEN"), "안 읽은 메일 검색")
        unseen = set(map(int, (data[0] or b"").split()))
        uids.update(unseen)
    outstanding = None if retranslate is not None else database.execute(
        "SELECT uid FROM pending WHERE mailbox = ?", (key,)
    ).fetchone()
    if outstanding:
        uids.add(outstanding[0])
    deferred = set() if retranslate is not None else {uid for uid, in database.execute(
        "SELECT uid FROM cloudian_fallback WHERE mailbox = ? AND validity = ?", (key, validity),
    )}
    uids.update(deferred)
    if retranslate is None:
        uids.update(uid for uid, in database.execute(
            "SELECT uid FROM mail_body_retry WHERE mailbox = ? AND validity = ?", (key, validity),
        ))
    received = {}
    ordered = sorted(uids)
    for offset in range(0, len(ordered), 200):
        batch = ",".join(map(str, ordered[offset:offset + 200]))
        data = checked(connection.uid("FETCH", batch, "(UID INTERNALDATE)"), "수신 시각 읽기")
        for row in data:
            if not isinstance(row, bytes):
                continue
            uid_match = re.search(rb"\bUID (\d+)", row)
            if uid_match:
                timestamp = imap_received_at(row)
                if timestamp is None:
                    raise RuntimeError("IMAP 수신 시각 또는 시간대를 가져오지 못했습니다.")
                received[int(uid_match[1])] = timestamp
    with database:
        database.executemany("DELETE FROM cloudian_fallback WHERE mailbox = ? AND validity = ? AND uid = ?",
                             ((key, validity, uid) for uid in deferred.difference(received)))
    if outstanding and outstanding[0] not in received:
        # The message can be removed after its delivery payload was saved.
        received[outstanding[0]] = None
    return key, validity, outstanding[0] if outstanding else None, received


def poll(settings, password, token, codex, database, *, unread=False, include_unread=False,
         retranslate=None, retranslate_validity=None, interrupted=None, interactive=False):
    poll_accounts([(settings, password, include_unread)], token, codex, database,
                  unread=unread, retranslate=retranslate, retranslate_validity=retranslate_validity,
                  interrupted=interrupted, interactive=interactive)


def poll_accounts(accounts, token, codex, database, *, unread=False, retranslate=None,
                  retranslate_validity=None, interrupted=None, interactive=False):
    accounts = tuple(accounts)
    with ExitStack() as stack:
        mailboxes = []
        candidates = []
        account_errors = []
        failed_keys = set()
        destination_keys = tuple(mailbox_key(settings) for settings, _, _ in accounts)
        for settings, password, include_unread in accounts:
            if interrupted is not None and interrupted():
                return
            try:
                with ExitStack() as mailbox_stack:
                    connection = open_mailbox(mailbox_stack, settings, password, readonly=False)
                    key, validity, pending_uid, received = mailbox_candidates(
                        connection, settings, database, unread=unread,
                        include_unread=include_unread, retranslate=retranslate,
                    )
                    stack.enter_context(mailbox_stack.pop_all())
            except (OSError, RuntimeError, imaplib.IMAP4.error) as error:
                if retranslate is not None:
                    raise
                account_errors.append(error)
                continue
            if retranslate_validity is not None and validity != retranslate_validity:
                if interactive:
                    clear_manual_retranslation(database, key, retranslate, token, settings["telegram_chat_id"])
                raise RuntimeError("메일함이 변경되어 선택한 메일을 다시 조회해야 합니다.")
            if interactive and retranslate is not None:
                with database:
                    database.execute("INSERT OR IGNORE INTO manual_retranslation VALUES (?, ?, ?, NULL)",
                                     (key, retranslate, validity))
                    database.execute("UPDATE manual_retranslation SET validity = ? WHERE mailbox = ? AND uid = ?",
                                     (validity, key, retranslate))
            index = len(mailboxes)
            mailboxes.append((settings, connection, key, validity, received))
            candidates.extend((uid != pending_uid, timestamp, index, uid) for uid, timestamp in received.items())
        if retranslate is not None and not candidates:
            if interactive:
                for settings, _, key, _, _ in mailboxes:
                    clear_manual_retranslation(database, key, retranslate, token, settings["telegram_chat_id"])
            raise RuntimeError("선택한 메일이 현재 메일함에 없습니다.")
        earliest = datetime.min.replace(tzinfo=timezone.utc)
        candidates.sort(key=lambda item: (item[0], item[1] or earliest, item[2], item[3]))
        def deliver(index, uid, received, *, fallback_raw=None):
            settings, connection, key, validity, _ = mailboxes[index]
            try:
                fallback = process_mail(connection, settings, token, codex, database, key, validity, uid,
                                        destination_keys=destination_keys, received=received, retranslate=retranslate,
                                        fallback_raw=fallback_raw, interactive=interactive)
                if fallback is None:
                    mail_uid(connection, "메일 읽음 표시", "STORE", str(uid), "+FLAGS.SILENT", r"(\Seen)")
                return fallback
            except (MailBodyUnavailable, MailboxOperationError) as error:
                if retranslate is not None:
                    raise
                with database:
                    database.execute("INSERT OR IGNORE INTO mail_body_retry VALUES (?, ?, ?)", (key, validity, uid))
                if isinstance(error, MailboxOperationError):
                    failed_keys.add(key)
                    account_errors.append(error)
                else:
                    logging.warning("메일 본문을 받지 못했습니다. 다음 확인 때 다시 시도합니다.")
                return None

        fallbacks = []
        for _, received, index, uid in candidates:
            if interrupted is not None and interrupted():
                return
            settings, _, key, _, _ = mailboxes[index]
            if key in failed_keys:
                continue
            received_text = received.isoformat() if received else "수신 시각 없음"
            logging.info("메일 계정 %s 검사 · %s · 수신 %s",
                         index + 1, settings["imap_user"], received_text)
            fallback = deliver(index, uid, received)
            if fallback is not None:
                fallbacks.append((index, uid, received, fallback))
        # Give clean copies in this batch the first chance to supply the alert and reply target.
        fallback_cutoff = datetime.now(timezone.utc) - CLOUDIAN_COPY_WAIT
        for index, uid, received, raw in fallbacks:
            if interrupted is not None and interrupted():
                return
            if mailboxes[index][2] in failed_keys or (received and received > fallback_cutoff):
                continue
            deliver(index, uid, received, fallback_raw=raw)
        if not unread and retranslate is None:
            # A partial batch must remain discoverable even if UID and arrival order differ.
            with database:
                for _, _, key, validity, received in mailboxes:
                    if received and key not in failed_keys:
                        database.execute(
                            "UPDATE cursor SET uid = MAX(uid, ?) WHERE mailbox = ? AND validity = ?",
                            (max(received), key, validity),
                        )
        if account_errors:
            raise account_errors[0]


def clear_manual_retranslation(database, key, uid, token, chat_id):
    with database:
        row = database.execute("DELETE FROM manual_retranslation WHERE mailbox = ? AND uid = ? RETURNING payload",
                               (key, uid)).fetchone()
    if row and row[0]:
        progress_id = json.loads(row[0]).get("progress_id")
        update_progress(token, chat_id, progress_id, "원본 메일을 찾을 수 없어 재번역을 중지했습니다.")


def process_mail(connection, settings, token, codex, database, key, validity, uid, *,
                 destination_keys, received, retranslate, fallback_raw=None, interactive=False):
    chat_id = settings["telegram_chat_id"]
    if retranslate is None and database.execute(
        "SELECT 1 FROM handled WHERE mailbox = ? AND validity = ? AND uid = ?",
        (key, validity, uid),
    ).fetchone():
        logging.debug("이미 처리된 메일을 건너뜁니다.")
        with database:
            mark_handled(database, key, validity, uid)
        return
    manual = interactive and retranslate is not None
    pending = None
    if manual:
        pending = database.execute(
            "SELECT uid, payload, 0 FROM manual_retranslation WHERE mailbox = ? AND uid = ? AND payload IS NOT NULL",
            (key, uid),
        ).fetchone()
    elif retranslate is None:
        pending = database.execute("SELECT uid, parts, sent FROM pending WHERE mailbox = ?", (key,)).fetchone()
    cached = json.loads(pending[1]) if pending and pending[0] == uid else None
    preparing = isinstance(cached, dict) and cached.get("preparing")
    if cached is not None and not preparing:
        payload, sent = cached, pending[2]
        if isinstance(payload, list):
            payload = {"parts": payload, "ack": None}
    else:
        progress_id = cached.get("progress_id") if preparing else None
        raw = fallback_raw
        if raw is None:
            data = mail_uid(connection, "본문 읽기", "FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((item[1] for item in data if isinstance(item, tuple)), None)
        if raw is None:
            if not received and preparing and retranslate is None:
                update_progress(token, chat_id, progress_id, "메일이 메일함에 없어 번역을 중지했습니다.")
                with database:
                    database.execute("DELETE FROM pending WHERE mailbox = ? AND uid = ?", (key, uid))
                return
            raise MailBodyUnavailable("메일 본문을 가져오지 못했습니다. 다음 확인 때 다시 시도합니다.")
        original_subject, body = mail_content(raw)
        remember_mail_uids(database, settings, [
            (uid, validity, int(received.timestamp()) if received else 0, uid_subject(original_subject)),
        ])
        body = compact_mail_text(body)
        cloudian_copy = bool(mailbot_reply.CLOUDIAN_COPY.match(body.lstrip()))
        reply_metadata = mailbot_reply.target_metadata(raw, body) if settings.get("smtp_host") else None
        subject = re.sub(r"\s*\[\s*ref:![^\]\r\n]+:ref\s*\]\s*$", "", original_subject, flags=re.IGNORECASE)
        body = clean_cloudian_case_confirmation(subject, body)
        support_key = cloudian_support_comment_key(subject, body)
        stamp = CLOUDIAN_STAMP.match(body)
        ack = cloudian_acknowledgement(body)
        if ack:
            case_id = ack[0]
            previous_reply = ""
            body, _, _ = cloudian_comment(body)
        else:
            body, case_id, previous_reply = cloudian_comment(body)
        body = remove_cloudian_reply_notice(remove_quoted_mail_headers(clean_cloudian_metadata(body)))
        previous_reply = remove_cloudian_reply_notice(remove_quoted_mail_headers(clean_cloudian_metadata(previous_reply)))
        placeholders = ",".join("?" for _ in destination_keys)
        if retranslate is None and (case_id or support_key) and database.execute(
            f"SELECT 1 FROM acknowledgement WHERE mailbox IN ({placeholders}) AND case_id IN (?, ?)",
            (*destination_keys, case_id, support_key),
        ).fetchone():
            logging.info("이미 전송한 Cloudian 알림을 건너뜁니다.")
            update_progress(token, chat_id, progress_id, "이미 전달된 Cloudian 알림입니다.")
            with database:
                if support_key and reply_metadata and reply_metadata.get("cloudian_original"):
                    mailbot_reply.link_cloudian_copy(database, token, chat_id, support_key, key, reply_metadata)
                mark_handled(database, key, validity, uid)
            return
        if (retranslate is None and not preparing and fallback_raw is None and support_key
                and stamp and stamp[1].lower() == "cloudian"):
            with database:
                database.execute("INSERT OR IGNORE INTO cloudian_fallback VALUES (?, ?, ?)", (key, validity, uid))
            return raw
        message = closed_case_notification(subject, body, received, raw)
        if message is None:
            message = case_feedback_notification(subject, raw, received)
        if message is None:
            language = settings.get("language", "한국어")
            source_language = mail_source_language(body, subject)
            if language == "자동 (한↔영)":
                language = "영어" if source_language == "한국어" else "한국어"
            model = settings.get("codex_model")
            logging.info("메일 번역 준비 중 · %s → %s · %s",
                         source_language, language, model or "Codex 기본 모델")
            common = settings.get("global_translation_prompt", "")
            account_prompt = settings.get("translation_prompt", "")
            custom_prompt = "\n\nAccount-specific preferences (use if common preferences conflict):\n".join(
                item for item in (common, account_prompt) if item
            )
            if progress_id:
                update_progress(token, chat_id, progress_id, "메일 번역을 다시 시도하는 중입니다…")
            else:
                progress_text = "메일을 다시 번역하는 중입니다…" if retranslate is not None else "새 메일을 번역하는 중입니다…"
                progress_id = post_progress(token, chat_id, progress_text)
                if progress_id:
                    draft = json.dumps({"preparing": True, "progress_id": progress_id})
                    with database:
                        if manual:
                            database.execute("UPDATE manual_retranslation SET payload = ? WHERE mailbox = ? AND uid = ?",
                                             (draft, key, uid))
                        elif retranslate is None:
                            database.execute("REPLACE INTO pending VALUES (?, ?, ?, 0)", (key, uid, draft))
            try:
                translation = translate(
                    codex, subject, body, language, previous_reply=previous_reply, model=model,
                    custom_prompt=custom_prompt, body_only=True,
                )
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                status = ("메일 번역에 실패했습니다. 다음 확인 때 다시 시도합니다." if retranslate is None or manual
                          else "메일 재번역에 실패했습니다. 앱 로그에서 원인을 확인하세요.")
                update_progress(token, chat_id, progress_id, status)
                raise
            translation = remove_cloudian_reply_notice(remove_quoted_mail_headers(translation))
            logging.info("메일 번역 완료")
            case_status = None
            if ack:
                case_status = "엔지니어 답변 예정"
            elif support_key or (case_id or "").startswith("comment:support:"):
                case_status = "지원팀 댓글"
            elif (case_id or "").startswith("comment:customer:"):
                case_status = "고객 댓글"
            elif re.match(r"(?i)^New Case\s+\[\d+\]", subject):
                case_status = "케이스 접수"
            message = format_mail_notification(
                subject, body, translation, language, model, source_language=source_language,
                received=received, case_status=case_status,
            )
        summary = message.split("\n\n", 1)[0]
        payload = {"message": message, "ack": case_id, "summary": summary}
        if support_key:
            payload["support_key"] = support_key
            if cloudian_copy:
                payload["cloudian_copy"] = True
        if progress_id:
            payload["progress_id"] = progress_id
        if reply_metadata:
            payload["reply"] = reply_metadata
        sent = 0
        with database:
            if manual:
                database.execute("UPDATE manual_retranslation SET payload = ? WHERE mailbox = ? AND uid = ?",
                                 (json.dumps(payload), key, uid))
            elif retranslate is None:
                database.execute("REPLACE INTO pending VALUES (?, ?, ?, ?)", (key, uid, json.dumps(payload), sent))
    reply_metadata = payload.get("reply")
    if retranslate is not None:
        result = send_mail(database, token, chat_id, uid, payload["message"], summary=payload["summary"],
                           reply_enabled=bool(reply_metadata), progress_id=payload.get("progress_id"),
                           interactive=interactive)
        with database:
            mailbot_reply.remember_target(database, token, chat_id, key, result, reply_metadata)
            if manual:
                database.execute("DELETE FROM manual_retranslation WHERE mailbox = ? AND uid = ?", (key, uid))
        logging.info("메일 재번역 및 전송 완료")
        return
    if "message" in payload and sent == 0:
        result = send_mail(database, token, chat_id, uid, payload["message"],
                           payload.get("report", ""), summary=payload.get("summary", ""),
                           reply_enabled=bool(reply_metadata), progress_id=payload.get("progress_id"),
                           interactive=interactive)
        with database:
            mailbot_reply.remember_target(database, token, chat_id, key, result, reply_metadata)
            if payload.get("cloudian_copy") and isinstance(result, dict) and isinstance(result.get("message_id"), int):
                database.execute("INSERT OR IGNORE INTO cloudian_copy_alerts VALUES (?, ?, ?, ?)",
                                 (mailbot_reply.scope_for(token), chat_id, payload["support_key"], result["message_id"]))
            database.execute("UPDATE pending SET sent = 1 WHERE mailbox = ?", (key,))
    parts = payload.get("parts", [])
    for index, part in enumerate(parts[sent:], start=sent):
        telegram(
            token, "sendMessage", chat_id=settings["telegram_chat_id"],
            text=part,
            link_preview_options={"is_disabled": True},
        )
        with database:
            database.execute("UPDATE pending SET sent = ? WHERE mailbox = ?", (index + 1, key))
    with database:
        database.executemany(
            "INSERT OR IGNORE INTO acknowledgement VALUES (?, ?)",
            ((key, fingerprint) for fingerprint in (payload.get("ack"), payload.get("support_key")) if fingerprint),
        )
        mark_handled(database, key, validity, uid)
    logging.info("메일 전송 완료")

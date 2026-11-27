import json
import re
from datetime import timedelta, timezone

import mailbot_reply

from mailbot_telegram import post_progress, publish_progress, telegram, TelegramRequestError
from mailbot_message import case_feedback_link, compact_mail_text


def notification_heading(subject, received, label="새 이메일 도착"):
    title = " ".join(subject.split()) or "(제목 없음)"
    lines = [label, f"제목: {title}"]
    if received is not None:
        korea = timezone(timedelta(hours=9))
        lines.append(f"수신: {received.astimezone(korea):%Y-%m-%d %H:%M:%S} KST")
    return "\n".join(lines)


def closed_case_notification(subject, body, received, raw):
    match = re.match(r"(?i)^\[Case Closed\]\s*\[(\d+)\]\s*(.*)", " ".join(subject.split()))
    if not match:
        return None
    number, title = match.groups()
    if not re.search(rf"(?im)^\s*Case\s+{number}\s+has been closed\s*\.", body):
        return None
    heading = notification_heading(title, received, f"✅ 케이스 종료 · #{number}")
    link = case_feedback_link(raw, number)
    detail = f"지원 평가: {link}" if link else "지원 평가 링크는 별도 메일로 도착하면 안내합니다."
    return f"{heading}\n\n{detail}"


def case_feedback_notification(subject, raw, received):
    match = re.fullmatch(r"(?i)Your Cloudian Case\s+(\d+)", " ".join(subject.split()))
    if not match:
        return None
    number = match[1]
    link = case_feedback_link(raw, number)
    if not link:
        return None
    heading = notification_heading("Cloudian 지원 만족도 평가", received, f"⭐ 케이스 평가 · #{number}")
    return f"{heading}\n\n지원 평가: {link}"


def format_mail_notification(subject, body, translation, language, model, *, source_language="영어",
                             received=None, case_status=None):
    markers = {"한국어": "🇰🇷", "영어": "🇺🇸", "일본어": "🇯🇵"}
    language_marker = markers.get(language, language)
    source_marker = markers.get(source_language, source_language)
    label = f"새 이메일 도착 · {case_status}" if case_status else "새 이메일 도착"
    heading = notification_heading(subject, received, label)
    translation_label = f"{model or 'Codex 기본 모델'} 번역 ({language_marker})"
    return (
        f"{heading}\n\n"
        f"━━━━━━━\n원문 ({source_marker})\n━━━━━━━\n{compact_mail_text(body)}\n\n"
        f"━━━━━━━━━━━━━━\n{translation_label}\n━━━━━━━━━━━━━━\n"
        f"{compact_mail_text(translation)}"
    )


def attachment_filename(uid, heading):
    match = re.search(r"(?m)^제목:[ \t]*(.+)$", heading)
    if not match:
        return f"mail-{uid}.txt"
    title = re.sub(r"\s*\[\s*ref:![^\]\r\n]+:ref\s*\]\s*$", "", match[1], flags=re.I)
    case = re.match(r"(?i)^(?:New (?:Case|Comment)\s*|\[Case Closed\]\s*)?\[(\d{6,})\]\s*(.*)$", title)
    if case:
        title = f"{case[1]} - {case[2]}" if case[2] else case[1]
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title)
    title = title.encode("utf-8")[:180].decode("utf-8", errors="ignore").strip(" .")
    if re.match(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", title):
        title = "_" + title
    return f"{title or f'mail-{uid}'}.txt"


def mail_preview_keyboard(view):
    label = {"open": "본문 보기 ▾", "close": "접기 ▴", "file": "본문 파일 받기"}[view]
    return {"inline_keyboard": [[{"text": label, "callback_data": f"mailview:{view}"}]]}


def handle_mail_preview(database, scope, token, chat_id, callback):
    view = callback["data"].partition(":")[2]
    message_id = callback["message"].get("message_id")
    row = database.execute(
        """SELECT heading, body, filename, file_message FROM mail_previews
           WHERE scope = ? AND chat = ? AND message = ?""",
        (scope, chat_id, message_id),
    ).fetchone()
    def answer(text=""):
        telegram(token, "answerCallbackQuery", callback_query_id=callback["id"], text=text,
                 quiet=True, request_timeout=5)

    if not row:
        answer("저장된 본문이 없습니다.")
        return
    heading, body, filename, file_message = row
    if view == "file" and filename:
        if file_message:
            answer("이 알림에 답장으로 본문 파일을 보냈습니다.")
            return
        answer("본문 파일을 보내는 중입니다.")
        try:
            result = telegram(
                token, "sendDocument", chat_id=chat_id,
                document=(filename, body.encode("utf-8-sig")),
                reply_parameters=json.dumps({"message_id": message_id}),
            )
        except (OSError, RuntimeError):
            post_progress(token, chat_id, "본문 파일을 보내지 못했습니다. 버튼을 다시 눌러 주세요.",
                          reply_to=message_id)
            return
        file_id = result["message_id"]
        with database:
            database.execute("UPDATE mail_previews SET file_message = ? WHERE scope = ? AND chat = ? AND message = ?",
                             (file_id, scope, chat_id, message_id))
            database.execute(
                """INSERT OR IGNORE INTO reply_targets
                   SELECT scope, chat, ?, mailbox, metadata FROM reply_targets
                   WHERE scope = ? AND chat = ? AND message = ?""",
                (file_id, scope, chat_id, message_id),
            )
        return
    if view not in {"open", "close"} or filename:
        answer()
        return
    text, next_view = (body, "close") if view == "open" else (heading, "open")
    try:
        telegram(token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                 reply_markup=mail_preview_keyboard(next_view), link_preview_options={"is_disabled": True},
                 quiet=True, request_timeout=8)
    except TelegramRequestError as error:
        if not error.unchanged:
            answer("본문 표시를 바꾸지 못했습니다. 다시 시도해 주세요.")
            return
    except (OSError, RuntimeError):
        answer("본문 표시를 바꾸지 못했습니다. 다시 시도해 주세요.")
        return
    answer()


def expandable_quote_entity(prefix, body):
    return {
        "type": "expandable_blockquote",
        "offset": len(prefix.encode("utf-16-le")) // 2,
        "length": len(body.encode("utf-16-le")) // 2,
    }


def send_mail(database, token, chat_id, uid, message, report="", *, summary="", reply_enabled=False, progress_id=None, interactive=True):
    preview_enabled = interactive and "\n\n━━━━━━━\n" in message
    reply_hint = "메일 회신: 이 알림에 답장으로 본문을 작성하세요." if reply_enabled else ""
    if reply_hint:
        message += f"\n\n{reply_hint}"
    if report:
        report = report.splitlines()[0]
        message += f"\n\n{report}"
    fits = len(message.encode("utf-16-le")) // 2 <= 4096
    if preview_enabled and fits:
        heading, separator, body = message.partition("\n\n")
        entity = expandable_quote_entity(heading + separator, body)
        return publish_progress(token, chat_id, message, progress_id=progress_id, entities=[entity])
    if preview_enabled:
        heading = (summary or message.split("\n\n", 1)[0])[:1000]
        filename = attachment_filename(uid, summary or heading)
        result = publish_progress(token, chat_id, heading, progress_id=progress_id,
                                  reply_markup=mail_preview_keyboard("file"))
        with database:
            database.execute(
                "INSERT OR REPLACE INTO mail_previews VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (mailbot_reply.scope_for(token), chat_id, result["message_id"], heading, message, filename),
            )
        return result
    if fits:
        return publish_progress(token, chat_id, message, progress_id=progress_id)
    else:
        caption = {"caption": summary[:450] + "\n본문은 첨부파일에서 확인하세요."} if summary else {}
        if not summary and report:
            caption = {"caption": report}
        if reply_hint:
            caption["caption"] = "\n\n".join(part for part in (caption.get("caption"), reply_hint) if part)
        filename = attachment_filename(uid, summary or message.split("\n\n", 1)[0])
        return publish_progress(token, chat_id, caption.get("caption", ""), progress_id=progress_id,
                                document=(filename, message.encode("utf-8-sig")))

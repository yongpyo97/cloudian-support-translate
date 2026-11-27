import imaplib
import json
import re
import ssl
from contextlib import ExitStack
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

import mailbot_reply

from mailbot_state import UID_DIRECTORY_PAGE_SIZE, uid_subject
from mailbot_message import compact_mail_text, mail_content


def checked(result, action):
    status, data = result
    if status != "OK":
        raise RuntimeError(f"IMAP {action} 실패")
    return data


def mail_uid(connection, action, *args):
    try:
        return checked(connection.uid(*args), action)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as error:
        raise MailboxOperationError(f"IMAP {action} 실패 ({type(error).__name__})") from error


def mailbox_number(connection, name):
    values = connection.response(name)[1]
    if not values or not values[-1]:
        raise RuntimeError(f"IMAP 응답에 {name} 값이 없습니다.")
    return int(values[-1])


def verify_imap_account(account, password):
    if not password:
        raise ValueError("IMAP 비밀번호를 입력하세요.")
    print("IMAP 연결과 받은편지함 접근을 확인합니다.")
    with imaplib.IMAP4_SSL(
        account["imap_host"], account["imap_port"],
        ssl_context=ssl.create_default_context(), timeout=30,
    ) as connection:
        connection.login(account["imap_user"], password)
        mailbox = account["mailbox"].replace("\\", "\\\\").replace('"', '\\"')
        checked(connection.select(f'"{mailbox}"', readonly=True), "받은편지함 열기")


class MailboxOperationError(RuntimeError):
    pass


def open_mailbox(stack, settings, password, *, readonly=True):
    host, port = settings["imap_host"], settings.get("imap_port", 993)
    user = settings["imap_user"]
    mailbox = settings.get("mailbox", "INBOX")
    quoted_mailbox = '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'
    stage = "서버 연결"
    try:
        connection = stack.enter_context(imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context(), timeout=30,
        ))
        stage = "로그인"
        connection.login(user, password)
        stage = "메일함 열기"
        checked(connection.select(quoted_mailbox, readonly=readonly), "폴더 열기")
    except (OSError, imaplib.IMAP4.error, RuntimeError) as error:
        if isinstance(error, ssl.SSLCertVerificationError):
            detail = error.verify_message
        elif isinstance(error, imaplib.IMAP4.error):
            detail = "서버가 IMAP 요청을 거부했습니다."
        elif isinstance(error, RuntimeError):
            detail = str(error)
        else:
            detail = error.strerror or type(error).__name__
        raise RuntimeError(f"IMAP {stage} 실패 · {user} · {host}:{port} · {mailbox} ({detail})") from None
    return connection


def inbox_uid_page(account, password, before=None):
    with ExitStack() as stack:
        connection = open_mailbox(stack, account, password, readonly=True)
        validity = mailbox_number(connection, "UIDVALIDITY")
        if before and validity != before[0]:
            raise RuntimeError("메일함의 식별 정보가 바뀌었습니다. 최신 목록을 다시 불러오세요.")
        search = ("UID", f"1:{before[1] - 1}") if before else ("ALL",)
        if before and before[1] <= 1:
            return [], None
        hits = checked(connection.uid("SEARCH", None, *search), "메일 목록 검색")
        ids = sorted({int(uid) for uid in (hits[0] or b"").split() if not before or int(uid) < before[1]})
        batch = ids[-UID_DIRECTORY_PAGE_SIZE:]
        if not batch:
            return [], None
        data = checked(connection.uid("FETCH", ",".join(map(str, batch)),
                                      "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT)])"),
                       "메일 목록 헤더 읽기")
        records = []
        parser = BytesParser(policy=policy.default)
        for item in data:
            if not isinstance(item, tuple):
                continue
            uid = re.search(rb"\bUID (\d+)", item[0])
            if not uid:
                continue
            timestamp = imap_received_at(item[0])
            received = int(timestamp.timestamp()) if timestamp else 0
            subject = uid_subject(str(parser.parsebytes(item[1], headersonly=True).get("Subject", "")))
            records.append((int(uid[1]), validity, received, subject))
    records.sort(key=lambda row: (row[2], row[0]), reverse=True)
    return records, (validity, batch[0]) if len(ids) > len(batch) else None


def imap_received_at(row):
    date_match = re.search(rb'\bINTERNALDATE "([^"]+)"', row)
    if date_match:
        date = date_match[1].decode("ascii").strip().replace("-", " ", 2)
        timestamp = parsedate_to_datetime(date)
        if timestamp.tzinfo is not None:
            return timestamp


def is_original_cloudian_reply(account, password, message_id):
    quoted_id = '"' + message_id.replace("\\", "\\\\").replace('"', '\\"') + '"'
    with ExitStack() as stack:
        connection = open_mailbox(stack, account, password)
        rows = checked(connection.uid("SEARCH", None, "HEADER", "Message-ID", quoted_id), "Cloudian 원본 검색")
        for uid in (rows[0] or b"").split():
            data = checked(connection.uid("FETCH", uid, "(BODY.PEEK[])"), "Cloudian 원본 확인")
            raw = next((part[1] for part in data if isinstance(part, tuple)), None)
            if raw is not None:
                _, body = mail_content(raw)
                target = mailbot_reply.target_metadata(raw, compact_mail_text(body))
                if target and target["cloudian_original"] and target["in_reply_to"] == message_id:
                    return True
    return False


def find_legacy_reply(accounts, title, timestamp):
    # IMAP date searches use the server's calendar date. Check the adjacent
    # days as well, then compare the exact timestamp with its time zone.
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

    def search_date(date):
        return f"{date.day:02d}-{months[date.month - 1]}-{date.year}"

    matches = []
    with ExitStack() as stack:
        for key, (account, password) in accounts.items():
            connection = open_mailbox(stack, account, password)
            rows = checked(connection.uid(
                "SEARCH", None, "SINCE", search_date(timestamp - timedelta(days=1)),
                "BEFORE", search_date(timestamp + timedelta(days=2)),
            ), "기존 알림 원본 검색")
            ids = (rows[0] or b"").split()
            for offset in range(0, len(ids), 200):
                dates = checked(connection.uid("FETCH", b",".join(ids[offset:offset + 200]), "(UID INTERNALDATE)"),
                                "기존 알림 수신 시각 확인")
                for row in dates:
                    if not isinstance(row, bytes):
                        continue
                    uid = re.search(rb"\bUID (\d+)", row)
                    internal = re.search(rb'\bINTERNALDATE "([^"]+)"', row)
                    if not uid or not internal:
                        continue
                    date = internal[1].decode("ascii").strip().replace("-", " ", 2)
                    if parsedate_to_datetime(date) != timestamp:
                        continue
                    data = checked(connection.uid("FETCH", uid[1], "(BODY.PEEK[])"), "기존 알림 원문 읽기")
                    raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                    if raw is None:
                        continue
                    subject, body = mail_content(raw)
                    without_ref = re.sub(r"\s*\[\s*ref:![^\]\r\n]+:ref\s*\]\s*$", "", subject, flags=re.I)
                    if title.strip() not in {" ".join(subject.split()), " ".join(without_ref.split()) or "(제목 없음)"}:
                        continue
                    matches.append((key, mailbot_reply.target_metadata(raw, compact_mail_text(body))))
                    if len(matches) > 1:
                        return None
    if len(matches) == 1 and matches[0][1]:
        return matches[0][0], json.dumps(matches[0][1])
    return None

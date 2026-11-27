import hashlib
import json
import sqlite3

import mailbot_reply


def mark_handled(database, key, validity, uid):
    database.execute("DELETE FROM mail_body_retry WHERE mailbox = ? AND validity = ? AND uid = ?",
                     (key, validity, uid))
    database.execute("INSERT OR IGNORE INTO handled VALUES (?, ?, ?)", (key, validity, uid))
    database.execute("DELETE FROM pending WHERE mailbox = ? AND uid = ?", (key, uid))
    database.execute("DELETE FROM cloudian_fallback WHERE mailbox = ? AND validity = ? AND uid = ?",
                     (key, validity, uid))


def imap_mailbox_identity(settings):
    return [
        settings["imap_host"], settings.get("imap_port", 993), settings["imap_user"],
        settings.get("mailbox", "INBOX"),
    ]


def mailbox_key(settings):
    identity = [*imap_mailbox_identity(settings), str(settings["telegram_chat_id"])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def uid_directory_key(settings):
    return hashlib.sha256(json.dumps(imap_mailbox_identity(settings)).encode()).hexdigest()


def open_state(config):
    database = sqlite3.connect(config.with_suffix(".sqlite3"))
    database.executescript("""
        CREATE TABLE IF NOT EXISTS cursor (mailbox TEXT PRIMARY KEY, validity INTEGER, uid INTEGER);
        CREATE TABLE IF NOT EXISTS pending (mailbox TEXT PRIMARY KEY, uid INTEGER, parts TEXT, sent INTEGER);
        CREATE TABLE IF NOT EXISTS mail_body_retry
            (mailbox TEXT, validity INTEGER, uid INTEGER, PRIMARY KEY (mailbox, validity, uid));
        CREATE TABLE IF NOT EXISTS manual_retranslation (
            mailbox TEXT, uid INTEGER, validity INTEGER, payload TEXT,
            PRIMARY KEY (mailbox, uid)
        );
        CREATE TABLE IF NOT EXISTS handled
            (mailbox TEXT, validity INTEGER, uid INTEGER, PRIMARY KEY (mailbox, validity, uid));
        CREATE TABLE IF NOT EXISTS acknowledgement
            (mailbox TEXT, case_id TEXT, PRIMARY KEY (mailbox, case_id));
        CREATE TABLE IF NOT EXISTS cloudian_fallback
            (mailbox TEXT, validity INTEGER, uid INTEGER, PRIMARY KEY (mailbox, validity, uid));
        CREATE TABLE IF NOT EXISTS mail_uid_directory
            (mailbox TEXT, validity INTEGER, uid INTEGER, received INTEGER, subject TEXT,
             PRIMARY KEY (mailbox, validity, uid));
        CREATE INDEX IF NOT EXISTS mail_uid_directory_recent
            ON mail_uid_directory (mailbox, received DESC, validity DESC, uid DESC);
    """)
    mailbot_reply.initialize(database)
    return database


UID_DIRECTORY_PAGE_SIZE = 100


def remember_mail_uids(database, account, records):
    key = uid_directory_key(account)
    with database:
        database.executemany(
            "INSERT OR REPLACE INTO mail_uid_directory VALUES (?, ?, ?, ?, ?)",
            ((key, validity, uid, received, subject) for uid, validity, received, subject in records),
        )


def uid_subject(subject):
    return " ".join(subject.split()) or "(제목 없음)"


def saved_uid_page(database, account, before=None):
    where = "AND (received, validity, uid) < (?, ?, ?)" if before else ""
    records = database.execute(
        f"""SELECT uid, validity, received, subject FROM mail_uid_directory WHERE mailbox = ? {where}
            ORDER BY received DESC, validity DESC, uid DESC LIMIT ?""",
        (uid_directory_key(account), *(before or ()), UID_DIRECTORY_PAGE_SIZE + 1),
    ).fetchall()
    if len(records) <= UID_DIRECTORY_PAGE_SIZE:
        return records, None
    uid, validity, received, _subject = records[-2]
    return records[:-1], (received, validity, uid)

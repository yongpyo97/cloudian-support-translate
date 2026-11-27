import importlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import mailbot


def module_for(function):
    return importlib.import_module(function.__module__)


class MailbotBehaviorTests(unittest.TestCase):
    def test_prefers_plain_text_and_can_extract_html_links(self):
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "Test subject"
        message.set_content("Plain body")
        message.add_alternative('<p>HTML <a href="https://example.invalid/case">case</a></p>'
                                "<script>hidden</script>", subtype="html")
        self.assertEqual(mailbot.mail_content(message.as_bytes()), ("Test subject", "Plain body"))
        subject, body = mailbot.mail_content(message.as_bytes(), prefer_html=True)
        self.assertEqual(subject, "Test subject")
        self.assertEqual(body, "HTML case https://example.invalid/case")

    def test_cloudian_comment_fingerprint_normalizes_formatting(self):
        first = mailbot.comment_fingerprint("customer", "123", "- **Details:**\nDone.")
        self.assertEqual(first, mailbot.comment_fingerprint("customer", "123", "Details: Done."))
        self.assertNotEqual(first, mailbot.comment_fingerprint("support", "123", "Details: Done."))
        self.assertNotEqual(first, mailbot.comment_fingerprint("customer", "124", "Details: Done."))

    def test_telegram_preserves_http_error_and_unchanged_result(self):
        transport = module_for(mailbot.telegram)
        error = HTTPError("https://example.invalid", 400, "Bad Request", {},
                          io.BytesIO(b'{"description":"Bad Request: message is not modified"}'))
        with patch.object(transport, "urlopen", side_effect=error):
            with self.assertLogs(level="WARNING"):
                with self.assertRaises(mailbot.TelegramRequestError) as result:
                    mailbot.telegram("token", "editMessageText", quiet=True)
        self.assertEqual(result.exception.code, 400)
        self.assertTrue(result.exception.unchanged)
        with patch.object(transport, "telegram", side_effect=result.exception):
            self.assertTrue(mailbot.update_progress("token", 1, 2, "same text"))

    def test_codex_reply_requires_three_values_and_excludes_title(self):
        module = module_for(mailbot.prepare_reply)
        result = {"polished_korean": " 정리 ", "english": " Reply. ", "back_translation": " 번역 "}
        with patch.object(module, "codex_translation", return_value=json.dumps(result)) as codex:
            self.assertEqual(mailbot.prepare_reply("codex", "Subject", "본문", model=None,
                                                   custom_prompt="", app_adds_signature=True),
                             ("정리", "Reply.", "번역"))
        self.assertIn("Do not add a title", codex.call_args.args[1])
        self.assertIn("Do not add either to the body", codex.call_args.args[1])
        self.assertEqual(codex.call_args.kwargs["schema"]["required"], list(result))

    def test_imap_error_keeps_original_cause(self):
        connection = Mock()
        failure = OSError("closed")
        connection.uid.side_effect = failure
        with self.assertRaises(mailbot.MailboxOperationError) as caught:
            mailbot.mail_uid(connection, "본문 읽기", "FETCH", "42")
        self.assertIs(caught.exception.__cause__, failure)
        connection.uid.assert_called_once_with("FETCH", "42")

    def test_existing_pending_row_retries_only_unsent_parts(self):
        account = {"imap_host": "mail.example.invalid", "imap_user": "test@example.invalid",
                   "telegram_chat_id": 1}
        key = mailbot.mailbox_key(account)
        module = module_for(mailbot.process_mail)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            database = mailbot.open_state(config)
            try:
                with database:
                    database.execute("INSERT INTO pending VALUES (?, ?, ?, ?)",
                                     (key, 42, json.dumps(["first", "second", "third"]), 1))
                connection = Mock()
                def process():
                    mailbot.process_mail(connection, account, "token", "codex", database, key, 7, 42,
                                         destination_keys=(key,), received=datetime.now(timezone.utc),
                                         retranslate=None)
                with patch.object(module, "telegram", side_effect=[{}, OSError("offline")]) as send:
                    with self.assertRaises(OSError):
                        process()
                    self.assertEqual([item.kwargs["text"] for item in send.call_args_list], ["second", "third"])
                self.assertEqual(database.execute("SELECT sent FROM pending").fetchone(), (2,))
                database.close()
                database = mailbot.open_state(config)
                with patch.object(module, "telegram") as send:
                    process()
                    send.assert_has_calls([call("token", "sendMessage", chat_id=1, text="third",
                                                link_preview_options={"is_disabled": True})])
                    self.assertEqual(send.call_count, 1)
                self.assertIsNone(database.execute("SELECT sent FROM pending").fetchone())
                self.assertEqual(database.execute("SELECT mailbox, validity, uid FROM handled").fetchone(),
                                 (key, 7, 42))
                connection.uid.assert_not_called()
            finally:
                database.close()

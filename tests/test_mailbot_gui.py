import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if os.name == "nt":
    import mailbot
    import mailbot_gui as gui


def value(text):
    return SimpleNamespace(get=lambda: text)


@unittest.skipUnless(os.name == "nt", "The desktop app runs on Windows.")
class DesktopBehaviorTests(unittest.TestCase):
    def test_account_identity_and_branding(self):
        account = dict(imap_user="user@example.invalid", imap_host="mail.example.invalid",
                       imap_port=993, mailbox="INBOX")
        self.assertEqual(gui.account_key(account), ("user@example.invalid", "mail.example.invalid", 993, "INBOX"))
        self.assertEqual(gui.app_display_name(gui.normalize_company_name(" Team ")), "Team " + gui.APP_NAME)
        with self.assertRaises(ValueError):
            gui.normalize_company_name("Team\nName")
        self.assertTrue(gui.asset_path().is_file())

    def test_theme_and_model_selection(self):
        window = object.__new__(gui.Window)
        window.appearance = value(gui.THEME_LABELS["dark"])
        window.model = value("gpt-6.2-sol" + gui.MODEL_LABEL_SEPARATOR + "example")
        self.assertEqual(window.theme_choice(), "dark")
        self.assertEqual(window.selected_model_id(), "gpt-6.2-sol")
        self.assertEqual(gui.theme_color("text"), tuple(gui.THEME_COLORS[mode]["text"] for mode in ("light", "dark")))

    def test_chat_discovery_and_selection(self):
        engine = Mock()
        engine.telegram.return_value = [
            {"message": {"chat": {"id": 1, "type": "private", "first_name": "Test"}}},
            {"my_chat_member": {"chat": {"id": -2, "type": "supergroup", "title": "Team"}}},
        ]
        self.assertEqual(gui.discover_chats(engine, "token"), {1: ("Test", "private"), -2: ("Team", "supergroup")})
        window = object.__new__(gui.Window)
        window.chat_mode, window.private_chat_id, window.group_chat_id = value("group"), value("1"), value(" -2 ")
        self.assertEqual(window.current_chat_id(), -2)

    def test_builds_settings_window_and_switches_theme(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            root = gui.ctk.CTk()
            root.withdraw()
            self.addCleanup(root.destroy)
            engine = Mock(APP_VERSION=mailbot.APP_VERSION)
            engine.runtime_options.side_effect = mailbot.runtime_options
            engine.find_codex.return_value = None
            with patch.object(gui, "MailWorker"), patch.object(gui.pystray, "Icon") as tray:
                window = gui.Window(root, engine, config)
                self.assertIn(mailbot.APP_VERSION, root.title())
                self.assertEqual(window.table.get_children(), ())
                self.assertTrue(window.model_refresh.winfo_exists())
                window.appearance.set(gui.THEME_LABELS["dark"])
                window.update_appearance()
                self.assertEqual(window.active_theme, "dark")
                window.appearance.set(gui.THEME_LABELS["light"])
                window.update_appearance()
                self.assertEqual(window.active_theme, "light")
                tray.return_value.run_detached.assert_called_once_with()

    def test_apply_and_start_use_the_same_worker_payload(self):
        engine = Mock()
        engine.find_codex.return_value = "codex"
        engine.runtime_options.side_effect = mailbot.runtime_options
        engine.unlock_password.return_value = "secret"
        engine.telegram.return_value = {"type": "private"}
        engine.imap_accounts.side_effect = mailbot.imap_accounts
        window = object.__new__(gui.Window)
        window.engine, window.config = engine, Path("settings.json")
        window.busy = window.closing = window.config_error = False
        window.running = True
        account = dict(imap_user="user@example.invalid", imap_host="mail.example.invalid",
                       imap_port=993, mailbox="INBOX", protected_password="protected")
        window.accounts = [account]
        window.settings = {"accounts": window.accounts, "telegram_chat_id": 1, "background_token": "protected"}
        window.current_token = lambda: "token"
        window.chat_mode, window.private_chat_id, window.group_chat_id = value("private"), value("1"), value("")
        window.model, window.reply_model = value(""), value("gpt-5.6-sol")
        window.token, window.reply_users, window.language, window.interval = value(""), value(""), value("한국어"), value("20")
        window.global_prompt = window.reply_prompt = window.reply_users_group = ""
        window.reply_format, window.available_models = {}, []
        window.keep_running, window.autostart = value(True), value(False)
        window.appearance = value(gui.THEME_LABELS["dark"])
        observed = {}
        window.async_action = lambda action, event, notice: observed.update({event: action()})
        window.apply()
        window.start_saved()
        applied, started = observed["applied"], observed["started"]
        self.assertEqual(applied[1:], started[1:])
        self.assertEqual(applied[0]["ui_theme"], "dark")
        self.assertEqual(applied[0]["telegram_chat_id"], 1)
        engine.save_accounts.assert_called_once_with(window.config, applied[0], [account])
        self.assertEqual(engine.telegram.call_args_list, [call("token", "getMe"), call("token", "getChat", chat_id=1)])

    def test_worker_applies_pauses_resumes_and_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            mailbot.open_state(config).close()
            events = queue.Queue()
            engine = Mock()
            engine.open_state.side_effect = mailbot.open_state
            engine.imap_accounts.return_value = []
            engine.runtime_options.return_value = {"poll_seconds": 60}
            with patch.object(gui.mailbot_reply, "check_updates", side_effect=lambda *a, **k: None):
                worker = gui.MailWorker(engine, config, events)
                try:
                    worker.request("apply", ({"telegram_chat_id": 1}, {}, "token", "codex"))
                    self.assertEqual(events.get(timeout=5), ("worker-status", "메일 확인 대기"))
                    self.assertEqual(events.get(timeout=5), ("worker-status", "메일 확인 중"))
                    self.assertEqual(events.get(timeout=5), ("worker-status", "메일 확인 대기"))
                    worker.request("pause")
                    self.assertEqual(events.get(timeout=5), ("worker-status", "일시정지"))
                    worker.request("resume")
                    self.assertEqual(events.get(timeout=5), ("worker-status", "메일 확인 대기"))
                finally:
                    worker.request("exit")
                    for thread in (worker.thread, worker.telegram_thread, worker.reply_thread):
                        thread.join(5)
                        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

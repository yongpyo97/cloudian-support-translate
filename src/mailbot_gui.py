import imaplib
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk
import pystray
import mailbot_reply
from PIL import Image

from mailbot_gui_worker import (
    ACCOUNT_FIELDS,
    account_key,
    inherit_delivery_history,
    MailWorker,
    NETWORK_ERRORS,
)
from mailbot_gui_theme import (
    _ThemeWidgets,
    APP_NAME,
    BODY_FONT,
    CHOICE_HOVER,
    FieldComboBox,
    theme_color,
    THEME_COLORS,
    THEME_LABELS,
    windows_appearance,
)
from mailbot_gui_uid import _UidDialog, KOREA_TIME
from mailbot_gui_accounts import _AccountEditor
from mailbot_gui_telegram import _TelegramSettings, discover_chats
from mailbot_gui_codex import (
    _CodexSettings,
    MODEL_LABEL_SEPARATOR,
    MODEL_REFRESH_MS,
    MODEL_USE_CASES,
    QUOTA_LOW,
    QUOTA_NORMAL,
    QUOTA_WINDOWS,
    TRANSLATION_MODELS,
)
from mailbot_gui_prompts import _PromptEditor
from mailbot_gui_layout import _MainLayout


COMPANY_NAME_LIMIT = 32


def asset_path(filename="cloudian.ico"):
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / filename
    return Path(__file__).resolve().parent.parent / "work" / filename


def normalize_company_name(value):
    if not isinstance(value, str) or not all(char.isprintable() for char in value):
        raise ValueError("회사명은 한 줄로 입력하세요.")
    name = value.strip()
    if len(name) > COMPANY_NAME_LIMIT:
        raise ValueError(f"회사명은 {COMPANY_NAME_LIMIT}자 이내로 입력하세요.")
    return name


def app_display_name(company_name):
    return f"{company_name} {APP_NAME}" if company_name else APP_NAME


class Window(
    _ThemeWidgets,
    _UidDialog,
    _AccountEditor,
    _TelegramSettings,
    _CodexSettings,
    _PromptEditor,
    _MainLayout,
):
    def __init__(self, root, engine, config, *, hidden=False):
        self.root = root
        self.engine = engine
        self.config = config
        self.ui_config = config.with_suffix(".ui.json")
        self.company_name = ""
        try:
            branding = json.loads(self.ui_config.read_text(encoding="utf-8-sig"))
            self.company_name = normalize_company_name(branding.get("company_name", ""))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            logging.warning("저장된 회사명을 읽지 못했습니다.")
        self.display_name = tk.StringVar(master=root, value=app_display_name(self.company_name))
        self.background_launch = hidden
        self.events = queue.Queue()
        self.busy = self.closing = self.paused = self.running = False
        self.config_error = False
        self.available_models = []
        self.models_loading = False
        self.codex_connecting = False
        self.inline_editor = None
        self.settings = {}
        self.accounts = []
        try:
            if config.exists():
                self.settings = json.loads(config.read_text(encoding="utf-8-sig"))
                self.accounts = engine.imap_accounts(self.settings)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.config_error = True
            self.settings = {}
        try:
            options = engine.runtime_options(self.settings)
        except (ValueError, TypeError, AttributeError):
            options = engine.runtime_options({})
        self.model = tk.StringVar(value=options["codex_model"] or "")
        self.reply_model = tk.StringVar(value=options["reply_codex_model"])
        self.global_prompt = options["global_translation_prompt"]
        self.reply_prompt = options["reply_translation_prompt"]
        self.reply_format = mailbot_reply.reply_format_options(options)
        self.prompt_status = tk.StringVar()
        self.language = tk.StringVar(value=options["language"])
        self.interval = tk.StringVar(value=str(options["poll_seconds"]))
        saved_chat = str(self.settings.get("telegram_chat_id") or "")
        chat_mode = self.settings.get("telegram_chat_mode") or ("group" if saved_chat.startswith("-") else "private")
        self.chat_mode = tk.StringVar(value=chat_mode)
        self.private_chat_id = tk.StringVar(value=str(
            self.settings.get("telegram_private_chat_id", saved_chat if chat_mode == "private" else "")))
        self.group_chat_id = tk.StringVar(value=str(
            self.settings.get("telegram_group_chat_id", saved_chat if chat_mode == "group" else "")))
        self.reply_users = tk.StringVar(value=", ".join(map(str, options["reply_allowed_users"])))
        self.reply_users_group = self.group_chat_id.get().strip()
        self.token = tk.StringVar()
        self.has_token = bool(self.settings.get("background_token"))
        self.keep_running = tk.BooleanVar(value=self.settings.get("gui_close_to_tray", True))
        self.autostart = tk.BooleanVar(value=self.settings.get("gui_autostart", False))
        self.appearance = tk.StringVar(value=THEME_LABELS.get(self.settings.get("ui_theme"), THEME_LABELS["system"]))
        self.status = tk.StringVar(value="설정 필요")
        self.notice = tk.StringVar(value="이메일 계정과 Telegram 설정을 입력한 뒤 변경을 적용하세요.")
        if self.config_error:
            self.notice.set("기존 설정 파일을 읽지 못했습니다. 로그와 설정 파일을 확인하세요.")
            logging.error("기존 설정 파일을 읽지 못했습니다.")
        root.title(f"{self.display_name.get()} · v{self.engine.APP_VERSION}")
        root.geometry("960x640")
        root.minsize(620, 450)
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        icon = asset_path()
        root.iconbitmap(str(icon))
        with Image.open(icon) as image:
            light_logo = image.convert("RGBA")
        with Image.open(asset_path("cloudian-dark.ico")) as image:
            dark_logo = image.convert("RGBA")
        self.logos = {"light": light_logo, "dark": dark_logo}
        self.header_logo = ctk.CTkImage(light_image=light_logo, dark_image=dark_logo, size=(30, 30))
        self.build()
        self.worker = MailWorker(engine, config, self.events)
        self.tray_theme = windows_appearance(taskbar=True)
        self.tray = pystray.Icon(
            "CloudianSupportTranslation", self.logos[self.tray_theme], self.display_name.get(),
            menu=pystray.Menu(
                pystray.MenuItem("설정 열기", self.tray_action("open"), default=True),
                pystray.MenuItem(lambda _: "재개" if self.paused else "일시정지", self.tray_action("pause")),
                pystray.MenuItem("로그 열기", self.tray_action("logs")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료", self.tray_action("exit")),
            ),
        )
        self.tray.run_detached()
        if hidden and self.has_token:
            root.withdraw()
        self.root.after(80, self.drain_events)
        self.root.after(2_000, self.watch_appearance)
        self.root.after(100, self.load_models)
        self.root.after(MODEL_REFRESH_MS, self.refresh_models_periodically)
        if self.has_token and not self.config_error:
            self.root.after(180, self.start_saved)

    def save_company_name(self, value):
        company_name = normalize_company_name(value)
        self.engine.save_settings(self.ui_config, {"company_name": company_name})
        self.company_name = company_name
        name = app_display_name(company_name)
        self.display_name.set(name)
        self.root.title(f"{name} · v{self.engine.APP_VERSION}")
        self.tray.title = name

    def edit_company_name(self):
        if self.busy or self.closing:
            return
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("회사명 변경")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        panel = self.frame(dialog)
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        self.label(panel, text="회사명", heading=True).pack(anchor="w")
        value = tk.StringVar(master=dialog, value=self.company_name)
        entry = self.entry(panel, textvariable=value, width=400)
        entry.pack(fill="x", pady=(6, 4))
        self.label(panel, text=f"비워두면 {APP_NAME}로 표시됩니다.", muted=True).pack(anchor="w")
        error_label = self.label(panel, text="", text_color=("#9b3525", "#f3a094"), wraplength=400)

        def submit(_event=None):
            if self.busy or self.closing:
                error_label.configure(text="진행 중인 작업이 끝난 뒤 다시 적용하세요.")
                error_label.pack(anchor="w", before=buttons, pady=(4, 0))
                return
            try:
                self.save_company_name(value.get())
            except (OSError, ValueError) as error:
                message = str(error) if isinstance(error, ValueError) else "회사명 설정을 저장하지 못했습니다."
                error_label.configure(text=message)
                error_label.pack(anchor="w", before=buttons, pady=(4, 0))
                return
            self.notice.set("표시 이름을 변경했습니다.")
            dialog.destroy()

        buttons = self.frame(panel)
        buttons.pack(fill="x", pady=(12, 0))
        self.button(buttons, text="취소", command=dialog.destroy).pack(side="left")
        self.button(buttons, text="변경 적용", primary=True, command=submit).pack(side="right")
        dialog.bind("<Return>", submit)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        entry.focus_set()

    def set_busy(self, busy):
        self.busy = busy
        widgets = [self.root]
        while widgets:
            for child in widgets.pop().winfo_children():
                if isinstance(child, (ctk.CTkButton, ctk.CTkEntry, ctk.CTkSwitch, ctk.CTkRadioButton,
                                      ctk.CTkComboBox, ctk.CTkOptionMenu, ttk.Spinbox)):
                    disabled = busy or (
                        self.models_loading and child in (self.model_refresh, self.quota_refresh)
                    ) or (self.codex_connecting and child is self.codex_connect)
                    child.configure(state="disabled" if disabled else getattr(child, "enabled_state", "normal"))
                widgets.append(child)

    def async_action(self, action, result_kind, waiting):
        self.set_busy(True)
        self.notice.set(waiting)
        def execute():
            try:
                result = action()
            except NETWORK_ERRORS as error:
                detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
                logging.warning("설정 또는 연결 작업 실패 (%s)", detail)
                self.events.put(("operation-error", detail))
            else:
                self.events.put((result_kind, result))
        threading.Thread(target=execute, name=result_kind, daemon=True).start()

    def apply(self):
        if self.busy or self.closing:
            return
        if self.config_error:
            self.notice.set("기존 설정 파일을 확인한 뒤 앱을 다시 열어주세요.")
            return
        if not self.accounts:
            self.notice.set("이메일 계정을 한 개 이상 추가하세요.")
            return
        try:
            token, chat = self.current_token(), self.current_chat_id()
            group_chat = self.group_chat_id.get().strip()
            options = self.engine.runtime_options({
                "codex_model": self.selected_model_id(),
                "reply_codex_model": self.reply_model.get().partition(MODEL_LABEL_SEPARATOR)[0].strip(),
                "reply_allowed_users": self.reply_users.get() if self.reply_users_group == group_chat else "",
                "language": self.language.get().strip(), "poll_seconds": self.interval.get(),
                "global_translation_prompt": self.global_prompt,
                "reply_translation_prompt": self.reply_prompt,
                **self.reply_format,
            })
        except (ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        existing = self.settings.copy()
        drafts = [dict(account) for account in self.accounts]
        new_settings = existing | options | {
            "telegram_chat_id": chat,
            "telegram_chat_mode": self.chat_mode.get(),
            "ui_theme": self.theme_choice(),
            "telegram_private_chat_id": self.private_chat_id.get().strip(),
            "telegram_group_chat_id": group_chat,
            "gui_close_to_tray": self.keep_running.get(), "gui_autostart": self.autostart.get(),
        }
        if self.available_models and options["codex_model"] and options["codex_model"] not in self.available_models:
            if options["codex_model"] != existing.get("codex_model"):
                self.notice.set("로그인한 Codex에서 사용할 수 있는 모델을 선택하세요.")
                return
        if existing.get("telegram_chat_id") and str(existing["telegram_chat_id"]) != str(chat):
            new_settings["previous_telegram_chat_id"] = existing["telegram_chat_id"]
        was_running = self.running
        changed_token = bool(self.token.get().strip())
        changed_chat = str(existing.get("telegram_chat_id")) != str(chat)

        def validate():
            codex = self.engine.find_codex()
            if not codex:
                raise RuntimeError("Codex CLI를 찾지 못했습니다.")
            saved = self.engine.imap_accounts(existing) if existing else []
            old_accounts = {account_key(account): account for account in saved}
            passwords = {}
            for account in drafts:
                key = account_key(account)
                previous = old_accounts.get(key, {})
                replacement = account.pop("new_password", None)
                try:
                    password = replacement or self.engine.unlock_password(account["protected_password"])
                except (KeyError, OSError, ValueError, TypeError):
                    raise RuntimeError(f"{account['imap_user']} 계정의 비밀번호를 다시 입력하세요.") from None
                if replacement or not previous or not was_running:
                    try:
                        self.engine.verify_imap_account(account, password)
                    except imaplib.IMAP4.error:
                        raise RuntimeError(f"{account['imap_user']} IMAP 로그인을 확인하지 못했습니다.") from None
                if replacement:
                    account["protected_password"] = self.engine.protect_password(password)
                if account.get("smtp_host") and (replacement or not was_running or
                                                 account["smtp_host"] != previous.get("smtp_host") or
                                                 account.get("smtp_port", 465) != previous.get("smtp_port", 465)):
                    mailbot_reply.verify_smtp(account, password)
                passwords[key] = password
            if not was_running or changed_token or changed_chat:
                self.engine.telegram(token, "getMe")
            reply_enabled = any(account.get("smtp_host") for account in drafts)
            if reply_enabled or not was_running or changed_token or changed_chat:
                chat_info = self.engine.telegram(token, "getChat", chat_id=chat)
                expected = {"group", "supergroup"} if new_settings["telegram_chat_mode"] == "group" else {"private"}
                if chat_info.get("type") not in expected:
                    raise RuntimeError("선택한 채팅 종류와 Telegram Chat ID가 일치하지 않습니다.")
            if changed_token or not existing.get("background_token"):
                new_settings["background_token"] = self.engine.protect_password(token)
            new_settings["accounts"] = drafts
            old_auto, new_auto = bool(existing.get("gui_autostart")), new_settings["gui_autostart"]
            if old_auto != new_auto:
                self.engine.background_task(self.config, enable=new_auto, start_now=False, stop_running=False)
            try:
                self.engine.save_accounts(self.config, new_settings, drafts)
            except OSError:
                if old_auto != new_auto:
                    self.engine.background_task(self.config, enable=old_auto, start_now=False, stop_running=False)
                raise
            return new_settings, passwords, token, codex

        self.async_action(validate, "applied", "연결을 확인한 뒤 변경을 적용합니다. 진행 중인 메일은 기존 설정으로 처리합니다.")

    def start_saved(self):
        if self.busy or self.closing:
            return
        saved = self.settings.copy()
        accounts = [dict(account) for account in self.accounts]
        try:
            token, chat = self.current_token(), self.current_chat_id()
        except (ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        def start():
            codex = self.engine.find_codex()
            if not codex:
                raise RuntimeError("Codex CLI를 찾지 못했습니다.")
            passwords = {}
            for account in accounts:
                try:
                    passwords[account_key(account)] = self.engine.unlock_password(account["protected_password"])
                except (KeyError, OSError, ValueError, TypeError):
                    raise RuntimeError(f"{account['imap_user']}의 IMAP 비밀번호를 다시 저장하세요.") from None
            self.engine.telegram(token, "getMe")
            chat_info = self.engine.telegram(token, "getChat", chat_id=chat)
            if any(account.get("smtp_host") for account in accounts) and chat_info.get("type") not in {"private", "group", "supergroup"}:
                raise RuntimeError("메일 회신은 Telegram 개인 또는 그룹 채팅에서 사용할 수 있습니다.")
            saved["reply_allowed_users"] = self.engine.runtime_options(saved)["reply_allowed_users"]
            saved["accounts"] = accounts
            return saved, passwords, token, codex
        self.async_action(start, "started", "저장된 Telegram 연결을 확인하는 중입니다.")

    def tray_action(self, action):
        def handler(icon, item):
            self.events.put(("tray", action))
        return handler

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def close_window(self):
        if self.settings.get("gui_close_to_tray", True) and not self.closing:
            self.root.withdraw()
        else:
            self.exit()

    def toggle_pause(self):
        if not self.running or self.closing:
            self.notice.set("먼저 연결 설정을 적용하세요.")
            return
        self.paused = not self.paused
        self.worker.request("pause" if self.paused else "resume")
        self.pause_button.configure(text="재개" if self.paused else "일시정지")
        self.tray.update_menu()

    def open_logs(self):
        logfile = self.config.with_name("mailbot.log")
        if logfile.is_file():
            os.startfile(logfile)
        else:
            self.notice.set("로그 파일이 아직 만들어지지 않았습니다.")

    def exit(self):
        if self.closing:
            return
        if self.busy:
            self.notice.set("현재 연결 확인이 끝난 뒤 종료할 수 있습니다.")
            self.show_window()
            return
        self.closing = True
        self.status.set("종료 대기")
        self.notice.set("진행 중인 메일을 마친 뒤 종료합니다.")
        self.worker.request("exit")
        self.finish_exit()

    def finish_exit(self):
        if any(thread.is_alive() for thread in (self.worker.thread, self.worker.telegram_thread, self.worker.reply_thread)):
            self.root.after(150, self.finish_exit)
            return
        self.tray.stop()
        self.root.destroy()

    def drain_events(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "notice":
                self.notice.set(value)
            elif kind == "worker-status":
                if not self.closing:
                    self.status.set(value)
                    color = "connected" if value in {"메일 확인 중", "메일 확인 대기", "메일 재번역 중"} else "pending"
                    self.status_label.configure(text_color=theme_color(color))
            elif kind == "codex-login":
                self.codex_connecting = False
                self.codex_connect.configure(state="disabled" if self.busy else "normal")
                if value:
                    self.notice.set(value)
                else:
                    self.notice.set("Codex 계정을 연결했습니다.")
                    self.refresh_models_after_login()
            elif kind == "models":
                models, account, limits, manual, limits_only, failure_message = value
                self.models_loading = False
                quota_received = self.update_codex_account(account, limits)
                if not self.busy:
                    self.model_refresh.configure(state="normal")
                    self.quota_refresh.configure(state="normal")
                if limits_only:
                    self.notice.set("Codex 사용 한도를 새로고침했습니다." if quota_received else "Codex 사용 한도를 확인할 수 없습니다.")
                elif models:
                    self.update_model_picker(models)
                    if manual:
                        self.notice.set(f"사용 가능한 Codex 모델 {len(self.available_models)}개를 불러왔습니다.")
                elif manual or not self.available_models:
                    self.notice.set(failure_message)
            elif kind == "tray":
                {"open": self.show_window, "pause": self.toggle_pause,
                 "logs": self.open_logs, "exit": self.exit}[value]()
            elif kind == "operation-error":
                self.set_busy(False)
                self.notice.set(value)
            elif kind == "chats":
                self.set_busy(False)
                self.notice.set(f"최근 채팅 {len(value)}개를 찾았습니다." if value else "아직 찾은 채팅이 없습니다.")
                self.show_chats(value)
            elif kind == "uid-directory":
                value()
            elif kind == "chat-tested":
                self.set_busy(False)
                self.notice.set("Telegram 봇과 채팅을 확인했습니다.")
            elif kind in {"started", "applied"}:
                self.set_busy(False)
                self.settings = value[0]
                self.accounts = self.engine.imap_accounts(self.settings)
                prompt_options = self.engine.runtime_options(self.settings)
                self.global_prompt = prompt_options["global_translation_prompt"]
                self.reply_prompt = prompt_options["reply_translation_prompt"]
                self.reply_format = mailbot_reply.reply_format_options(prompt_options)
                self.has_token = True
                self.running = True
                self.token.set("")
                self.refresh_token_hint()
                self.refresh_accounts()
                self.refresh_prompt_status()
                self.worker.request("apply", value)
                self.notice.set("변경을 적용했습니다. 다음 메일부터 반영합니다." if kind == "applied" else "저장된 설정으로 메일 확인을 시작했습니다.")
        if self.background_launch and self.engine.background_stop_path(self.config).exists() and not self.busy:
            self.exit()
        elif not self.closing:
            self.root.after(100, self.drain_events)


def run(engine, config, *, hidden=False):
    config = config.resolve()
    if not config.parent.is_dir():
        return 1
    if hidden and engine.background_stop_path(config).exists():
        return 0
    handlers = [logging.StreamHandler()] if sys.stderr is not None else []
    try:
        handlers.append(RotatingFileHandler(config.with_name("mailbot.log"), maxBytes=1_048_576,
                                            backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CloudianSupportTranslate.Desktop")
    root = ctk.CTk()
    root.withdraw()
    # Initialize CTk while hidden so its first mainloop does not re-hide the configured window.
    root.update()
    try:
        lock = engine.execution_lock(config)
    except (OSError, RuntimeError) as error:
        if not hidden:
            messagebox.showinfo(APP_NAME, "이미 실행 중이면 Windows 알림 영역에서 Cloudian 아이콘을 열어주세요.", parent=root)
        logging.info("GUI 실행 잠금을 얻지 못했습니다 (%s).", type(error).__name__)
        root.destroy()
        return 0
    with lock:
        logging.info("%s 시작 · v%s", APP_NAME, engine.APP_VERSION)
        window = Window(root, engine, config, hidden=hidden)
        if not hidden or not window.has_token:
            root.deiconify()
        try:
            root.mainloop()
        finally:
            window.worker.request("exit")
            window.worker.thread.join()
            window.worker.telegram_thread.join()
            window.worker.reply_thread.join()
            window.tray.stop()
        logging.info("%s 종료", APP_NAME)
    return 0

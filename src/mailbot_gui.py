import imaplib
import ctypes
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from contextlib import closing
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter import font as tkfont

import customtkinter as ctk
import pystray
import mailbot_reply
from PIL import Image


APP_NAME = "Cloudian Support Translate"
COMPANY_NAME_LIMIT = 32
MODEL_REFRESH_MS = 15 * 60 * 1000
TRANSLATION_MODELS = ("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol")
MODEL_LABEL_SEPARATOR = " · "
MODEL_USE_CASES = {
    "gpt-6.2-sol": "일반 메일·업무",
    "gpt-6.2-astra": "복잡한 기술 문의",
    "gpt-6.1-terra": "이미지·화면 분석",
    "gpt-6.1-sol": "일반 업무·코딩",
    "gpt-6-astra": "복잡한 전문 작업",
    "gpt-6-sol": "일반 업무·코딩",
    "gpt-6-luna": "간단한 번역·응답",
    "gpt-5.6-sol": "짧고 빠른 회신",
    "gpt-5.6-terra": "여러 단계의 코딩",
    "gpt-5.6-luna": "짧고 단순한 작업",
}
ACCOUNT_FIELDS = ("imap_user", "imap_host", "imap_port", "mailbox")
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, imaplib.IMAP4.error, sqlite3.Error)


def account_key(account):
    return tuple(account[field] for field in ACCOUNT_FIELDS)


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


def discover_chats(engine, token):
    updates = engine.telegram(
        token, "getUpdates", allowed_updates=["message", "channel_post", "my_chat_member"],
    )
    chats = {}
    for update in updates:
        for field in ("message", "channel_post", "my_chat_member"):
            item = update.get(field) or {}
            chat = item.get("chat")
            if chat and "id" in chat:
                name = chat.get("title") or " ".join(
                    part for part in (chat.get("first_name"), chat.get("last_name")) if part
                )
                chats[chat["id"]] = (name or chat.get("username") or "이름 없는 채팅", chat.get("type", ""))
    return chats


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
        self.thread = threading.Thread(target=self.run, name="mail-worker")
        self.thread.start()

    def request(self, command, value=None):
        if command in {"apply", "pause", "exit"}:
            self.interrupt.set()
        self.commands.put((command, value))

    def run(self):
        try:
            database = self.engine.open_state(self.config)
        except sqlite3.Error:
            logging.error("메일 처리 기록을 열 수 없습니다.")
            self.events.put(("notice", "메일 처리 기록을 열 수 없습니다."))
            self.events.put(("worker-status", "처리 기록 오류"))
            return
        try:
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
        mail_due = reply_due = None
        reply_accounts = {}
        while True:
            deadlines = [deadline for deadline in (mail_due, reply_due) if deadline is not None]
            delay = max(0, min(deadlines) - time.monotonic()) if deadlines else None
            try:
                command, value = self.commands.get(timeout=delay)
            except queue.Empty:
                command = "replies" if reply_due is not None and (mail_due is None or reply_due <= mail_due) else "poll"
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
                reply_due = mail_due
                self.events.put(("worker-status", "일시정지" if paused else "메일 확인 대기"))
                continue
            if command == "pause":
                paused = True
                mail_due = reply_due = None
                self.events.put(("worker-status", "일시정지"))
                continue
            if command == "resume":
                paused = False
                self.interrupt.clear()
                mail_due = time.monotonic() if settings else None
                reply_due = mail_due
                self.events.put(("worker-status", "메일 확인 대기" if settings else "설정 필요"))
                continue
            if command == "replies":
                try:
                    mailbot_reply.check_updates(self.engine, database, settings, reply_accounts, token, codex)
                    reply_due = time.monotonic() + 4
                except NETWORK_ERRORS as error:
                    logging.warning("Telegram 회신 확인 실패 (%s)", type(error).__name__)
                    reply_due = time.monotonic() + 30
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
                                     retranslate=value[1], retranslate_validity=value[2])
                else:
                    self.engine.poll_accounts([
                        (settings | account, passwords[account_key(account)], account_key(account) in first_checks)
                        for account in accounts
                    ], token, codex, database, interrupted=self.interrupt.is_set)
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


THEME_LABELS = {"system": "시스템 설정", "dark": "다크 모드", "light": "라이트 모드"}
BODY_FONT = ("Malgun Gothic", 12)
CHOICE_HOVER = ("#dbeafe", "#355170")
QUOTA_WINDOWS = (("primary", "기본 한도"), ("secondary", "추가 한도"))
QUOTA_NORMAL = ("#285a9a", "#9bbcf2")
QUOTA_LOW = ("#a45110", "#f5b264")
KOREA_TIME = timezone(timedelta(hours=9))
THEME_COLORS = {
    "light": {
        "background": "#f1f2f4",
        "card": "#ffffff",
        "text": "#21252d",
        "muted": "#565f6e",
        "status": "#edf0f2",
        "connected": "#236548",
        "pending": "#515a6a",
        "button": "#e9ecf0",
        "button_border": "#d7dbe1",
        "button_text": "#323a47",
        "hover": "#e8eaef",
        "primary": "#285a9a",
        "primary_hover": "#214b81",
        "divider": "#dce0e5",
        "input_border": "#c8cdd5",
        "table_border": "#e4e6ea",
        "table_header": "#f0f2f4",
        "table_header_text": "#495363",
        "table_selected": "#d9e9fc",
        "table_selected_text": "#163d70",
        "table_hover": "#e8eaef",
        "input": "#ffffff",
        "table_alternate": "#fafafb",
    },
    "dark": {
        "background": "#15171c",
        "card": "#20232a",
        "text": "#eef0f4",
        "muted": "#aab1bf",
        "status": "#292e37",
        "connected": "#a6d6bd",
        "pending": "#c4cfdf",
        "button": "#2a2e37",
        "button_border": "#3b424f",
        "button_text": "#dce1ea",
        "hover": "#333a46",
        "primary": "#3264ab",
        "primary_hover": "#3d70bd",
        "divider": "#353b46",
        "input_border": "#424a58",
        "table_border": "#353b46",
        "table_header": "#282c35",
        "table_header_text": "#c8cfdb",
        "table_selected": "#2a4568",
        "table_selected_text": "#eef5ff",
        "table_hover": "#303641",
        "input": "#191c22",
        "table_alternate": "#242830",
    },
}


def theme_color(role):
    return THEME_COLORS["light"][role], THEME_COLORS["dark"][role]


def windows_appearance(*, taskbar=False):
    setting = "SystemUsesLightTheme" if taskbar else "AppsUseLightTheme"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, setting)[0] else "dark"
    except OSError:
        return "light"


class FieldComboBox(ctk.CTkComboBox):
    def __init__(self, parent, *, open_choices, readonly=False, **kwargs):
        self.open_choices = open_choices
        self.enabled_state = "readonly" if readonly else "normal"
        super().__init__(parent, state=self.enabled_state, **kwargs)
        self.bind("<Button-1>", self.show_choices)
        self.bind("<Alt-Down>", self.show_choices)
        self.bind("<F4>", self.show_choices)
        for tag in ("right_parts", "dropdown_arrow"):
            self._canvas.tag_unbind(tag, "<Button-1>")
        self._canvas.bind("<Button-1>", self.show_choices)
        self.update_choice_cursor()

    def configure(self, require_redraw=False, **kwargs):
        super().configure(require_redraw=require_redraw, **kwargs)
        if "state" in kwargs or "values" in kwargs:
            self.update_choice_cursor()

    def _on_leave(self, event=0):
        super()._on_leave(event)
        self.update_choice_cursor()

    def update_choice_cursor(self):
        state = self.cget("state")
        clickable = state != "disabled" and bool(self.cget("values"))
        cursor = "hand2" if clickable else "arrow"
        self._canvas.configure(cursor=cursor)
        self._entry.configure(cursor="xterm" if not clickable and state == "normal" else cursor)

    def show_choices(self, _event=None):
        if self.cget("state") == "disabled":
            return "break"
        values = self.cget("values")
        if values:
            self.open_choices(self, values, self.get(), self.set, restore_focus=self.focus_set)
            return "break"


class Window:
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
        root.title(self.display_name.get())
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

    def theme_choice(self):
        return next((key for key, label in THEME_LABELS.items() if label == self.appearance.get()), "system")

    def update_appearance(self, _choice=None):
        theme = self.theme_choice()
        if theme == "system":
            theme = windows_appearance()
        if getattr(self, "active_theme", None) == theme:
            return
        self.active_theme = theme
        colors = THEME_COLORS[theme]
        ctk.set_appearance_mode(theme)
        style = ttk.Style(self.root)
        style.configure("TSpinbox", fieldbackground=colors["input"], foreground=colors["text"],
                        insertcolor=colors["text"], arrowcolor=colors["text"], bordercolor=colors["input_border"],
                        background=colors["button"], lightcolor=colors["input"], darkcolor=colors["input"],
                        font=("Malgun Gothic", 10), padding=(8, 8))
        style.map("TSpinbox", bordercolor=[("focus", colors["primary"])])
        style.configure("Cell.TEntry", fieldbackground=colors["input"], foreground=colors["text"],
                        insertcolor=colors["text"], bordercolor=colors["primary"])
        row_height = max(27, self.row_font.metrics("linespace") + 8)
        style.configure("Ledger.Treeview", font=self.row_font, rowheight=row_height, background=colors["card"],
                        fieldbackground=colors["card"], foreground=colors["text"], borderwidth=0, relief="flat",
                        bordercolor=colors["table_border"], lightcolor=colors["table_border"], darkcolor=colors["table_border"])
        style.configure("Ledger.Treeview.Heading", font=self.heading_font, padding=(8, 6), relief="flat",
                        background=colors["table_header"], foreground=colors["table_header_text"], bordercolor=colors["table_border"],
                        lightcolor=colors["table_header"], darkcolor=colors["table_header"])
        style.map("Ledger.Treeview", background=[("selected", colors["table_selected"])],
                  foreground=[("selected", colors["table_selected_text"])])
        style.map("Ledger.Treeview.Heading", background=[("active", colors["table_hover"])])
        if hasattr(self, "viewport"):
            self.viewport.configure(background=colors["background"])
            self.table.tag_configure("alternate", background=colors["table_alternate"])
        windows = [self.root]
        while windows:
            for child in windows.pop().winfo_children():
                if isinstance(child, tk.Listbox):
                    child.configure(background=colors["input"], foreground=colors["text"],
                                    selectbackground=colors["table_selected"], selectforeground=colors["table_selected_text"],
                                    highlightbackground=colors["divider"])
                windows.append(child)

    def watch_appearance(self):
        if not self.closing:
            self.update_appearance()
            tray_theme = windows_appearance(taskbar=True)
            if tray_theme != self.tray_theme:
                self.tray.icon = self.logos[tray_theme]
                self.tray_theme = tray_theme
            self.root.after(2_000, self.watch_appearance)

    def frame(self, parent, *, surface=None, corner_radius=0, **kwargs):
        return ctk.CTkFrame(parent, fg_color=theme_color(surface) if surface else "transparent",
                            corner_radius=corner_radius, **kwargs)

    def divider(self, parent):
        return self.frame(parent, surface="divider", height=1, corner_radius=0)

    def label(self, parent, *, muted=False, heading=False, title=False, **kwargs):
        font = ("Malgun Gothic", 18 if title else 14, "bold") if heading or title else BODY_FONT
        kwargs.setdefault("text_color", theme_color("muted" if muted else "text"))
        return ctk.CTkLabel(parent, font=font, anchor="w", justify="left", height=22, **kwargs)

    def button(self, parent, *, primary=False, width=None, **kwargs):
        return ctk.CTkButton(
            parent, width=width if width is not None else (108 if primary else 52), height=32, corner_radius=8,
            font=("Malgun Gothic", 12, "bold") if primary else BODY_FONT,
            fg_color=theme_color("primary" if primary else "button"),
            hover_color=theme_color("primary_hover" if primary else "hover"),
            border_width=0 if primary else 1, border_color=theme_color("button_border"),
            text_color="white" if primary else theme_color("button_text"),
            text_color_disabled=("#dbe3ec", "#c4cfdf") if primary else theme_color("muted"), **kwargs,
        )

    def entry(self, parent, **kwargs):
        return ctk.CTkEntry(parent, height=32, corner_radius=7, border_width=1, font=BODY_FONT,
                            fg_color=theme_color("input"), border_color=theme_color("input_border"),
                            text_color=theme_color("text"), **kwargs)

    def dropdown_colors(self):
        return {
            "font": BODY_FONT,
            "dropdown_font": BODY_FONT,
            "text_color": theme_color("text"),
            "button_color": theme_color("button"),
            "button_hover_color": theme_color("hover"),
            "dropdown_fg_color": theme_color("card"),
            "dropdown_hover_color": theme_color("table_selected"),
            "dropdown_text_color": theme_color("text"),
        }

    def menu(self, parent, *, values, **kwargs):
        return ctk.CTkOptionMenu(parent, values=list(values), height=32, corner_radius=7,
                                 dynamic_resizing=False, fg_color=theme_color("button"),
                                 **self.dropdown_colors(), **kwargs)

    def combo(self, parent, variable, values, *, readonly=False):
        return FieldComboBox(parent, open_choices=self.open_choice_menu, readonly=readonly, variable=variable,
                             values=values, height=32, corner_radius=7, border_width=1,
                             fg_color=theme_color("input"), border_color=theme_color("input_border"),
                             **self.dropdown_colors())

    def scrollbar(self, parent, **kwargs):
        return ctk.CTkScrollbar(parent, fg_color="transparent", button_color=theme_color("input_border"),
                                 button_hover_color=theme_color("muted"), **kwargs)

    def switch(self, parent, **kwargs):
        return ctk.CTkSwitch(parent, width=0, font=BODY_FONT, text_color=theme_color("text"),
                              fg_color=theme_color("input_border"), progress_color=theme_color("primary"),
                              button_color=("#ffffff", "#e7eaf0"), **kwargs)

    def open_theme_menu(self, button):
        def apply_theme(label):
            self.appearance.set(label)
            self.update_appearance()

        self.open_choice_menu(button, list(THEME_LABELS.values()), self.appearance.get(), apply_theme,
                              mark_current=True)

    def open_choice_menu(self, button, labels, value, on_select, *, mark_current=False, restore_focus=None):
        colors = THEME_COLORS[self.active_theme]
        hover_color = CHOICE_HOVER[self.active_theme == "dark"]
        popup = tk.Toplevel(self.root)
        popup.withdraw()
        popup.transient(self.root)
        popup.overrideredirect(True)
        scale = ctk.ScalingTracker.get_widget_scaling(button)
        choices = tk.Listbox(popup, height=min(8, len(labels)), font=(BODY_FONT[0], -round(BODY_FONT[1] * scale)),
                             background=colors["card"], foreground=colors["text"],
                             selectbackground=hover_color, selectforeground=colors["table_selected_text"],
                             highlightbackground=colors["input_border"], highlightcolor=colors["input_border"],
                             highlightthickness=1, borderwidth=0, selectborderwidth=0, activestyle="none",
                             exportselection=False)
        if len(labels) > 8:
            scrollbar = self.scrollbar(popup, height=0, command=choices.yview)
            scrollbar.pack(side="right", fill="y")
            choices.configure(yscrollcommand=scrollbar.set)
        choices.pack(side="left", fill="both", expand=True)
        current = labels.index(value) if value in labels else 0
        choices.insert("end", *(
            ((" ✓  " if index == current else "      ") if mark_current else "  ") + label
            for index, label in enumerate(labels)
        ))
        choices.itemconfigure(current, background=colors["table_alternate"])
        choices.activate(current)
        if not mark_current:
            choices.selection_set(current)
            choices.see(current)
        hovered = None

        def highlight(index):
            nonlocal hovered
            if hovered == index:
                return
            if hovered is not None:
                background = colors["table_alternate"] if hovered == current else colors["card"]
                choices.itemconfigure(hovered, background=background)
            hovered = index
            if index is not None:
                choices.selection_clear(0, "end")
                choices.itemconfigure(index, background=hover_color)

        def close():
            popup.destroy()
            if restore_focus:
                restore_focus()
            return "break"

        def select(index):
            close()
            on_select(labels[index])
            return "break"

        def dismiss_outside(event):
            if not (popup.winfo_rootx() <= event.x_root < popup.winfo_rootx() + popup.winfo_width()
                    and popup.winfo_rooty() <= event.y_root < popup.winfo_rooty() + popup.winfo_height()):
                popup.destroy()
                return "break"

        def click_choice(event):
            if 0 <= event.x < choices.winfo_width() and 0 <= event.y < choices.winfo_height():
                return select(choices.nearest(event.y))

        choices.bind("<Motion>", lambda event: highlight(choices.nearest(event.y)))
        choices.bind("<Leave>", lambda _: highlight(None))
        choices.bind("<ButtonRelease-1>", click_choice)
        choices.bind("<Return>", lambda _: select(choices.index("active")))
        choices.bind("<FocusOut>", lambda _: popup.destroy())
        popup.bind("<ButtonPress-1>", dismiss_outside)
        popup.bind("<Escape>", lambda _: close())
        popup.update_idletasks()
        x = button.winfo_rootx()
        gap = round(3 * scale)
        height = choices.winfo_reqheight()
        y = button.winfo_rooty() + button.winfo_height() + gap
        above = button.winfo_rooty() - height - gap
        if y + height > self.root.winfo_rooty() + self.root.winfo_height() and above >= self.root.winfo_rooty():
            y = above
        popup.geometry(f"{button.winfo_width()}x{height}+{x}+{y}")
        popup.deiconify()
        popup.grab_set()
        choices.focus_force()

    def build(self):
        ttk.Style(self.root).theme_use("clam")
        self.root.configure(fg_color=theme_color("background"))
        self.row_font = tkfont.Font(family="Malgun Gothic", size=10)
        self.heading_font = tkfont.Font(family="Malgun Gothic", size=10, weight="bold")
        self.update_appearance()
        top = self.frame(self.root)
        top.pack(fill="x", padx=16, pady=(12, 4))
        top.columnconfigure(1, weight=1)
        self.label(top, text="", image=self.header_logo).grid(row=0, column=0, sticky="w")
        header_font = ctk.CTkFont(family=BODY_FONT[0], size=16, weight="bold")
        header_title = self.label(top, textvariable=self.display_name, title=True)
        header_title.configure(font=header_font)
        header_title.grid(row=0, column=1, sticky="w", padx=(8, 4))
        self.status_label = self.label(top, textvariable=self.status, fg_color=theme_color("status"),
                                       text_color=theme_color("pending"), corner_radius=13, padx=10)
        self.status_label.grid(row=0, column=3, sticky="e")
        theme_text = tk.StringVar(value=f"{self.appearance.get()}  ▾")
        self.appearance.trace_add("write", lambda *_: theme_text.set(f"{self.appearance.get()}  ▾"))
        theme_box = self.button(top, textvariable=theme_text, width=93)
        theme_box.configure(command=lambda: self.open_theme_menu(theme_box), height=28, corner_radius=14,
                            hover_color=CHOICE_HOVER)
        theme_box.grid(row=0, column=2, sticky="e", padx=(4, 8))

        def fit_header(_event=None):
            width = top.winfo_width()
            scale = ctk.ScalingTracker.get_widget_scaling(top)
            required = theme_box.winfo_reqwidth() + self.status_label.winfo_reqwidth()
            required += (header_font.measure(self.display_name.get()) + 54) * scale
            compact = width < required
            header_title.configure(wraplength=max(150, width / scale - 50) if compact else 0)
            header_title.grid_configure(columnspan=3 if compact else 1)
            theme_box.grid_configure(row=1 if compact else 0, column=1 if compact else 2,
                                     sticky="w" if compact else "e", pady=(6, 0) if compact else 0)
            self.status_label.grid_configure(row=1 if compact else 0, column=2 if compact else 3,
                                             columnspan=2 if compact else 1, pady=(6, 0) if compact else 0)

        top.bind("<Configure>", fit_header)
        self.display_name.trace_add("write", lambda *_: self.root.after_idle(fit_header))
        self.build_quota()
        self.divider(self.root).pack(fill="x", padx=16, pady=(6, 0))
        footer = self.frame(self.root)
        footer.pack(side="bottom", fill="x", padx=16, pady=8)
        footer.columnconfigure(0, weight=1)
        notice = self.label(footer, textvariable=self.notice, muted=True, wraplength=320)
        notice.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        buttons = self.frame(footer)
        buttons.grid(row=0, column=1, sticky="e")
        self.button(buttons, text="로그 열기", command=self.open_logs).pack(side="left")
        self.pause_button = self.button(buttons, text="일시정지", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=8)
        self.button(buttons, text="변경 적용", primary=True, command=self.apply).pack(side="left")

        def fit_footer(event):
            scale = ctk.ScalingTracker.get_widget_scaling(footer)
            wrap = max(110, (event.width - buttons.winfo_reqwidth()) / scale - 16)
            if abs(notice.cget("wraplength") - wrap) > 1:
                notice.configure(wraplength=wrap)

        footer.bind("<Configure>", fit_footer)

        body = self.frame(self.root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        viewport = tk.Canvas(body, background=THEME_COLORS[self.active_theme]["background"], highlightthickness=0)
        self.viewport = viewport
        viewport.grid(row=0, column=0, sticky="nsew")
        scrollbar = self.scrollbar(body, orientation="vertical", command=viewport.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        viewport.configure(yscrollcommand=scrollbar.set)
        main = self.frame(viewport, surface="background")
        content = viewport.create_window(0, 0, window=main, anchor="nw")
        main.columnconfigure((0, 1), weight=1, uniform="cards")
        mail, _ = self.card(main, "이메일 계정", 0, 0, colspan=2)
        columns = ("email", "host", "port", "folder", "credential")
        self.table = ttk.Treeview(mail, columns=columns, show="headings", height=1, selectmode="browse",
                                  style="Ledger.Treeview")
        self.table.tag_configure("alternate", background=THEME_COLORS[self.active_theme]["table_alternate"])
        for name, label, width in zip(columns, ("이메일 주소", "IMAP 서버", "TLS 포트", "메일함", "비밀번호"),
                                      (230, 180, 75, 85, 110), strict=True):
            self.table.heading(name, text=label, anchor="w")
            self.table.column(name, width=width, minwidth=55, stretch=False)
        self.table_scroll = self.scrollbar(mail, orientation="vertical", height=0, command=self.table.yview)
        self.table.configure(yscrollcommand=self.table_scroll.set)
        self.table.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        self.table_xscroll = self.scrollbar(mail, orientation="horizontal", width=0, command=self.table.xview)
        self.table.configure(xscrollcommand=self.update_horizontal_scroll)
        self.table.bind("<Double-Button-1>", self.table_double_click)
        self.table.bind("<F2>", lambda _: self.edit_account())
        self.table.bind("<Configure>", lambda _: self.fit_table_width())
        row = self.frame(mail)
        row.grid(row=0, column=0, columnspan=2, sticky="e")
        self.button(row, text="계정 추가", command=lambda: self.edit_account(add=True)).pack(side="left")
        self.button(row, text="수정", command=self.edit_account).pack(side="left", padx=8)
        self.button(row, text="제거", command=self.remove_account).pack(side="left")
        self.button(row, text="UID 조회·재번역", command=self.retranslate).pack(side="left", padx=(8, 0))
        translation, translation_shell = self.card(main, "Codex 번역", 1, 0)
        self.model_refresh = self.button(translation, text="모델 새로고침",
                                        command=lambda: self.load_models(manual=True))
        self.model_refresh.grid(row=0, column=2, columnspan=2, sticky="e")
        self.model_picker = self.combo(translation, self.model, [self.model.get()] if self.model.get() else [])
        self.field(translation, "수신 메일 모델", self.model_picker, 1, span=3, pady=2)
        self.reply_model_picker = self.combo(translation, self.reply_model, [self.reply_model.get()], readonly=True)
        self.field(translation, "회신 메일 모델", self.reply_model_picker, 2, span=3, pady=2)
        language_picker = self.combo(translation, self.language, ["자동 (한↔영)", "한국어", "영어", "일본어"])
        language_picker.configure(width=108)
        self.field(translation, "번역 언어", language_picker, 3, pady=2)
        self.label(translation, text="확인 간격(초)").grid(row=3, column=2, sticky="w", padx=(10, 6), pady=2)
        ttk.Spinbox(translation, from_=1, to=86400, textvariable=self.interval, width=5).grid(
            row=3, column=3, sticky="ew", pady=2,
        )
        prompts = self.frame(translation)
        prompts.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(4, 2))
        self.button(prompts, text="프롬프트 관리", command=self.edit_prompts).pack(side="left")
        self.button(prompts, text="회신 프롬프트", command=lambda: self.edit_prompts(reply=True)).pack(side="left", padx=8)
        self.button(prompts, text="회신 서명", command=self.edit_reply_signature).pack(side="left")
        self.codex_account = tk.StringVar(value="계정 확인 중")
        identity = self.frame(translation)
        identity.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(4, 2))
        identity.columnconfigure(0, weight=1)
        account_label = self.label(identity, textvariable=self.codex_account, muted=True, wraplength=210)
        account_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.label(identity, textvariable=self.prompt_status, muted=True).grid(row=1, column=0, sticky="w")
        self.codex_connect = self.button(identity, text="계정 연결", command=self.connect_codex)
        self.codex_connect.grid(row=0, column=1, rowspan=2, sticky="e")
        telegram, telegram_shell = self.card(main, "Telegram", 1, 1)
        label_width = round(82 * ctk.ScalingTracker.get_widget_scaling(telegram))
        telegram.columnconfigure(0, minsize=label_width)
        self.field(telegram, "봇 토큰", self.entry(telegram, textvariable=self.token, show="•"), 1)
        self.token_hint = self.label(telegram, muted=True)
        self.token_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        destination = self.frame(telegram)
        destination.grid(row=3, column=0, columnspan=2, sticky="ew")
        destination.columnconfigure(0, weight=1)
        modes = self.frame(destination)
        modes.grid(row=0, column=0, sticky="w", pady=(4, 0))
        for value, title in (("private", "개인 채팅"), ("group", "그룹 채팅")):
            ctk.CTkRadioButton(modes, text=title, value=value, variable=self.chat_mode,
                               width=0, font=BODY_FONT, text_color=theme_color("text"),
                               fg_color=theme_color("primary"), border_color=theme_color("input_border"),
                            command=self.update_chat_mode).pack(side="left", padx=(0, 12))
        self.private_chat_fields = self.frame(destination)
        self.group_chat_fields = self.frame(destination)
        for panel in (self.private_chat_fields, self.group_chat_fields):
            panel.columnconfigure(0, minsize=label_width)
            panel.columnconfigure(1, weight=1)
        self.field(self.private_chat_fields, "개인 Chat ID",
                   self.entry(self.private_chat_fields, textvariable=self.private_chat_id), 0)
        self.field(self.group_chat_fields, "그룹 Chat ID",
                   self.entry(self.group_chat_fields, textvariable=self.group_chat_id), 0)
        self.group_users_button = self.button(self.group_chat_fields, command=self.choose_reply_users)
        self.group_users_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.label(self.group_chat_fields, text="팀원이 그룹에서 /myid@봇아이디를 보내면 선택 목록에 나타납니다.",
                  muted=True, wraplength=290).grid(row=2, column=0, columnspan=2, sticky="w")
        self.group_chat_id.trace_add("write", lambda *_: self.update_chat_mode())
        actions = self.frame(telegram)
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.find_chat_button = self.button(actions, command=self.find_chats)
        self.find_chat_button.pack(side="left")
        self.button(actions, text="연결 테스트", command=self.test_chat).pack(side="left", padx=8)
        self.update_chat_mode()
        toggles = self.frame(main)
        toggles.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 6))
        self.switch(toggles, text="창을 닫아도 트레이에서 실행", variable=self.keep_running).pack(side="left")
        self.switch(toggles, text="Windows 로그인 시 자동 시작", variable=self.autostart).pack(side="left", padx=12)
        rename = self.button(toggles, text="회사명 변경", command=self.edit_company_name)
        rename.configure(height=26)
        rename.pack(side="right")
        compact = None

        def resize(event):
            nonlocal compact
            viewport.itemconfigure(content, width=event.width)
            narrow = event.width < 812 * ctk.ScalingTracker.get_widget_scaling(main)
            if narrow != compact:
                compact = narrow
                translation_shell.grid(columnspan=2 if narrow else 1, padx=16 if narrow else (16, 6))
                telegram_shell.grid(row=2 if narrow else 1, column=0 if narrow else 1,
                                    columnspan=2 if narrow else 1, padx=16 if narrow else (6, 16))

        def wrap_details(event):
            scale = ctk.ScalingTracker.get_widget_scaling(translation)
            account_label.configure(wraplength=max(120, (event.width - self.codex_connect.winfo_reqwidth()) / scale - 16))

        def scroll_body(event):
            if isinstance(event.widget, (ttk.Treeview, tk.Text)):
                return
            if event.widget is viewport or str(event.widget).startswith(f"{viewport}."):
                viewport.yview_scroll(-int(event.delta / 120), "units")
                return "break"

        viewport.bind("<Configure>", resize)
        main.bind("<Configure>", lambda _: viewport.configure(scrollregion=viewport.bbox(content)))
        translation.bind("<Configure>", wrap_details)
        self.root.bind("<MouseWheel>", scroll_body, add="+")
        self.refresh_accounts()
        self.refresh_token_hint()
        self.refresh_prompt_status()

    def build_quota(self):
        band = self.frame(self.root, surface="card", corner_radius=10,
                          border_color=theme_color("divider"), border_width=1)
        band.pack(fill="x", padx=16, pady=(2, 0))
        band.columnconfigure((1, 2), weight=1, uniform="quota")

        summary = self.frame(band)
        summary.grid(row=0, column=0, sticky="nsw", padx=(12, 10), pady=8)
        self.label(summary, text="Codex 사용 한도", heading=True).pack(anchor="w")
        self.quota_credits = self.label(summary, text="크레딧 —", muted=True)
        self.quota_credits.pack(anchor="w")

        self.quota_meters = {}
        for column, (key, fallback) in enumerate(QUOTA_WINDOWS, start=1):
            pane = self.frame(band)
            pane.grid(row=0, column=column, sticky="nsew", padx=(0, 12), pady=8)
            pane.columnconfigure(0, weight=1)
            title = self.label(pane, text=fallback)
            title.grid(row=0, column=0, sticky="w")
            remaining = self.label(pane, text="—", muted=True)
            remaining.grid(row=0, column=1, sticky="e", padx=(4, 0))
            meter = ctk.CTkProgressBar(pane, height=5, width=1, corner_radius=3,
                                       fg_color=theme_color("input_border"), progress_color=QUOTA_NORMAL)
            meter.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 1))
            meter.set(0)
            reset = self.label(pane, text="한도 확인 중", muted=True)
            reset.grid(row=2, column=0, columnspan=2, sticky="w")
            self.quota_meters[key] = title, remaining, meter, reset

        refresh = self.frame(band)
        refresh.grid(row=0, column=3, padx=(4, 12), pady=8)
        self.quota_refresh = self.button(refresh, text="↻  새로고침",
                                         command=lambda: self.load_models(manual=True, limits_only=True))
        self.quota_refresh.pack()
        self.quota_refreshed_at = self.label(refresh, text="갱신 기록 없음", muted=True)
        self.quota_refreshed_at.pack(pady=(1, 0))

    def card(self, parent, title, row, column, *, colspan=1):
        shell = self.frame(parent, surface="card", corner_radius=12,
                           border_width=1, border_color=theme_color("divider"))
        shell.grid(row=row, column=column, columnspan=colspan, sticky="new",
                   padx=(16, 6) if row and not column else (6, 16) if column else 16,
                   pady=(8, 9) if not row else (0, 8))
        panel = self.frame(shell)
        panel.pack(fill="both", expand=True, padx=14, pady=9)
        panel.columnconfigure(0, weight=0 if row else 1)
        panel.columnconfigure(1, weight=1 if row else 0)
        self.label(panel, text=title, heading=True).grid(row=0, column=0, columnspan=2, sticky="w")
        return panel, shell

    def field(self, parent, caption, widget, row, *, span=1, pady=4):
        self.label(parent, text=caption).grid(row=row, column=0, sticky="w", pady=pady, padx=(0, 12))
        widget.grid(row=row, column=1, columnspan=span, sticky="ew", pady=pady)

    def refresh_accounts(self):
        self.table.delete(*self.table.get_children())
        for index, account in enumerate(self.accounts):
            credential = "변경 대기" if account.get("new_password") else (
                "저장됨" if account.get("protected_password") else "입력 필요"
            )
            self.table.insert("", "end", iid=str(index), values=(*account_key(account), credential),
                              tags=("alternate",) if index % 2 else ())
        self.table.configure(height=max(1, min(5, len(self.accounts))))
        if len(self.accounts) > 5:
            self.table_scroll.grid(row=1, column=1, sticky="ns", pady=(8, 4))
        else:
            self.table_scroll.grid_forget()
        self.fit_table_width()

    def update_horizontal_scroll(self, first, last):
        visible = float(first) > 0 or float(last) < 0.995
        if visible and not self.table_xscroll.winfo_manager():
            self.table_xscroll.grid(row=2, column=0, sticky="ew")
        elif not visible and self.table_xscroll.winfo_manager():
            self.table_xscroll.grid_forget()
        if visible and tuple(map(float, (first, last))) != self.table_xscroll.get():
            self.table_xscroll.set(first, last)

    def measured_column_width(self, column):
        widths = [self.heading_font.measure(self.table.heading(column, "text"))]
        widths.extend(self.row_font.measure(str(self.table.set(row, column))) for row in self.table.get_children())
        minimum = {"email": 190, "host": 145, "port": 66, "folder": 73, "credential": 86}
        return max(minimum[column], min(540, max(widths) + 24))

    def fit_table_width(self, fitted=None):
        widths = {column: self.measured_column_width(column) for column in self.table["columns"]}
        spare = max(0, self.table.winfo_width() - sum(widths.values()) - 6)
        expand = [column for column in ("email", "host") if column != fitted]
        for column in expand:
            widths[column] += spare // len(expand)
        for column, width in widths.items():
            if self.table.column(column, "width") != width:
                self.table.column(column, width=width)

    def table_double_click(self, event):
        if self.busy or self.closing:
            return "break"
        region = self.table.identify_region(event.x, event.y)
        identifier = self.table.identify_column(event.x)
        if not identifier or identifier == "#0":
            return "break"
        column_index = int(identifier[1:]) - 1
        column = self.table["columns"][column_index]
        if region in {"heading", "separator"}:
            self.fit_table_width(fitted=column)
            return "break"
        row = self.table.identify_row(event.y)
        if region == "cell" and row:
            self.table.selection_set(row)
            if column_index < len(ACCOUNT_FIELDS):
                self.edit_cell(row, identifier, ACCOUNT_FIELDS[column_index])
            else:
                self.edit_account()
        return "break"

    def edit_cell(self, row, column, field):
        if self.inline_editor:
            previous = self.inline_editor
            self.inline_editor = None
            previous.destroy()
        bounds = self.table.bbox(row, column)
        if not bounds:
            return
        x, y, width, height = bounds
        index = int(row)
        entry = ttk.Entry(self.table, style="Cell.TEntry")
        self.inline_editor = entry
        entry.insert(0, str(self.accounts[index][field]))
        entry.place(x=x, y=y, width=width, height=height)
        entry.after_idle(entry.focus_set)
        entry.selection_range(0, "end")

        def close(commit):
            if self.inline_editor is not entry:
                return "break"
            value = entry.get().strip()
            self.inline_editor = None
            entry.destroy()
            if not commit:
                return "break"
            current = self.accounts[index]
            try:
                if not value or any(char in value for char in "\r\n\x00"):
                    raise ValueError("셀에 올바른 값을 입력하세요.")
                if field == "imap_port":
                    value = int(value)
                    if not 1 <= value <= 65535:
                        raise ValueError("TLS 포트는 1~65535 사이로 입력하세요.")
                if value == current[field]:
                    return "break"
                updated = current | {field: value}
                if any(account_key(other) == account_key(updated) for i, other in enumerate(self.accounts) if i != index):
                    raise ValueError("이미 등록된 이메일 계정과 메일함입니다.")
            except ValueError as error:
                self.notice.set(str(error))
                return "break"
            new_login = field in ACCOUNT_FIELDS[:3]
            if new_login:
                updated.pop("protected_password", None)
                updated.pop("new_password", None)
            self.accounts[index] = updated
            self.refresh_accounts()
            self.table.selection_set(row)
            self.notice.set("비밀번호 셀을 더블클릭해 새 로그인 정보를 입력하세요." if new_login
                            else "변경 적용을 누르면 새 메일함을 확인합니다.")
            return "break"

        entry.bind("<Return>", lambda _: close(True))
        entry.bind("<Escape>", lambda _: close(False))
        entry.bind("<FocusOut>", lambda _: close(True))

    def refresh_prompt_status(self):
        configured = sum(bool(account.get("translation_prompt")) for account in self.accounts)
        if self.global_prompt or configured:
            text = f"공통 {'사용 중' if self.global_prompt else '없음'} · 계정별 {configured}개"
        else:
            text = "사용자 프롬프트 없음"
        self.prompt_status.set(text)

    def edit_prompts(self, *, reply=False):
        if self.busy or self.closing:
            return
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("번역 프롬프트 관리")
        dialog.transient(self.root)
        dialog.geometry("760x480")
        dialog.minsize(540, 370)
        dialog.grab_set()
        panel = self.frame(dialog)
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        drafts = [self.global_prompt, self.reply_prompt, *(account.get("translation_prompt", "") for account in self.accounts)]
        names = ["모든 계정에 공통으로 적용", "텔레그램 회신용", *(
            f"{index + 1}. {account['imap_user']} · {account['mailbox']}" for index, account in enumerate(self.accounts)
        )]
        current = int(reply)
        picker = self.menu(panel, values=names)
        picker.set(names[current])
        picker.pack(fill="x")
        self.label(panel, text="계정별 프롬프트가 공통 프롬프트에 우선합니다.",
                   muted=True, wraplength=465).pack(anchor="w", pady=(7, 8))

        text = ctk.CTkTextbox(panel, wrap="word", padx=10, pady=8, font=BODY_FONT, undo=True,
                              fg_color=theme_color("input"), text_color=theme_color("text"),
                              border_width=1, border_color=theme_color("input_border"),
                              scrollbar_button_color=theme_color("input_border"),
                              scrollbar_button_hover_color=theme_color("muted"))
        text.pack(fill="both", expand=True)
        text.insert("1.0", drafts[current])

        def switch(choice):
            nonlocal current
            drafts[current] = text.get("1.0", "end-1c")
            current = names.index(choice)
            text.delete("1.0", "end")
            text.insert("1.0", drafts[current])
            text.edit_reset()

        def save():
            drafts[current] = text.get("1.0", "end-1c")
            try:
                prompts = [self.engine.validate_translation_prompt(value) for value in drafts]
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error), parent=dialog)
                return
            self.global_prompt = prompts[0]
            self.reply_prompt = prompts[1]
            for account, value in zip(self.accounts, prompts[2:], strict=True):
                if value:
                    account["translation_prompt"] = value
                else:
                    account.pop("translation_prompt", None)
            self.refresh_prompt_status()
            self.notice.set("프롬프트 편집 완료. 변경 적용을 누르면 다음 메일부터 사용합니다.")
            dialog.destroy()

        picker.configure(command=switch)
        footer = self.frame(panel)
        footer.pack(fill="x", pady=(10, 0))
        self.button(footer, text="취소", command=dialog.destroy).pack(side="left")
        self.button(footer, text="편집 완료", primary=True, command=save).pack(side="right")

    def refresh_token_hint(self):
        self.token_hint.configure(text="저장됨 · 빈칸을 유지하면 기존 토큰 사용" if self.has_token else "BotFather에서 복사한 토큰을 입력하세요.")

    def edit_reply_signature(self):
        if self.busy or self.closing:
            return
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("회신 서명")
        dialog.transient(self.root)
        dialog.geometry("470x340")
        dialog.minsize(420, 315)
        dialog.grab_set()
        panel = self.frame(dialog)
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        footer = self.frame(panel)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        self.label(panel, text="인사말", heading=True).pack(anchor="w")
        greeting = self.entry(panel, placeholder_text="예: Hello,")
        greeting.insert(0, self.reply_format["reply_greeting"])
        greeting.pack(fill="x", pady=(4, 10))
        self.label(panel, text="마무리 서명", heading=True).pack(anchor="w")
        signature = ctk.CTkTextbox(panel, height=100, wrap="word", padx=10, pady=8, font=BODY_FONT, undo=True,
                                   fg_color=theme_color("input"), text_color=theme_color("text"),
                                   border_width=1, border_color=theme_color("input_border"),
                                   scrollbar_button_color=theme_color("input_border"),
                                   scrollbar_button_hover_color=theme_color("muted"))
        signature.insert("1.0", self.reply_format["reply_signature"])
        signature.pack(fill="both", expand=True, pady=(4, 6))
        tip = self.label(panel, text="예: Best regards, 다음 줄에 회사명이나 담당자 이름을 입력하세요.",
                         muted=True, wraplength=375)
        tip.pack(anchor="w")
        self.label(panel, text="비워 둔 항목은 회신에 추가되지 않습니다.", muted=True).pack(anchor="w")

        def save():
            try:
                self.reply_format = mailbot_reply.reply_format_options({
                    "reply_greeting": greeting.get(),
                    "reply_signature": signature.get("1.0", "end-1c"),
                })
            except ValueError as error:
                tip.configure(text=str(error), text_color=("#9b3525", "#f3a094"))
                return
            self.notice.set("회신 서명을 편집했습니다. 변경 적용을 누르면 새 회신에 사용합니다.")
            dialog.destroy()

        self.button(footer, text="취소", command=dialog.destroy).pack(side="left")
        self.button(footer, text="편집 완료", primary=True, command=save).pack(side="right")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        greeting.focus_set()

    def save_company_name(self, value):
        company_name = normalize_company_name(value)
        self.engine.save_settings(self.ui_config, {"company_name": company_name})
        self.company_name = company_name
        name = app_display_name(company_name)
        self.display_name.set(name)
        self.root.title(name)
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

    def selected_index(self):
        selection = self.table.selection()
        return int(selection[0]) if selection else None

    def edit_account(self, add=False):
        if self.busy:
            return
        index = None if add else self.selected_index()
        if not add and index is None:
            self.notice.set("수정할 이메일 계정을 선택하세요.")
            return
        initial = self.accounts[index] if index is not None else {"imap_port": 993, "mailbox": "INBOX"}
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("이메일 계정 추가" if add else "이메일 계정 수정")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        content = self.frame(dialog)
        content.pack(fill="both", expand=True, padx=16, pady=16)
        labels = ("이메일 주소", "IMAP 서버", "IMAP 포트", "메일함", "IMAP 비밀번호", "SMTP 서버 (선택)", "SMTP 포트")
        fields = (*ACCOUNT_FIELDS, "new_password", "smtp_host", "smtp_port")
        port_defaults = {"imap_port": 993, "smtp_port": 465}
        inputs = {}
        for row, (label, field) in enumerate(zip(labels, fields, strict=True)):
            variable = tk.StringVar(value="" if field == "new_password" else str(initial.get(field, port_defaults.get(field, ""))))
            inputs[field] = variable
            self.label(content, text=label, width=120).grid(row=row, column=0, sticky="w", pady=4)
            entry = self.entry(content, textvariable=variable, width=260, show="•" if field == "new_password" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if row == 0:
                entry.focus_set()
        self.label(content, text="기본 SSL/TLS 포트는 IMAP 993, SMTP 465입니다. "
                  "SMTP 회신은 IMAP 로그인 정보를 사용합니다.",
                  muted=True, wraplength=360).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 8))
        if initial.get("protected_password") or initial.get("new_password"):
            self.label(content, text="주소·서버·포트가 같으면 기존 비밀번호를 사용할 수 있습니다.",
                      muted=True, wraplength=360).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 8))

        def submit():
            try:
                draft = {field: inputs[field].get().strip() for field in ACCOUNT_FIELDS}
                draft["imap_port"] = int(draft["imap_port"] or port_defaults["imap_port"])
                if not all(draft.values()) or not 1 <= draft["imap_port"] <= 65535:
                    raise ValueError("이메일 주소, 서버, 포트와 메일함을 확인하세요.")
                if any(char in draft["mailbox"] for char in "\r\n\x00"):
                    raise ValueError("메일함 이름을 확인하세요.")
                smtp_host = inputs["smtp_host"].get().strip()
                if smtp_host:
                    if not re.fullmatch(r"[a-zA-Z0-9.-]+", smtp_host):
                        raise ValueError("SMTP 서버 주소를 확인하세요.")
                    smtp_port = int(inputs["smtp_port"].get().strip() or port_defaults["smtp_port"])
                    if not 1 <= smtp_port <= 65535:
                        raise ValueError("SMTP 포트는 1~65535 사이로 입력하세요.")
                    draft["smtp_host"] = smtp_host
                    draft["smtp_port"] = smtp_port
                if any(account_key(saved) == account_key(draft) for i, saved in enumerate(self.accounts) if i != index):
                    raise ValueError("이미 등록된 이메일 계정과 메일함입니다.")
                same_login = all(initial.get(field) == draft[field] for field in ACCOUNT_FIELDS[:3])
                password = inputs["new_password"].get()
                if password:
                    draft["new_password"] = password
                elif same_login:
                    for name in ("new_password", "protected_password"):
                        if initial.get(name):
                            draft[name] = initial[name]
                if not (draft.get("new_password") or draft.get("protected_password")):
                    raise ValueError("IMAP 비밀번호를 입력하세요.")
                if initial.get("translation_prompt"):
                    draft["translation_prompt"] = initial["translation_prompt"]
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error), parent=dialog)
                return
            if index is None:
                self.accounts.append(draft)
            else:
                self.accounts[index] = draft
            self.refresh_accounts()
            self.table.selection_set(str(index if index is not None else len(self.accounts) - 1))
            self.notice.set("계정 변경을 준비했습니다. 변경 적용을 누르면 연결을 확인합니다.")
            dialog.destroy()

        self.button(content, text="확인", primary=True, command=submit).grid(row=9, column=1, sticky="e")
        self.button(content, text="취소", command=dialog.destroy).grid(row=9, column=0, sticky="w")
        dialog.bind("<Return>", lambda _: submit())

    def remove_account(self):
        if self.busy:
            return
        index = self.selected_index()
        if index is None:
            self.notice.set("제거할 이메일 계정을 선택하세요.")
            return
        if self.running and len(self.accounts) == 1:
            self.notice.set("메일 확인을 유지하려면 다른 계정을 먼저 추가하세요.")
            return
        account = self.accounts[index]
        if messagebox.askyesno(APP_NAME, f"{account['imap_user']} 계정을 목록에서 제거할까요?", parent=self.root):
            del self.accounts[index]
            self.refresh_accounts()
            self.refresh_prompt_status()
            self.notice.set("변경 적용을 누르면 이 계정의 메일 확인을 중지합니다.")

    def current_token(self):
        token = self.token.get().strip()
        if not token and self.has_token:
            try:
                token = self.engine.unlock_password(self.settings["background_token"])
            except (OSError, ValueError, TypeError):
                raise RuntimeError("저장된 Telegram 토큰을 열 수 없습니다. 토큰을 다시 입력하세요.") from None
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("BotFather에서 발급한 Telegram 봇 토큰을 입력하세요.")
        return token

    def current_chat_id(self):
        group = self.chat_mode.get() == "group"
        raw = (self.group_chat_id if group else self.private_chat_id).get().strip()
        if not re.fullmatch(r"-?[0-9]+", raw) or not int(raw):
            raise ValueError("Telegram Chat ID를 입력하거나 채팅 ID 찾기를 이용하세요.")
        if group != (int(raw) < 0):
            raise ValueError("그룹 Chat ID는 음수, 개인 Chat ID는 양수입니다.")
        return int(raw)

    def update_chat_mode(self):
        if self.chat_mode.get() == "group":
            self.private_chat_fields.grid_forget()
            self.group_chat_fields.grid(row=1, column=0, sticky="ew")
            self.find_chat_button.configure(text="그룹 찾기")
        else:
            self.group_chat_fields.grid_forget()
            self.private_chat_fields.grid(row=1, column=0, sticky="ew")
            self.find_chat_button.configure(text="개인 채팅 찾기")
        selected = self.reply_users.get() if self.reply_users_group == self.group_chat_id.get().strip() else ""
        count = len(mailbot_reply.parse_reply_users(selected))
        self.group_users_button.configure(text=f"회신 허용 사용자 선택 ({count}명)")

    def choose_reply_users(self):
        if self.busy or self.closing:
            return
        try:
            chat = self.current_chat_id()
            scope = mailbot_reply.scope_for(self.current_token())
        except (ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        group_id = self.group_chat_id.get().strip()
        configured = set(mailbot_reply.parse_reply_users(
            self.reply_users.get() if self.reply_users_group == group_id else ""))
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("그룹 회신 허용 사용자")
        dialog.transient(self.root)
        dialog.geometry("490x350")
        dialog.grab_set()
        panel = self.frame(dialog)
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        self.label(panel, text="회신할 팀원을 클릭해 선택하세요. /myid를 보낸 뒤 목록을 새로고침할 수 있습니다.",
                  wraplength=430, muted=True).pack(anchor="w", pady=(0, 8))
        colors = THEME_COLORS[self.active_theme]
        listing = self.frame(panel)
        listing.pack(fill="both", expand=True)
        users = tk.Listbox(listing, selectmode="multiple", exportselection=False, height=8,
                           font=("Malgun Gothic", 9), relief="flat", highlightthickness=1,
                           background=colors["input"], foreground=colors["text"],
                           selectbackground=colors["table_selected"], selectforeground=colors["table_selected_text"],
                           highlightbackground=colors["divider"])
        self.scrollable_list(listing, users)
        ordered = []
        manual = tk.StringVar()
        self.label(panel, text="목록에 없는 사용자 ID 추가", muted=True).pack(anchor="w", pady=(8, 2))
        self.entry(panel, textvariable=manual).pack(fill="x")

        def selected_ids():
            return {ordered[index] for index in users.curselection()}

        def reload(selected):
            try:
                uri = self.config.with_suffix(".sqlite3").resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=1)) as database:
                    known = dict(database.execute(
                        "SELECT author, name FROM reply_group_members WHERE scope=? AND chat=?",
                        (scope, chat),
                    ))
            except sqlite3.Error:
                known = {}
            ordered[:] = sorted(known.keys() | selected | configured,
                                key=lambda user: (known.get(user, "").casefold(), user))
            users.delete(0, "end")
            for index, user in enumerate(ordered):
                users.insert("end", f"{known.get(user) or 'Telegram 사용자'}  ·  {user}")
                if user in selected:
                    users.selection_set(index)

        def save():
            try:
                allowed = selected_ids() | set(mailbot_reply.parse_reply_users(manual.get()))
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error), parent=dialog)
                return
            self.reply_users_group = group_id
            self.reply_users.set(", ".join(map(str, sorted(allowed))))
            self.update_chat_mode()
            self.notice.set("회신 허용 사용자를 선택했습니다. 변경 적용을 누르면 반영됩니다.")
            dialog.destroy()

        footer = self.frame(panel)
        footer.pack(fill="x", pady=(8, 0))
        self.button(footer, text="목록 새로고침", command=lambda: reload(selected_ids())).pack(side="left")
        self.button(footer, text="선택 완료", primary=True, command=save).pack(side="right")
        reload(configured)

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

    def scrollable_list(self, panel, widget):
        scrollbar = self.scrollbar(panel, command=widget.yview)
        scrollbar.pack(side="right", fill="y", padx=(4, 0))
        widget.configure(yscrollcommand=scrollbar.set)
        widget.pack(side="left", fill="both", expand=True)

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

    def find_chats(self):
        if self.busy:
            return
        try:
            token = self.current_token()
        except (ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        mode = self.chat_mode.get()
        allowed = {"group", "supergroup"} if mode == "group" else {"private"}
        use_cache = self.running and not self.token.get().strip()

        def fetch():
            if use_cache:
                uri = self.config.with_suffix(".sqlite3").resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=1)) as database:
                    chats = {chat: (name, kind) for chat, name, kind in database.execute(
                        "SELECT chat, name, kind FROM telegram_known_chats WHERE scope=?", (mailbot_reply.scope_for(token),)
                    )}
            else:
                chats = discover_chats(self.engine, token)
            return {chat: details for chat, details in chats.items() if details[1] in allowed}

        self.async_action(fetch, "chats", "Telegram 채팅 목록을 읽는 중입니다.")

    def show_chats(self, chats):
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("Telegram 채팅 ID 찾기")
        dialog.transient(self.root)
        dialog.geometry("500x330")
        dialog.grab_set()
        toolbar = self.frame(dialog)
        toolbar.pack(fill="both", expand=True, padx=12, pady=12)
        self.label(toolbar, text="Telegram 채팅 선택", heading=True).pack(anchor="w")
        self.label(toolbar, text="그룹에 봇을 추가하거나, 원하는 채팅에서 /myid@봇아이디를 보낸 뒤 새로고침하세요.",
                  muted=True, wraplength=455).pack(anchor="w", pady=(4, 8))
        listing = self.frame(toolbar)
        listing.pack(fill="both", expand=True)
        table = ttk.Treeview(listing, columns=("name", "type", "id"), show="headings", height=5, selectmode="browse",
                             style="Ledger.Treeview")
        for field, label, width in (("name", "이름", 180), ("type", "종류", 86), ("id", "Chat ID", 160)):
            table.heading(field, text=label)
            table.column(field, width=width)
        self.scrollable_list(listing, table)
        labels = {"private": "개인", "group": "그룹", "supergroup": "그룹", "channel": "채널"}
        for chat_id, (name, kind) in chats.items():
            table.insert("", "end", iid=str(chat_id), values=(name, labels.get(kind, kind), chat_id))
        if len(chats) == 1:
            table.selection_set(str(next(iter(chats))))
        footer = self.frame(toolbar)
        footer.pack(fill="x", pady=(8, 0))

        def select():
            selected = table.selection()
            if selected:
                kind = chats[int(selected[0])][1]
                if kind not in {"private", "group", "supergroup"}:
                    self.notice.set("개인 또는 그룹 채팅을 선택하세요.")
                    return
                group = kind in {"group", "supergroup"}
                self.chat_mode.set("group" if group else "private")
                (self.group_chat_id if group else self.private_chat_id).set(selected[0])
                self.update_chat_mode()
                self.notice.set("채팅을 선택했습니다. 변경 적용을 누르면 전송 대상이 바뀝니다.")
                dialog.destroy()

        def refresh():
            dialog.destroy()
            self.find_chats()

        self.button(footer, text="새로고침", command=refresh).pack(side="left")
        self.button(footer, text="이 채팅 선택", primary=True, command=select).pack(side="right")
        table.bind("<Double-1>", lambda _: select())
        table.bind("<Return>", lambda _: select())

    def test_chat(self):
        if self.busy:
            return
        try:
            token, chat = self.current_token(), self.current_chat_id()
        except (ValueError, RuntimeError) as error:
            self.notice.set(str(error))
            return
        def test():
            self.engine.telegram(token, "getMe")
            return self.engine.telegram(token, "getChat", chat_id=chat)
        self.async_action(test, "chat-tested", "Telegram 봇 토큰과 채팅을 확인하는 중입니다.")

    def selected_model_id(self):
        return self.model.get().partition(MODEL_LABEL_SEPARATOR)[0].strip() or None

    def refresh_models_periodically(self):
        if not self.closing:
            self.load_models()
            self.root.after(MODEL_REFRESH_MS, self.refresh_models_periodically)

    def connect_codex(self):
        self.codex_connecting = True
        self.codex_connect.configure(state="disabled")
        self.notice.set("브라우저에서 ChatGPT 로그인을 완료하세요.")

        def sign_in():
            try:
                codex = self.engine.find_codex()
                if not codex:
                    message = "Codex CLI를 찾지 못했습니다. Windows Codex 앱을 확인하세요."
                else:
                    subprocess.run(
                        [codex, "login", "-c", self.engine.CHATGPT_AUTH],
                        env=self.engine.codex_environment(),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=300, check=True,
                    )
                    message = None
            except subprocess.TimeoutExpired:
                message = "Codex 로그인 시간이 초과되었습니다. 계정 연결을 다시 눌러 주세요."
            except (OSError, subprocess.CalledProcessError):
                message = "Codex 로그인에 실패했습니다. 브라우저를 확인하고 다시 시도하세요."
            self.events.put(("codex-login", message))

        threading.Thread(target=sign_in, name="codex-login", daemon=True).start()

    def refresh_models_after_login(self):
        if self.closing:
            return
        if self.models_loading:
            self.root.after(200, self.refresh_models_after_login)
        else:
            self.load_models(manual=True)

    def load_models(self, *, manual=False, limits_only=False):
        if self.models_loading or self.closing:
            return
        self.models_loading = True
        self.model_refresh.configure(state="disabled")
        self.quota_refresh.configure(state="disabled")
        if manual:
            self.notice.set("Codex 사용 한도를 확인하고 있습니다." if limits_only
                            else "Codex 계정, 사용 한도, 모델 목록을 확인하고 있습니다.")

        def fetch():
            codex = self.engine.find_codex()
            if not codex:
                self.events.put(("models", ([], None, None, manual, limits_only, "Codex CLI를 찾지 못했습니다. Windows Codex 앱을 확인하세요.")))
                return
            try:
                models, account, limits = self.engine.read_codex_info(codex, include_account=True, include_models=not limits_only)
            except OSError:
                models, account, limits = [], None, None
            self.events.put(("models", (models, account, limits, manual, limits_only, "Codex 정보를 가져오지 못했습니다. 로그인을 확인하세요.")))
        threading.Thread(target=fetch, name="codex-models", daemon=True).start()

    def update_codex_account(self, result, limits):
        account = (result or {}).get("account")
        if result is None:
            self.codex_account.set("Codex 계정을 확인할 수 없습니다.")
        elif not account:
            self.codex_account.set("Codex 로그인 필요")
        else:
            email = account.get("email")
            self.codex_account.set(f"계정  {email}" if email else "Codex 로그인됨 · 이메일 정보 없음")

        limits = (limits or {}) if account else {}
        credits = limits.get("credits") or {}
        balance = "무제한" if credits.get("unlimited") else credits.get("balance")
        self.quota_credits.configure(text=f"크레딧 {balance}" if balance is not None else "크레딧 —")
        received = balance is not None
        today = time.localtime()[:3]
        for key, fallback in QUOTA_WINDOWS:
            title, percent, meter, reset_label = self.quota_meters[key]
            window = limits.get(key) or {}
            used = window.get("usedPercent")
            if used is None:
                title.configure(text=fallback)
                percent.configure(text="—", text_color=theme_color("muted"))
                meter.set(0)
                reset_label.configure(text="한도 정보 없음" if limits else "한도 확인 불가")
                continue
            received = True
            minutes = window.get("windowDurationMins")
            label = fallback
            if minutes:
                if minutes % 1440 == 0:
                    label = f"{minutes // 1440}일 한도"
                elif minutes % 60 == 0:
                    label = f"{minutes // 60}시간 한도"
                else:
                    label = f"{minutes}분 한도"
            remaining = max(0, min(100, 100 - used))
            accent = QUOTA_LOW if remaining < 20 else QUOTA_NORMAL
            title.configure(text=label)
            percent.configure(text=f"{remaining:g}% 남음", text_color=accent)
            meter.configure(progress_color=accent)
            meter.set(remaining / 100)
            reset = window.get("resetsAt")
            reset_text = "초기화 시간 없음"
            if reset:
                local_reset = time.localtime(reset)
                format_str = "%H:%M 초기화" if local_reset[:3] == today else "%m/%d %H:%M 초기화"
                reset_text = time.strftime(format_str, local_reset)
            reset_label.configure(text=reset_text)
        if received:
            self.quota_refreshed_at.configure(text=f"마지막 갱신 {time.strftime('%H:%M')}")
        return received

    def update_model_picker(self, models):
        current = self.selected_model_id()
        reply = self.reply_model.get().partition(MODEL_LABEL_SEPARATOR)[0].strip()
        catalog = {entry["model"]: entry for entry in models}

        def version(model):
            match = re.match(r"gpt-(\d+(?:\.\d+)*)", model)
            return tuple(map(int, match[1].split("."))) if match else ()

        values = sorted(catalog, key=version, reverse=True)
        self.available_models = values
        receive_recommended = next((model for model in TRANSLATION_MODELS if model in values), None)
        if not current and not self.running:
            current = next((entry["model"] for entry in models if entry.get("isDefault")), receive_recommended)

        def label(model):
            if model not in catalog:
                return model
            hint = MODEL_USE_CASES.get(model) or catalog[model].get("description") or "용도 정보 없음"
            return f"{model}{MODEL_LABEL_SEPARATOR}{hint}"

        for picker, variable, selected in (
            (self.model_picker, self.model, current),
            (self.reply_model_picker, self.reply_model, reply),
        ):
            ordered = values.copy()
            if selected and selected not in ordered:
                ordered.append(selected)
            picker.configure(values=[label(model) for model in ordered])
            if selected:
                variable.set(label(selected))

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

    def retranslate(self):
        if self.busy or self.closing:
            return
        index = self.selected_index()
        if index is None and len(self.accounts) == 1:
            index = 0
        if index is None:
            self.notice.set("조회할 이메일 계정을 먼저 선택하세요.")
            return
        key = account_key(self.accounts[index])
        saved = self.engine.imap_accounts(self.settings) if self.settings and not self.config_error else []
        account = next((item for item in saved if account_key(item) == key), None)
        if account is None:
            self.notice.set("계정 변경을 먼저 적용하세요.")
            return
        self.show_uid_directory(account)

    def show_uid_directory(self, account):
        dialog = ctk.CTkToplevel(self.root, fg_color=theme_color("background"))
        dialog.title("메일 UID 조회·재번역")
        dialog.transient(self.root)
        dialog.geometry("780x470")
        dialog.minsize(660, 385)
        dialog.grab_set()
        panel = self.frame(dialog)
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        self.label(panel, text=f"{account['imap_user']}  ·  {account['mailbox']}", heading=True).pack(anchor="w")
        tools = self.frame(panel)
        tools.pack(fill="x", pady=(8, 6))
        views = ("받은편지함", "저장된 UID")
        source = tk.StringVar(master=dialog, value=views[0])
        source_picker = self.menu(tools, variable=source, values=views, width=122)
        source_picker.pack(side="left", padx=(0, 8))
        query = tk.StringVar(master=dialog)
        self.entry(tools, textvariable=query, placeholder_text="불러온 목록에서 제목, 케이스 번호, UID 검색").pack(
            side="left", fill="x", expand=True,
        )
        status = self.label(panel, text="받은편지함을 확인하는 중입니다.", muted=True)
        status.pack(anchor="w", pady=(0, 4))
        listing = self.frame(panel)
        listing.pack(fill="both", expand=True)
        table = ttk.Treeview(listing, columns=("received", "subject", "uid"), show="headings",
                             style="Ledger.Treeview", selectmode="browse")
        for field, label, width in (("received", "수신 시각 (KST)", 152), ("subject", "제목", 395), ("uid", "UID", 82)):
            table.heading(field, text=label, anchor="w")
            table.column(field, width=width, minwidth=width if field != "subject" else 180,
                         stretch=field == "subject")
        self.scrollable_list(listing, table)
        actions = self.frame(panel)
        actions.pack(fill="x", pady=(10, 0))
        rows = {}
        cursor = None
        loading = False

        def draw_table():
            selection = table.selection()
            table.delete(*table.get_children())
            text = query.get().strip().casefold()
            for iid, (uid, _validity, received, subject) in sorted(
                rows.items(), key=lambda item: (item[1][2], item[1][0]), reverse=True,
            ):
                date = datetime.fromtimestamp(received, KOREA_TIME).strftime("%Y-%m-%d %H:%M:%S") if received else "—"
                if text in f"{subject} {uid} {date}".casefold():
                    table.insert("", "end", iid=iid, values=(date, subject, uid))
            if selection and table.exists(selection[0]):
                table.selection_set(selection[0])
            if not loading:
                count = len(table.get_children())
                status.configure(text=f"{count}개 표시 · 불러온 메일 {len(rows)}개" if rows else "표시할 메일이 없습니다.")

        def complete(result, error, reset):
            nonlocal loading, cursor
            if not dialog.winfo_exists():
                return
            loading = False
            source_picker.configure(state="normal")
            refresh.configure(state="normal")
            if error:
                status.configure(text=error)
            else:
                records, cursor = result
                if reset:
                    rows.clear()
                rows.update((f"{validity}:{uid}", (uid, validity, received, subject))
                            for uid, validity, received, subject in records)
                draw_table()
            older.configure(state="normal" if cursor else "disabled")

        def load_page(reset=False):
            nonlocal loading, cursor
            if loading or (not reset and cursor is None):
                return
            loading = True
            view = source.get()
            before = None if reset else cursor
            if reset:
                rows.clear()
                cursor = None
                draw_table()
            status.configure(text=f"{view} 목록을 불러오는 중입니다.")
            for control in (source_picker, refresh, older):
                control.configure(state="disabled")

            def fetch():
                result = None
                error_message = None
                try:
                    if view == views[0]:
                        try:
                            password = self.engine.unlock_password(account["protected_password"])
                        except (KeyError, OSError, TypeError, ValueError):
                            raise RuntimeError("저장된 IMAP 비밀번호를 확인하세요.") from None
                        result = self.engine.inbox_uid_page(account, password, before)
                        with closing(self.engine.open_state(self.config)) as database:
                            self.engine.remember_mail_uids(database, account, result[0])
                    else:
                        with closing(self.engine.open_state(self.config)) as database:
                            result = self.engine.saved_uid_page(database, account, before)
                except NETWORK_ERRORS as error:
                    logging.warning("메일 목록 조회 실패 (%s)", type(error).__name__)
                    error_message = str(error) if isinstance(error, RuntimeError) else "메일 목록을 불러오지 못했습니다."
                self.events.put(("uid-directory", lambda: complete(result, error_message, reset)))

            threading.Thread(target=fetch, name="uid-directory", daemon=True).start()

        def selected():
            selection = table.selection()
            if selection:
                return rows.get(selection[0])
            status.configure(text="목록에서 메일을 선택하세요.")

        def copy_uid():
            row = selected()
            if row:
                dialog.clipboard_clear()
                dialog.clipboard_append(str(row[0]))
                status.configure(text="선택한 메일의 UID를 복사했습니다.")

        def submit(uid, validity=None):
            if self.busy or self.closing or not self.running:
                status.configure(text="먼저 연결 설정을 적용한 뒤 재번역할 수 있습니다.")
                return
            self.worker.request("retranslate", (account_key(account), uid, validity))
            self.notice.set(f"메일 UID {uid} 재번역을 요청했습니다.")
            dialog.destroy()

        def retranslate_selected():
            row = selected()
            if row:
                submit(row[0], row[1])

        def enter_uid():
            uid = simpledialog.askinteger("UID 직접 입력", "다시 번역할 메일의 UID를 입력하세요.",
                                          parent=dialog, minvalue=1, maxvalue=4294967295)
            if uid is not None:
                submit(uid)

        refresh = self.button(actions, text="새로고침", command=lambda: load_page(reset=True))
        refresh.pack(side="left")
        older = self.button(actions, text="이전 메일 더 보기", command=load_page)
        older.pack(side="left", padx=8)
        self.button(actions, text="UID 직접 입력", command=enter_uid).pack(side="left")
        self.button(actions, text="선택 메일 재번역", primary=True, command=retranslate_selected).pack(side="right")
        self.button(actions, text="UID 복사", command=copy_uid).pack(side="right", padx=8)
        query.trace_add("write", lambda *_: draw_table())
        source_picker.configure(command=lambda _: load_page(reset=True))
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        load_page(reset=True)

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
        if self.worker.thread.is_alive():
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
        logging.info("%s 시작", APP_NAME)
        window = Window(root, engine, config, hidden=hidden)
        if not hidden or not window.has_token:
            root.deiconify()
        try:
            root.mainloop()
        finally:
            if window.worker.thread.is_alive():
                window.worker.request("exit")
                window.worker.thread.join()
            window.tray.stop()
        logging.info("%s 종료", APP_NAME)
    return 0

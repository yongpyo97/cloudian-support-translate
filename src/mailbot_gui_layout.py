import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

import customtkinter as ctk

from mailbot_gui_theme import BODY_FONT, CHOICE_HOVER, theme_color, THEME_COLORS


class _MainLayout:
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

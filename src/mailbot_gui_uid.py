import logging
import sqlite3
import threading
import tkinter as tk
from contextlib import closing
from datetime import datetime, timedelta, timezone
from tkinter import simpledialog, ttk

import customtkinter as ctk

from mailbot_gui_worker import account_key, NETWORK_ERRORS
from mailbot_gui_theme import theme_color


KOREA_TIME = timezone(timedelta(hours=9))


class _UidDialog:
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
            try:
                key = self.engine.mailbox_key(self.settings | account)
                with closing(sqlite3.connect(self.config.with_suffix(".sqlite3"), timeout=1)) as database, database:
                    database.execute("INSERT OR IGNORE INTO manual_retranslation VALUES (?, ?, ?, NULL)",
                                     (key, uid, validity))
            except sqlite3.Error:
                status.configure(text="재번역 요청을 저장하지 못했습니다. 잠시 후 다시 시도하세요.")
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

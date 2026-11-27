import re
import sqlite3
import tkinter as tk
from contextlib import closing
from tkinter import messagebox, ttk

import customtkinter as ctk
import mailbot_reply

from mailbot_gui_theme import APP_NAME, theme_color, THEME_COLORS


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


class _TelegramSettings:
    def refresh_token_hint(self):
        self.token_hint.configure(text="저장됨 · 빈칸을 유지하면 기존 토큰 사용" if self.has_token else "BotFather에서 복사한 토큰을 입력하세요.")

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

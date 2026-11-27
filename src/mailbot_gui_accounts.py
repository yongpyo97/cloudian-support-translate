import re
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from mailbot_gui_worker import ACCOUNT_FIELDS, account_key
from mailbot_gui_theme import APP_NAME, theme_color


class _AccountEditor:
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

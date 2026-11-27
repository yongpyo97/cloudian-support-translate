from tkinter import messagebox

import customtkinter as ctk
import mailbot_reply

from mailbot_gui_theme import APP_NAME, BODY_FONT, theme_color


class _PromptEditor:
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

import re
import subprocess
import threading
import time

import customtkinter as ctk

from mailbot_gui_theme import theme_color


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


QUOTA_WINDOWS = (("primary", "기본 한도"), ("secondary", "추가 한도"))


QUOTA_NORMAL = ("#285a9a", "#9bbcf2")


QUOTA_LOW = ("#a45110", "#f5b264")


class _CodexSettings:
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

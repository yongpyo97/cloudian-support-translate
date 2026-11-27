import json
import re
import tempfile
from pathlib import Path

import mailbot_reply

from mailbot_codex import DEFAULT_REPLY_PROMPT


def imap_accounts(settings):
    entries = settings.get("accounts")
    if entries is None:
        entries = [settings]
    if not isinstance(entries, list) or not entries:
        raise ValueError("IMAP 계정을 하나 이상 등록하세요.")
    accounts = []
    for entry in entries:
        host, user = entry["imap_host"], entry["imap_user"]
        port, mailbox = int(entry.get("imap_port", 993)), entry.get("mailbox", "INBOX")
        if not host or not user or not mailbox or not 1 <= port <= 65535:
            raise ValueError("IMAP 계정 정보를 확인하세요.")
        account = dict(imap_host=host, imap_port=port, imap_user=user, mailbox=mailbox)
        smtp_host = entry.get("smtp_host", "")
        if smtp_host:
            if not isinstance(smtp_host, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", smtp_host):
                raise ValueError("SMTP 서버 주소를 확인하세요.")
            smtp_port = int(entry.get("smtp_port", 465))
            if not 1 <= smtp_port <= 65535:
                raise ValueError("SMTP 포트는 1~65535 사이로 입력하세요.")
            account["smtp_host"] = smtp_host
            account["smtp_port"] = smtp_port
        if entry.get("protected_password"):
            account["protected_password"] = entry["protected_password"]
        if entry.get("translation_prompt"):
            account["translation_prompt"] = validate_translation_prompt(entry["translation_prompt"])
        accounts.append(account)
    return accounts


def save_accounts(config, settings, accounts):
    updated = dict(settings)
    for field in ("imap_host", "imap_user", "imap_port", "mailbox", "protected_password"):
        updated.pop(field, None)
    updated["accounts"] = accounts
    save_settings(config, updated)


def save_settings(config, settings):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config.parent, delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(settings, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary.replace(config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


PROMPT_LIMIT = 8000


def validate_translation_prompt(value):
    if not isinstance(value, str) or len(value) > PROMPT_LIMIT:
        raise ValueError(f"추가 프롬프트는 {PROMPT_LIMIT:,}자 이내여야 합니다.")
    return value.strip()


def runtime_options(settings):
    interval = int(settings.get("poll_seconds", 60))
    language = settings.get("language", "한국어")
    model = settings.get("codex_model")
    reply_model = settings.get("reply_codex_model", mailbot_reply.REPLY_MODEL)
    if interval < 1 or not isinstance(language, str) or not language.strip():
        raise ValueError("확인 간격 또는 번역 언어를 확인하세요.")
    if model is not None and (
        not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", model)
    ):
        raise ValueError("Codex 모델 ID를 확인하세요.")
    if not isinstance(reply_model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", reply_model):
        raise ValueError("회신 Codex 모델 ID를 확인하세요.")
    return {
        "poll_seconds": interval, "language": language, "codex_model": model,
        "reply_codex_model": reply_model,
        "reply_allowed_users": mailbot_reply.parse_reply_users(settings.get("reply_allowed_users", [])),
        "global_translation_prompt": validate_translation_prompt(settings.get("global_translation_prompt", "")),
        "reply_translation_prompt": validate_translation_prompt(settings.get("reply_translation_prompt", DEFAULT_REPLY_PROMPT)),
        **mailbot_reply.reply_format_options(settings),
    }

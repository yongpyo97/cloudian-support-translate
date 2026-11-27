import getpass
import json
import logging
import re
import ssl
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import truststore


class TelegramRequestError(RuntimeError):
    def __init__(self, message, code, description=""):
        super().__init__(message)
        self.code = code
        self.description = description or ""

    @property
    def unchanged(self):
        return "message is not modified" in self.description.casefold()


def telegram(token, method, *, document=None, quiet=False, request_timeout=None, **data):
    if document is None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    else:
        filename, contents = document
        boundary = f"mailbot-{uuid.uuid4().hex}"
        fields = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in data.items()
        ]
        fields.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        )
        content = "".join(fields).encode("utf-8") + contents + f"\r\n--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=content,
        headers={"Content-Type": content_type},
    )
    if not quiet:
        logging.info("Telegram %s 요청 중", method)
    try:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        timeout = request_timeout if request_timeout is not None else (90 if document is not None else 30)
        with urlopen(request, timeout=timeout, context=context) as response:
            result = json.load(response)
    except HTTPError as error:
        detail = f"Telegram {method} HTTP {error.code}"
        logging.warning(detail)
        description = ""
        try:
            with error:
                response = json.load(error)
                if isinstance(response, dict) and isinstance(response.get("description"), str):
                    description = response["description"]
        except (OSError, ValueError):
            pass
        raise TelegramRequestError(detail, error.code, description) from None
    except OSError as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            detail = f"Telegram {method} 인증서 검증 실패 ({reason.verify_message})"
        else:
            detail = f"Telegram {method} 연결 실패 ({type(reason).__name__})"
        logging.warning(detail)
        raise RuntimeError(detail) from None
    except ValueError:
        logging.warning("Telegram %s 응답 형식 오류", method)
        raise RuntimeError(f"Telegram {method} 응답 형식 오류") from None
    if not result.get("ok"):
        logging.warning("Telegram %s 요청 거절", method)
        description = result.get("description")
        raise TelegramRequestError(f"Telegram {method} 요청 실패", result.get("error_code"),
                                   description if isinstance(description, str) else "")
    if not quiet:
        logging.info("Telegram %s 성공", method)
    return result["result"]


def post_progress(token, chat_id, text, *, reply_to=None):
    kwargs = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if reply_to else {}
    try:
        message = telegram(token, "sendMessage", chat_id=chat_id, text=text, quiet=True,
                           request_timeout=5, **kwargs)
    except (OSError, RuntimeError):
        return None
    if isinstance(message, dict):
        return message.get("message_id")


def update_progress(token, chat_id, message_id, text, *, caption=False, clear_buttons=False):
    if not message_id:
        return False
    method, field = ("editMessageCaption", "caption") if caption else ("editMessageText", "text")
    kwargs = {field: text}
    if clear_buttons:
        kwargs["reply_markup"] = {"inline_keyboard": []}
    try:
        telegram(token, method, chat_id=chat_id, message_id=message_id, quiet=True, request_timeout=5, **kwargs)
    except TelegramRequestError as error:
        return error.unchanged
    except (OSError, RuntimeError):
        return False
    return True


def publish_progress(token, chat_id, text, *, progress_id=None, document=None, reply_markup=None, entities=None):
    kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
    if entities is not None:
        kwargs["entities"] = entities
    if document is not None and "reply_markup" in kwargs:
        kwargs["reply_markup"] = json.dumps(reply_markup)
    if progress_id:
        try:
            if document is not None:
                media = {"type": "document", "media": "attach://document", "caption": text}
                return telegram(token, "editMessageMedia", chat_id=chat_id, message_id=progress_id,
                                document=document, media=json.dumps(media), **kwargs)
            return telegram(token, "editMessageText", chat_id=chat_id, message_id=progress_id, text=text,
                            link_preview_options={"is_disabled": True}, **kwargs)
        except TelegramRequestError as error:
            if error.unchanged:
                return {"message_id": progress_id}
            if error.code not in (400, 403):
                raise
    if document is not None:
        result = telegram(token, "sendDocument", chat_id=chat_id, caption=text, document=document, **kwargs)
    else:
        result = telegram(token, "sendMessage", chat_id=chat_id, text=text,
                          link_preview_options={"is_disabled": True}, **kwargs)
    if progress_id:
        update_progress(token, chat_id, progress_id, "처리 결과를 새 메시지로 보냈습니다.")
    return result


def read_telegram_token():
    logging.info("Telegram 봇 토큰 입력 대기 중. 입력한 문자는 *로 표시됩니다. 입력 후 Enter를 누르세요.")
    try:
        token = getpass.getpass("Telegram 봇 토큰: ", echo_char="*").strip()
    except (EOFError, KeyboardInterrupt):
        logging.warning("Telegram 봇 토큰 입력이 취소되었습니다.")
        raise RuntimeError("Telegram 봇 토큰 입력이 취소되었습니다.") from None
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        logging.warning("Telegram 봇 토큰이 비어 있거나 형식이 올바르지 않습니다.")
        raise RuntimeError("토큰 형식을 확인하세요. BotFather에서 토큰을 다시 복사하세요.")
    logging.info("Telegram 봇 토큰 입력 완료")
    return token

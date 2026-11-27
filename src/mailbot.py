import argparse
import getpass
import hashlib
import imaplib
import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mailbot_message import (
    case_feedback_link,
    clean_cloudian_case_confirmation,
    clean_cloudian_metadata,
    CLOUDIAN_ACK,
    CLOUDIAN_ACK_FOOTER,
    cloudian_acknowledgement,
    cloudian_comment,
    CLOUDIAN_COPY_WAIT,
    CLOUDIAN_PORTAL,
    CLOUDIAN_REPLY_FOOTER,
    CLOUDIAN_REPLY_NOTICE,
    CLOUDIAN_STAMP,
    cloudian_support_body,
    cloudian_support_comment_key,
    comment_fingerprint,
    compact_mail_text,
    mail_content,
    mail_source_language,
    MailText,
    QUOTED_HEADER,
    QUOTED_HEADER_LABELS,
    remove_cloudian_reply_notice,
    remove_quoted_mail_headers,
)
from mailbot_codex import (
    CHATGPT_AUTH,
    choose_codex_model,
    chunks,
    codex_environment,
    codex_translation,
    DEFAULT_REPLY_PROMPT,
    find_codex,
    list_codex_models,
    prepare_reply,
    read_codex_info,
    translate,
)
from mailbot_telegram import (
    post_progress,
    publish_progress,
    read_telegram_token,
    telegram,
    TelegramRequestError,
    update_progress,
)
from mailbot_notifications import (
    attachment_filename,
    case_feedback_notification,
    closed_case_notification,
    expandable_quote_entity,
    format_mail_notification,
    handle_mail_preview,
    mail_preview_keyboard,
    notification_heading,
    send_mail,
)
from mailbot_windows import (
    background_stop_path,
    execution_lock,
    powershell_command,
    powershell_literal,
    protect_password,
    unlock_password,
    windows_password,
)
from mailbot_settings import (
    imap_accounts,
    PROMPT_LIMIT,
    runtime_options,
    save_accounts,
    save_settings,
    validate_translation_prompt,
)
from mailbot_state import (
    imap_mailbox_identity,
    mailbox_key,
    mark_handled,
    open_state,
    remember_mail_uids,
    saved_uid_page,
    uid_directory_key,
    UID_DIRECTORY_PAGE_SIZE,
    uid_subject,
)
from mailbot_imap import (
    checked,
    find_legacy_reply,
    imap_received_at,
    inbox_uid_page,
    is_original_cloudian_reply,
    mail_uid,
    mailbox_number,
    MailboxOperationError,
    open_mailbox,
    verify_imap_account,
)
from mailbot_polling import (
    clear_manual_retranslation,
    MailBodyUnavailable,
    mailbox_candidates,
    poll,
    poll_accounts,
    process_mail,
)


APP_VERSION = "1.0.12"


def read_imap_account():
    host = input("IMAP 서버 주소: ").strip()
    port = int(input("IMAP TLS 포트 [993]: ").strip() or "993")
    user = input("IMAP 이메일 주소: ").strip()
    if not host or not user or not 1 <= port <= 65535:
        raise ValueError("IMAP 서버, 포트, 이메일 주소를 확인하세요.")
    return {"imap_host": host, "imap_port": port, "imap_user": user, "mailbox": "INBOX"}


def background_task(config, *, enable, start_now=True, stop_running=True):
    if os.name != "nt":
        raise RuntimeError("백그라운드 자동 실행에는 Windows가 필요합니다.")
    task_id = hashlib.sha256(str(config.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
    task = f"$taskName = 'MailBot-{task_id}';"
    if enable:
        background_stop_path(config).unlink(missing_ok=True)
        executable = Path(sys.executable)
        if getattr(sys, "frozen", False):
            launch = f'--config "{config.resolve()}" --background'
            task += (
                f"$action = New-ScheduledTaskAction -Execute {powershell_literal(executable)} "
                f"-Argument {powershell_literal(launch)};"
            )
        else:
            action = f"& {powershell_literal(executable)}"
            action += f" {powershell_literal(Path(__file__).resolve())}"
            action += f" --config {powershell_literal(config.resolve())} --background; exit $LASTEXITCODE"
            command = powershell_command(action)
            task += (
                f"$action = New-ScheduledTaskAction -Execute \"$PSHOME\\powershell.exe\" "
                f"-Argument '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {command}';"
            )
        task += (
            "$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name;"
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity;"
            "$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited;"
            "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);"
            "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger "
            "-Principal $principal -Settings $settings -Force | Out-Null;"
        )
        if start_now:
            task += "Start-ScheduledTask -TaskName $taskName;"
    else:
        if stop_running:
            background_stop_path(config).touch()
        task += "if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {"
        if stop_running:
            task += "Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue;"
        task += "Unregister-ScheduledTask -TaskName $taskName -Confirm:$false;}"
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         powershell_command("$ErrorActionPreference = 'Stop';" + task)],
        capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Windows 작업 스케줄러 요청 실패 (종료 코드 {result.returncode})")


def setup(config):
    if config.exists():
        raise RuntimeError("기존 설정에 이메일을 추가하려면 --add-email을 사용하세요.")
    account = read_imap_account()
    codex = find_codex()
    if not codex:
        raise RuntimeError("Windows에서 Codex CLI를 찾을 수 없습니다.")
    model = choose_codex_model(codex)
    password = getpass.getpass("IMAP 비밀번호 또는 앱 비밀번호: ", echo_char="*")
    token = read_telegram_token()
    verify_imap_account(account, password)
    chat_ids = {}
    for update in telegram(token, "getUpdates", allowed_updates=["message"]):
        chat = update.get("message", {}).get("chat")
        if chat:
            chat_ids[chat["id"]] = chat.get("title") or chat.get("first_name", "")
    for chat_id, name in chat_ids.items():
        print(f"{chat_id}  {name}")
    chat_id = int(input("전송할 Telegram 채팅 ID: ").strip())
    telegram(token, "getChat", chat_id=chat_id)
    account["protected_password"] = protect_password(password)
    settings = {
        "accounts": [account],
        "telegram_chat_id": chat_id, "language": "한국어", "poll_seconds": 60, "codex_model": model,
    }
    with config.open("x", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(f"IMAP과 Telegram 연결을 확인했습니다. 설정 저장 위치 {config}")
    return password, token


def add_email(config, settings, accounts):
    account = read_imap_account()
    if any(
        saved["imap_host"].casefold() == account["imap_host"].casefold()
        and saved["imap_user"].casefold() == account["imap_user"].casefold()
        and saved["imap_port"] == account["imap_port"]
        and saved["mailbox"] == account["mailbox"]
        for saved in accounts
    ):
        raise ValueError("이미 등록된 이메일 계정입니다.")
    set_account_password(account)
    save_accounts(config, settings, [*accounts, account])
    print(f"{account['imap_user']} 등록 완료. 저장된 IMAP 계정 {len(accounts) + 1}개.")


def set_account_password(account):
    password = getpass.getpass(f"{account['imap_user']} IMAP 비밀번호 또는 앱 비밀번호: ", echo_char="*")
    verify_imap_account(account, password)
    account["protected_password"] = protect_password(password)
    return password


def edit_runtime_options(config, settings):
    codex = find_codex()
    if not codex:
        raise RuntimeError("Windows에서 Codex CLI를 찾을 수 없습니다.")
    options = runtime_options(settings)
    options["codex_model"] = choose_codex_model(codex, options["codex_model"])
    options["language"] = input(f"번역 언어 [{options['language']}]: ").strip() or options["language"]
    seconds = input(f"메일 확인 간격(초) [{options['poll_seconds']}]: ").strip()
    if seconds:
        options["poll_seconds"] = int(seconds)
    options = runtime_options(options)
    current = json.loads(config.read_text(encoding="utf-8-sig"))
    current.update(options)
    save_accounts(config, current, imap_accounts(current))
    logging.info("사용 설정을 저장했습니다. 실행 중인 봇이 다음 확인부터 적용합니다.")


def wait_for_next_poll(config, options):
    deadline = time.monotonic() + options["poll_seconds"]
    last_error = None
    while True:
        try:
            updated = runtime_options(json.loads(config.read_text(encoding="utf-8-sig")))
            if updated != options:
                logging.info(
                    "사용 설정 반영. 모델 %s, 번역 언어 %s, 확인 간격 %s초",
                    updated["codex_model"] or "Codex 기본값", updated["language"], updated["poll_seconds"],
                )
                return updated
            last_error = None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if type(error) is not last_error:
                logging.warning("사용 설정을 읽지 못했습니다. 현재 설정으로 계속 실행합니다.")
                last_error = type(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return options
        time.sleep(min(2, remaining))


def main():
    parser = argparse.ArgumentParser(description="IMAP 메일을 Codex로 번역해 Telegram으로 전송")
    entry_path = sys.executable if getattr(sys, "frozen", False) else __file__
    parser.add_argument("--config", type=Path, default=Path(entry_path).with_name("settings.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="받은편지함을 한 번 확인")
    mode.add_argument("--unread", action="store_true", help="기존 안 읽은 메일을 한 번 확인하고 전송")
    mode.add_argument("--chat-id", action="store_true", help="봇에 메시지를 보낸 채팅 ID 확인")
    mode.add_argument("--check-token", action="store_true", help="Telegram 봇 토큰 유효성 확인")
    mode.add_argument("--demo", action="store_true", help="샘플 이메일을 한국어로 번역")
    mode.add_argument("--setup", action="store_true", help="IMAP과 Telegram 연결을 확인하고 설정 저장")
    mode.add_argument("--add-email", action="store_true", help="기존 설정에 IMAP 이메일 추가")
    mode.add_argument("--update-password", action="store_true", help="등록된 IMAP 계정의 저장된 비밀번호 변경")
    mode.add_argument("--retranslate", metavar="UID", type=int, help="지정한 메일을 다시 번역해 한 번 전송")
    mode.add_argument("--enable-background", action="store_true", help="Windows 백그라운드 실행 및 로그인 시 자동 시작 켜기")
    mode.add_argument("--disable-background", action="store_true", help="Windows 백그라운드 실행 중지 및 자동 시작 끄기")
    mode.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--settings", action="store_true", help="모델, 번역 언어, 메일 확인 간격 변경")
    args = parser.parse_args()
    if args.retranslate is not None and not 1 <= args.retranslate <= 4294967295:
        parser.error("메일 UID는 1~4294967295 사이의 정수를 입력하세요.")
    log_path = args.config.with_name("mailbot.log")
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"))
    except OSError:
        print("로그 파일을 열 수 없어 콘솔에만 기록합니다.", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.info("MailBot 시작. 로그 파일 %s", log_path)
    if args.check_token:
        try:
            token = read_telegram_token()
            bot = telegram(token, "getMe")
        except RuntimeError as error:
            parser.exit(1, f"토큰 확인 실패 ({error})\n")
        print(f"유효한 Telegram 봇 토큰입니다. {bot['first_name']} (@{bot['username']}) · ID {bot['id']}")
        return 0
    first_run = len(sys.argv) == 1 and not args.config.exists()
    credentials = None
    if args.setup or first_run:
        try:
            credentials = setup(args.config)
        except imaplib.IMAP4.error:
            parser.exit(1, "IMAP 로그인 또는 받은편지함 접근에 실패했습니다.\n")
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            parser.exit(1, f"연결 설정 실패 ({detail})\n")
        if args.setup:
            return 0
    if args.demo:
        codex = find_codex()
        if not codex:
            parser.error("PATH 또는 설치된 Windows Codex 앱에서 Codex CLI를 찾을 수 없습니다.")
        subject = "Delivery schedule update"
        body = (
            "Hi Mina,\n\n"
            "Your order will ship on Thursday. Please confirm the delivery address by 3 p.m. tomorrow.\n\n"
            "Thank you,\nCustomer Support"
        )
        try:
            translation = translate(codex, subject, body, "한국어")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            parser.exit(1, f"샘플 번역 실패 ({type(error).__name__})\n")
        print(f"[원문]\nSubject: {subject}\n\n{body}\n\n[번역]\n{translation}")
        return 0
    if args.chat_id:
        try:
            token = read_telegram_token()
            updates = telegram(token, "getUpdates", allowed_updates=["message"])
        except RuntimeError as error:
            parser.exit(1, f"{error}\n")
        chats = {}
        for update in updates:
            chat = update.get("message", {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("title") or chat.get("first_name", "")
        for chat_id, name in chats.items():
            print(f"{chat_id}  {name}")
        if not chats:
            print("Telegram에서 봇에 /start 또는 새 메시지를 보낸 뒤 다시 실행하세요.")
        return
    try:
        settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
        accounts = imap_accounts(settings)
        if not settings["telegram_chat_id"]:
            raise ValueError("telegram_chat_id")
        options = runtime_options(settings)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"설정 파일을 확인하세요. ({error})\n")
    if args.settings:
        try:
            edit_runtime_options(args.config, settings)
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            logging.error("사용 설정 변경 실패 (%s)", str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__)
            return 1
        return 0
    if args.enable_background or args.disable_background:
        try:
            if args.enable_background:
                with execution_lock(args.config):
                    background_codex = find_codex()
                    if not background_codex:
                        raise RuntimeError("Codex CLI를 찾을 수 없습니다.")
                    for account in accounts:
                        if not account.get("protected_password"):
                            raise RuntimeError("먼저 일반 실행으로 모든 IMAP 비밀번호를 저장하세요.")
                        try:
                            unlock_password(account["protected_password"])
                        except (OSError, ValueError, TypeError):
                            raise RuntimeError("저장된 IMAP 비밀번호를 열 수 없습니다. --update-password로 갱신하세요.") from None
                    token = read_telegram_token()
                    telegram(token, "getMe")
                    telegram(token, "getChat", chat_id=settings["telegram_chat_id"])
                    if "codex_model" not in settings:
                        settings["codex_model"] = choose_codex_model(background_codex)
                    settings["background_token"] = protect_password(token)
                    save_accounts(args.config, settings, accounts)
                background_task(args.config, enable=True)
                logging.info("백그라운드 실행을 시작하도록 요청했습니다. Windows 로그인 시 자동 시작합니다.")
            else:
                background_task(args.config, enable=False)
                if settings.pop("background_token", None) is not None:
                    save_accounts(args.config, settings, accounts)
                logging.info("백그라운드 실행을 중지하고 로그인 시 자동 시작을 껐습니다.")
        except (OSError, RuntimeError, ValueError, EOFError, subprocess.TimeoutExpired) as error:
            detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
            logging.error("백그라운드 설정 실패 (%s)", detail)
            return 1
        return 0
    if args.add_email or args.update_password:
        try:
            if args.add_email:
                add_email(args.config, settings, accounts)
            else:
                account = accounts[0]
                if len(accounts) > 1:
                    for index, entry in enumerate(accounts, 1):
                        print(f"{index}. {entry['imap_user']} ({entry['imap_host']})")
                    choice = int(input("비밀번호를 변경할 계정 번호: "))
                    if not 1 <= choice <= len(accounts):
                        raise ValueError("목록에 표시된 계정 번호를 입력하세요.")
                    account = accounts[choice - 1]
                set_account_password(account)
                save_accounts(args.config, settings, accounts)
                print(f"{account['imap_user']} 비밀번호를 저장했습니다.")
        except imaplib.IMAP4.error:
            parser.exit(1, "IMAP 로그인 또는 받은편지함 접근에 실패했습니다.\n")
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
            parser.exit(1, f"IMAP 계정 설정 실패 ({detail})\n")
        return 0
    codex = find_codex()
    if not codex:
        logging.error("PATH 또는 설치된 Windows Codex 앱에서 Codex CLI를 찾을 수 없습니다.")
        return 1
    try:
        run_lock = execution_lock(args.config)
    except (OSError, RuntimeError) as error:
        logging.error("%s", str(error) if isinstance(error, RuntimeError) else "실행 잠금 파일을 열 수 없습니다.")
        return 1
    if args.retranslate is not None and len(accounts) > 1:
        for index, account in enumerate(accounts, 1):
            print(f"{index}. {account['imap_user']} ({account['imap_host']})")
        try:
            selected = int(input("다시 번역할 메일의 계정 번호: "))
            if not 1 <= selected <= len(accounts):
                raise ValueError
        except (ValueError, EOFError):
            parser.error("목록에 표시된 계정 번호를 입력하세요.")
        selected_account = accounts[selected - 1]
    else:
        selected_account = None
    logging.info("Codex CLI 확인 완료")
    if credentials is None:
        try:
            if args.background:
                try:
                    token = unlock_password(settings["background_token"])
                except (KeyError, OSError, ValueError, TypeError):
                    raise RuntimeError("저장된 Telegram 토큰을 열 수 없습니다. --enable-background로 다시 설정하세요.") from None
            else:
                token = read_telegram_token()
            telegram(token, "getMe")
            telegram(token, "getChat", chat_id=settings["telegram_chat_id"])
        except RuntimeError as error:
            logging.error("Telegram 연결 확인 실패 (%s)", error)
            return 1
        active_accounts = []
        passwords_changed = False
        for account in accounts:
            if selected_account is not None and account is not selected_account:
                continue
            password = None
            if account.get("protected_password"):
                try:
                    password = unlock_password(account["protected_password"])
                except (OSError, ValueError, TypeError):
                    logging.warning("%s 저장된 비밀번호를 열 수 없습니다.", account["imap_user"])
            if not password:
                if args.background:
                    logging.error("%s 저장된 IMAP 비밀번호가 필요합니다. --update-password를 실행하세요.", account["imap_user"])
                    continue
                try:
                    password = set_account_password(account)
                    passwords_changed = True
                except (OSError, ValueError, RuntimeError, imaplib.IMAP4.error):
                    logging.error("%s 비밀번호를 확인하거나 저장할 수 없습니다.", account["imap_user"])
                    continue
            active_accounts.append((account, password))
        if passwords_changed:
            try:
                save_accounts(args.config, settings, accounts)
            except OSError:
                parser.exit(1, "설정 파일에 암호화된 IMAP 비밀번호를 저장할 수 없습니다.\n")
        if not active_accounts:
            logging.error("연결할 수 있는 IMAP 계정이 없습니다.")
            return 1
    else:
        password, token = credentials
        active_accounts = [(accounts[0], password)]
    if "codex_model" not in settings and not (args.background or args.retranslate is not None or args.once or args.unread):
        try:
            settings["codex_model"] = choose_codex_model(codex)
            save_accounts(args.config, settings, accounts)
            options = runtime_options(settings)
        except (OSError, EOFError, ValueError) as error:
            logging.error("Codex 모델 설정 실패 (%s)", str(error) if isinstance(error, ValueError) else type(error).__name__)
            return 1
    logging.info("메일 확인 시작. IMAP 계정 %s개, 확인 간격 %s초", len(active_accounts), options["poll_seconds"])
    database = open_state(args.config)
    try:
        startup_pending = set(range(len(active_accounts)))
        while True:
            failed = selected_account is None and len(active_accounts) != len(accounts)
            try:
                poll_accounts([
                    (settings | options | account, password, index in startup_pending and args.retranslate is None)
                    for index, (account, password) in enumerate(active_accounts)
                ], token, codex, database, unread=args.unread, retranslate=args.retranslate)
                startup_pending.clear()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, imaplib.IMAP4.error) as error:
                detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                logging.error("메일 확인 실패 (%s)", detail)
                failed = True
            if args.once or args.unread or args.retranslate is not None:
                return int(failed)
            options = wait_for_next_poll(args.config, options)
    except KeyboardInterrupt:
        return 0
    finally:
        database.close()
        run_lock.close()


if __name__ == "__main__":
    if len(sys.argv) == 1 or "--background" in sys.argv[1:] or "--gui" in sys.argv[1:]:
        import mailbot_gui

        gui_parser = argparse.ArgumentParser()
        gui_parser.add_argument("--config", type=Path)
        gui_parser.add_argument("--background", action="store_true")
        gui_parser.add_argument("--gui", action="store_true")
        gui_options = gui_parser.parse_args()
        executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        raise SystemExit(mailbot_gui.run(
            sys.modules[__name__], gui_options.config or executable.with_name("settings.json"),
            hidden=gui_options.background,
        ))
    try:
        status = main()
    except SystemExit as exit_status:
        if exit_status.code and getattr(sys, "frozen", False) and len(sys.argv) == 1 and sys.stdin.isatty():
            try:
                input("Enter 키를 누르면 창을 닫습니다.")
            except (EOFError, KeyboardInterrupt):
                pass
        raise
    raise SystemExit(status)

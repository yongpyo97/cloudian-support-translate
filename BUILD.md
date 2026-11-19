# Cloudian Support Translate 소스

저장소를 내려받은 폴더를 Codex나 편집기에서 열어 수정할 수 있습니다.

- `src/mailbot.py`는 메일 수신, 번역, Telegram 전송과 기본 프롬프트를 담고 있습니다.
- `src/mailbot_gui.py`는 설정 화면, 트레이, 실행 중 설정 적용을 담당합니다.
- `work/cloudian.ico`와 `work/cloudian-dark.ico`는 설정 창과 트레이에 사용하는 [Cloudian 로고](https://cloudian.com/company/brand-assets/)입니다.
- `settings.example.json`은 개인 설정이 없는 예시입니다.

## 실행 파일 다시 빌드하기

Windows용 64비트 Python 3.15가 설치된 PC에서 저장소를 내려받은 폴더를 PowerShell로 열고 실행합니다.

```powershell
.\Build.ps1
```

스크립트는 폴더 안에 가상환경을 만들고 라이브러리를 설치한 뒤 `dist` 폴더에 EXE를 생성합니다. 빌드 PC에서 라이브러리 설치에 인터넷 연결이 필요합니다. 완성된 EXE에서 실제 번역을 사용하려면 Codex CLI를 설치하고 ChatGPT 계정으로 로그인해야 합니다.

빌드한 EXE를 기존 설치 폴더에 교체하면 그 폴더의 `settings.json`과 `settings.sqlite3`를 사용합니다. 이 저장소에는 개인 설정, 메일 데이터, 비밀번호와 토큰이 들어 있지 않습니다.

기본 실행 파일은 콘솔 창 없이 실행됩니다. 명령줄에서 사용하는 버전은 `.\Build.ps1 -Console`로 빌드할 수 있습니다.

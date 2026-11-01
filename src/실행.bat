@echo off
chcp 65001 > nul
cd /d "%~dp0"
pip install cryptography -q
REM Claude 챗봇: 환경변수 ANTHROPIC_API_KEY (없으면 검색형 답변) / 이메일 발송: 환경변수 SMTP_PASSWORD (SMTP 계정을 쓰는 경우)
REM 처음 실행하면 브라우저에서 관리자 계정을 만듭니다.
REM PFX 비밀번호는 플랫폼의 'PFX 비밀번호' 탭에서 담당자가 등록 → AES-256-GCM 암호화 보관. 암호화 키: %APPDATA%\cert_platform\vault.key (DB와 분리, 백업 시 따로 보관)
python platform_server.py --scan "%~dp0pem" --deploy "%~dp0deploy"
pause

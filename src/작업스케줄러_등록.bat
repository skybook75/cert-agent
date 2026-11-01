@echo off
chcp 65001 > nul
cd /d "%~dp0"
REM 알림_배치.bat 을 매일 오전 8시 50분에 실행하도록 Windows 작업 스케줄러에 등록합니다 (현재 사용자 권한).
schtasks /Create /F /SC DAILY /ST 08:50 /TN "인증서관리_알림Agent" /TR "\"%~dp0알림_배치.bat\""
schtasks /Query /TN "인증서관리_알림Agent"
pause

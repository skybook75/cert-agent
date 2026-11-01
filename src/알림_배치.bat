@echo off
chcp 65001 > nul
cd /d "%~dp0"
REM 매일 1회: 알림 Agent 실행 후, 아직 보내지 않은 알림을 담당자 이메일로 발송 (SMTP 는 플랫폼 관리 탭에서 설정)
python agents.py notify --send >> "%~dp0notify.log" 2>&1

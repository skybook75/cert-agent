인증서 관리 플랫폼 — 서버 Agent 설치 (폐쇄망)

1. 플랫폼에서 서버 등록
   관리자 로그인 → [관리] 탭 → '서버 Agent' → 서버 등록(호스트·OS·배포 단계) → [등록하고 토큰 발급] → 토큰 복사(1회만 표시)
   · 배포 단계: 수집만 / 개발·테스트·운영 서버(승인된 배포를 이 서버에 적용)

2-A. Linux 서버 (root)
   sudo ./agent_install.sh --platform https://<플랫폼 주소> --token <토큰> --scan /etc/nginx/ssl --nginx
     · nginx 가 아니면 --nginx 대신 --check "설정검사 명령" --reload "재적용 명령" --verify 127.0.0.1:443
     · 플랫폼 HTTPS 를 사내 CA 로 발급했다면 --cafile /경로/corp-ca.pem
     · Python 3.9+ 와 cryptography 가 필요합니다(OS 패키지 python3-cryptography, 또는 묶음의 wheels/py39 등)

2-B. Windows 서버 (관리자 PowerShell)
   powershell -ExecutionPolicy Bypass -File .\agent_install.ps1 -Platform https://<플랫폼 주소> -Token <토큰> -Scan "C:\certs"
     · Python 이 없으면 묶음 windows\ 폴더의 설치 파일로 자동 설치합니다(묶음 폴더째 복사해 실행)

3. 확인
   플랫폼 [수집 승인] 탭에 이 서버의 인증서가 올라오면 정상입니다.
   이후 1분마다 [수집 실행] 요청·승인된 배포를 확인하고, 30분마다 전체 수집합니다.

보안: 개인키·PFX 파일은 플랫폼으로 보내지 않습니다(인증서 공개 부분과 경로만 전송).

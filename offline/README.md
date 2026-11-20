# 인증서 관리 AI Agent — 폐쇄망 설치 가이드 (v1.0.0)

인터넷이 안 되는 사내망에 **관리 플랫폼**(웹)과 **서버 Agent**(인증서가 있는 각 서버)를 설치하는 순서입니다.
위에서부터 그대로 따라 하면 됩니다.

- 웹으로 보기·내려받기: <https://skybook75.github.io/cert-agent/install/>
- 파일 직접 주소: `https://skybook75.github.io/cert-agent/download/<파일명>` (저장소 `download/` 폴더)
- 소스: <https://github.com/skybook75/cert-agent> (`src/` 폴더)

---

## 0. 한눈에 보기

```
 [인터넷 PC]                    [반입]     [폐쇄망]
 ① 묶음 내려받기                          ③ 플랫폼 서버 1대  ── https://cert.corp.local
 ② prepare_online 실행(선택) ── USB 등 ──▶ ④ 각 서버에 Agent  ── 서버→플랫폼 방향으로만 접속
```

| 구성 | 설치 위치 | 필요한 것 |
|---|---|---|
| 관리 플랫폼 | Linux 서버 1대 (RHEL·Rocky·Alma 8/9, Amazon Linux 2023) | Python 3.9+, cryptography / 선택: PostgreSQL, nginx, Qdrant |
| 서버 Agent | 인증서가 있는 서버 (Linux, Windows Server 2016+) | Python 3.9+, cryptography |

권장 사양(플랫폼): 2 vCPU · 4 GB 메모리 · 20 GB 디스크. 벡터 DB 를 쓰지 않으면 2 GB 메모리로도 동작합니다.

### 내려받을 파일

| 파일 | 내용 |
|---|---|
| `cert-agent-offline-1.0.0.tar.gz` | 전체 묶음: 소스(`src/`), 설치 스크립트(`install/`), 준비 스크립트(`prepare/`), Qdrant 실행 파일(`bin/`), Agent 묶음(`agent/`) |
| `cert-agent-agent-1.0.0.zip` | 각 서버에 복사할 Agent 만 (`cert_agent.py` + 설치 스크립트) |
| `SHA256SUMS-1.0.0.txt` | 무결성 확인용 해시 |

---

## 1. [인터넷 PC] 묶음 내려받기

```bash
# Linux / macOS / WSL
curl -LO https://skybook75.github.io/cert-agent/download/cert-agent-offline-1.0.0.tar.gz
curl -LO https://skybook75.github.io/cert-agent/download/cert-agent-agent-1.0.0.zip
curl -LO https://skybook75.github.io/cert-agent/download/SHA256SUMS-1.0.0.txt
sha256sum -c SHA256SUMS-1.0.0.txt          # 둘 다 OK 가 나와야 합니다
tar xzf cert-agent-offline-1.0.0.tar.gz
```

Windows PC 라면 위 주소를 브라우저로 내려받고 PowerShell 에서 `Get-FileHash 파일명` 으로 해시를 비교합니다.

## 2. [인터넷 PC] 추가 파일 채우기 (필요할 때만)

묶음에는 **소스·설치 스크립트·Qdrant** 가 들어 있습니다. 아래 경우에만 인터넷 PC에서 준비 스크립트를 한 번 실행해 나머지를 채웁니다.

| 이런 경우 | 채워지는 폴더 |
|---|---|
| 폐쇄망 dnf 저장소에 `python3-cryptography` 가 없다 | `wheels/py39`, `wheels/py311`, `wheels/py312` |
| 벡터 DB(하이브리드 검색)를 쓴다 | `wheels/…`(fastembed) + `models/`(임베딩 모델 약 230 MB) |
| Windows 서버에 Agent 를 설치한다 | `windows/`(Python 설치 파일 + cryptography 휠) |

```bash
cd cert-agent-offline-1.0.0
./prepare/prepare_online.sh                 # 전부 (벡터 DB 안 쓰면 --no-vector)
```
```powershell
# Windows 인터넷 PC
cd cert-agent-offline-1.0.0
powershell -ExecutionPolicy Bypass -File .\prepare\prepare_online.ps1        # 벡터 DB 안 쓰면 -NoVector
```

끝나면 `cert-agent-offline-1.0.0` **폴더 전체**를 반입 매체에 담아 폐쇄망으로 옮깁니다.
반입 후 `sha256sum -c SHA256SUMS` (기본 묶음)와 `sha256sum -c SHA256SUMS.prepared` (채운 파일)로 확인합니다.

---

## 3. [폐쇄망] 관리 플랫폼 설치

플랫폼 서버에 묶음 폴더를 복사한 뒤 **root** 로 실행합니다. OS 패키지(Python·PostgreSQL·nginx)는 사내 dnf 저장소(미러·ISO)에서 설치합니다.

### 3-1. 가장 간단한 설치 (SQLite, HTTP 8000)

```bash
cd cert-agent-offline-1.0.0
sudo ./install/platform_install.sh
```

### 3-2. 운영 권장 설치 (PostgreSQL + 벡터 DB + HTTPS)

```bash
sudo ./install/platform_install.sh --db postgres --vector \
     --https /root/cert/cert.corp.local.crt /root/cert/cert.corp.local.key
```

| 옵션 | 하는 일 |
|---|---|
| `--db postgres` | 같은 서버에 PostgreSQL 설치·초기화, 127.0.0.1 전용·SCRAM 인증, 접속 정보는 `/etc/cert-platform/db.env`(600)에만, 매일 03:30 백업(14일) |
| `--vector` | Qdrant(127.0.0.1:6333) + 임베딩 모델로 가이드 색인 → QA 검색 1위 적중 85% → 95% |
| `--https 인증서 키` | nginx 가 443 에서 사내 인증서로 HTTPS 제공, 플랫폼은 127.0.0.1:8000 에만 열림 |
| `--port 8000` | 플랫폼 포트 변경 |

스크립트는 다시 실행해도 안전합니다(설정·데이터·보관함 키 유지, 프로그램은 `src.bak.날짜` 로 보관 후 교체). 옵션은 나중에 추가로 실행해도 됩니다.

### 3-3. 설치 결과

| 항목 | 위치 |
|---|---|
| 프로그램 | `/opt/cert-agent/src`, 가상환경 `/opt/cert-agent/venv` (서비스 계정 `certplat`) |
| 데이터 | `/var/lib/cert-platform` (SQLite DB, 배포 폴더, 임베딩 모델) |
| 설정·비밀 값 | `/etc/cert-platform/env`, `db.env`, **`vault.key`** (모두 600) |
| 서비스 | `cert-platform`(웹), `cert-notify.timer`(매일 09:05 만료 알림), 선택: `qdrant`, `postgresql`, `nginx` |
| 백업 | `/var/backups/cert-platform` (매일 03:30, 14일 보관) |

> **`vault.key` 는 DB 와 따로 백업하세요.** PFX 비밀번호 보관함의 암호화 키입니다. 잃어버리면 저장된 PFX 비밀번호를 다시 등록해야 합니다.

### 3-4. 첫 접속

1. 브라우저로 `http://<서버IP>:8000` (HTTPS 설치 시 `https://<서버 이름>`) 접속
2. **'최초 관리자 계정 만들기'** 화면에서 관리자 계정을 만듭니다. 첫 계정만 만들 수 있으므로 설치 직후 바로 만드세요.
3. [관리] 탭에서
   - 사용자 추가: 담당자·검토자·승인자·조회자 (새 사용자는 첫 로그인 때 비밀번호 변경)
   - 메일(SMTP): 사내 SMTP 서버 주소·보내는 주소 입력 → [테스트 발송]
4. [관리대장] 탭에서 기존 엑셀 관리대장을 올려 등록합니다.

### 3-5. 폐쇄망 설정 (`/etc/cert-platform/env`)

| 설정 | 설명 |
|---|---|
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | 사내 LLM 게이트웨이(Anthropic Messages API 호환)가 있으면 입력. **비워 두면 QA 챗봇은 가이드 원문 검색형 답변**으로 동작하고 나머지 기능은 모두 그대로 동작합니다. |
| `SMTP_PASSWORD` | 사내 SMTP 가 계정 인증을 요구할 때만 |
| `NTFY_URL` | 사내 ntfy 서버로 휴대폰 푸시를 보낼 때만 |
| `HF_HUB_OFFLINE=1` | 임베딩 모델을 인터넷에서 찾지 않도록 고정(기본 설정됨) |

수정 후 `sudo systemctl restart cert-platform`.

---

## 4. [폐쇄망] 서버 Agent 설치 (인증서가 있는 각 서버)

### 4-1. 플랫폼에서 서버 등록 → 토큰 받기

관리자 로그인 → **[관리] 탭 → '서버 Agent' → 서버 등록**(호스트·OS·배포 단계) → **[등록하고 토큰 발급]** → 토큰 복사(한 번만 표시).

- 배포 단계: `수집만` 또는 `개발/테스트/운영 서버(배포 적용)` — 승인된 인증서 교체를 이 서버에 직접 적용할지 정합니다.

### 4-2. Linux 서버

`cert-agent-agent-1.0.0.zip` 을 풀거나 묶음의 `agent/` 폴더를 서버에 복사한 뒤:

```bash
# nginx 서버 (교체 후 설정 검사 → 재적용 → 443 응답 지문 확인까지 자동, 실패 시 자동 원복)
sudo ./agent_install.sh --platform https://cert.corp.local --token <토큰> --scan /etc/nginx/ssl --nginx

# 수집만 하는 서버
sudo ./agent_install.sh --platform https://cert.corp.local --token <토큰> --scan /etc/pki/tls/certs,/opt/app/ssl

# 그 외 웹서버(예: Apache)
sudo ./agent_install.sh --platform https://cert.corp.local --token <토큰> --scan /etc/httpd/ssl \
     --check "apachectl configtest" --reload "systemctl reload httpd" --verify 127.0.0.1:443
```

- 플랫폼 HTTPS 를 사내 CA 로 발급했다면 `--cafile /etc/pki/ca-trust/source/anchors/corp-ca.pem` 추가 (또는 서버에 사내 CA 를 `update-ca-trust` 로 신뢰 등록)
- cryptography 가 없으면 OS 패키지 → 묶음의 `wheels/` 순서로 자동 설치합니다(묶음 폴더째 복사했을 때).

### 4-3. Windows 서버 (IIS 등)

묶음 폴더째(또는 `agent/` + `windows/`) 복사한 뒤 **관리자 PowerShell**:

```powershell
powershell -ExecutionPolicy Bypass -File .\agent\agent_install.ps1 -Platform https://cert.corp.local -Token <토큰> -Scan "C:\certs"
```

Python 이 없으면 `windows\python-3.12.7-amd64.exe` 로 조용히 설치하고, Windows 인증서 저장소(`LocalMachine\My`)도 함께 수집합니다.

### 4-4. 동작 확인

- 플랫폼 **[수집 승인]** 탭에 이 서버의 인증서가 올라오면 정상입니다.
- 이후 Agent 는 **1분마다** 플랫폼의 [수집 실행] 요청·승인된 배포를 확인하고, **30분마다** 전체 수집합니다.
- 로그: Linux `journalctl -u cert-agent -u cert-agent-poll -f` / Windows 작업 스케줄러 `cert-agent`, `cert-agent-poll`

---

## 5. 방화벽

| 출발 → 도착 | 포트 | 용도 |
|---|---|---|
| 사용자 PC → 플랫폼 | 443 (또는 8000) | 웹 화면 |
| 각 서버 Agent → 플랫폼 | 443 (또는 8000) | 수집 결과 업로드·배포 작업 수신 (**Agent 쪽에서만 접속**, 플랫폼 → 서버 방향 포트 불필요) |
| 플랫폼 → 사내 SMTP | 25 / 587 | 만료 알림 메일 |
| 플랫폼 → 사내 LLM 게이트웨이 | 443 | (선택) QA 챗봇 생성형 답변 |

PostgreSQL(5432)과 Qdrant(6333)는 플랫폼 서버 안(127.0.0.1)에서만 열립니다.

## 6. 운영

```bash
systemctl status cert-platform              # 상태
journalctl -u cert-platform -f              # 로그
systemctl list-timers 'cert-*'              # 알림·Agent 타이머
sudo -u certplat /opt/cert-agent/venv/bin/python /opt/cert-agent/src/agents.py notify --send   # 만료 알림 즉시 실행
```

- **업그레이드**: 새 묶음을 풀고 같은 옵션으로 `platform_install.sh` 를 다시 실행 → 이전 프로그램은 `/opt/cert-agent/src.bak.날짜` 에 보관됩니다. Agent 는 `agent_install.sh` 를 같은 인자로 다시 실행합니다.
- **되돌리기**: `systemctl stop cert-platform` → `src` 를 `src.bak.날짜` 로 바꾸고 → `systemctl start cert-platform`
- **백업 대상**: DB(자동 백업), `/etc/cert-platform/vault.key`, `/etc/cert-platform/env` — vault.key 는 DB 와 **다른 매체**에 보관
- **Agent 제거(Linux)**: `systemctl disable --now cert-agent.timer cert-agent-poll.timer && rm -rf /opt/cert-agent /etc/cert-agent /etc/systemd/system/cert-agent*` 후 플랫폼에서 해당 서버 Agent 삭제(토큰 즉시 무효)

## 7. 문제 해결

| 증상 | 확인 |
|---|---|
| `cryptography 가 없습니다` | 사내 저장소에 `python3-cryptography` 가 있는지, 없으면 인터넷 PC에서 `prepare_online` 실행 후 `wheels/` 반입 |
| `Python 3.9 이상이 필요합니다` (RHEL 8) | `dnf install python3.11` (AppStream) 후 다시 실행 |
| 화면이 안 열림 | `systemctl status cert-platform`, 방화벽 포트, HTTPS 설치 시 `nginx -t` / SELinux `setsebool -P httpd_can_network_connect 1` |
| Agent 첫 실행 실패 | 플랫폼 주소·토큰 오타, Agent → 플랫폼 포트 방화벽, 사내 CA 사용 시 `--cafile` |
| `가이드 색인 실패` (--vector) | `models/` 가 비어 있음 → 인터넷 PC에서 `prepare_online` 으로 모델을 채워 반입 |
| QA 챗봇이 원문만 보여 줌 | 정상(LLM 미설정 시 검색형 답변). 사내 LLM 게이트웨이가 있으면 3-5 설정 |

## 8. 보안 요약

- 개인키·PFX 파일은 플랫폼으로 보내지 않습니다. Agent 는 인증서 공개 부분(DER)과 경로만 올립니다.
- PFX·서버 접속 비밀번호는 AES-256-GCM 으로 암호화 보관(키는 DB 와 분리), Agent 에는 Agent 공개키(RSA-OAEP)로 감싸서만 전달합니다.
- 사용자 비밀번호 PBKDF2-SHA256, 세션 쿠키(HttpOnly·SameSite=Strict) + CSRF 토큰, 5회 실패 시 10분 잠금.
- 모든 변경은 서버의 권한 검사(역할·항목·상태 순서·담당 범위·직무 분리)를 거치고 감사 로그에 남습니다.
- 플랫폼은 전용 계정(`certplat`)으로 실행되며 systemd 보호 옵션(`NoNewPrivileges`, `ProtectSystem=full`)을 씁니다.
- Agent 토큰은 플랫폼에 해시만 저장되며, 서버 Agent 를 삭제하면 즉시 무효가 됩니다.

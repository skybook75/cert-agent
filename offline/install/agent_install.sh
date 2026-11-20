#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# 서버 Agent 설치 (Linux: RHEL/Rocky/Alma 8·9, Amazon Linux 2023, Ubuntu 20.04+) — 인터넷 불필요
#
#   sudo ./agent_install.sh --platform https://cert.corp.local --token <발급 토큰> \
#        --scan /etc/nginx/ssl,/etc/pki/tls/certs \
#        [--nginx]                       # nginx 서버: 교체 후 설정검사·재적용·443 응답 확인 자동 설정
#        [--check "명령" --reload "명령" --verify 127.0.0.1:443]   # 그 외 웹서버
#        [--cafile /path/corp-ca.pem]    # 플랫폼 HTTPS 가 사내 CA 로 발급된 경우
#
#   토큰: 플랫폼 [관리] 탭 '서버 Agent' 에서 서버 등록 → [등록하고 토큰 발급] 시 1회 표시됩니다.
#   설치 후 1분마다 플랫폼의 [수집 실행] 요청·승인된 배포를 확인하고, 30분마다 전체 수집을 합니다.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
PLAT=""; TOKEN=""; SCAN=""; CHECK=""; RELOAD=""; VERIFY=""; CAFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLAT="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --scan) SCAN="$2"; shift 2;;
    --nginx) CHECK="nginx -t"; RELOAD="systemctl reload nginx"; VERIFY="127.0.0.1:443"; shift;;
    --check) CHECK="$2"; shift 2;;
    --reload) RELOAD="$2"; shift 2;;
    --verify) VERIFY="$2"; shift 2;;
    --cafile) CAFILE="$2"; shift 2;;
    -h|--help) sed -n 2,14p "$0"; exit 0;;
    *) echo "알 수 없는 옵션: $1"; exit 2;;
  esac
done
die(){ printf '\033[1;31m[중단]\033[0m %s\n' "$1"; exit 1; }
[ "$(id -u)" = 0 ] || die "root 권한으로 실행하세요"
[ -n "$PLAT" ] && [ -n "$TOKEN" ] && [ -n "$SCAN" ] || die "--platform, --token, --scan 은 필수입니다 (--help 참고)"
AG="$D/cert_agent.py"; [ -f "$AG" ] || AG="$D/../src/remote/cert_agent.py"
[ -f "$AG" ] || die "cert_agent.py 를 찾지 못했습니다"

PY=""
for c in python3.12 python3.11 python3.9 python3; do
  if command -v $c >/dev/null && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then PY=$(command -v $c); break; fi
done
if [ -z "$PY" ]; then
  (dnf install -y python3.11 || dnf install -y python39 || apt-get install -y python3) >/dev/null 2>&1 || true
  for c in python3.11 python3.9 python3; do command -v $c >/dev/null && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' && { PY=$(command -v $c); break; }; done
fi
[ -n "$PY" ] || die "Python 3.9 이상이 필요합니다(사내 저장소: dnf install python3.11)"
PYV=$($PY -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')

install -d -m 755 /opt/cert-agent
install -m 755 "$AG" /opt/cert-agent/cert_agent.py
[ -x /opt/cert-agent/venv/bin/python ] || $PY -m venv --system-site-packages /opt/cert-agent/venv
VPY=/opt/cert-agent/venv/bin/python
if ! $VPY -c 'import cryptography' 2>/dev/null; then
  (dnf install -y python3-cryptography || apt-get install -y python3-cryptography) >/dev/null 2>&1 || true
  if ! $VPY -c 'import cryptography' 2>/dev/null; then
    for W in "$D/wheels/py$PYV" "$D/../wheels/py$PYV"; do [ -d "$W" ] && $VPY -m pip install -q --no-index --find-links "$W" cryptography && break; done
  fi
fi
$VPY -c 'import cryptography' 2>/dev/null || die "cryptography 가 없습니다: OS 패키지(python3-cryptography) 또는 wheels/py$PYV 를 준비하세요"

install -d -m 700 /etc/cert-agent
$VPY - "$PLAT" "$TOKEN" "$SCAN" "$CHECK" "$RELOAD" "$VERIFY" "$CAFILE" <<'PY'
import json, sys
plat, token, scan, check, reload, verify, cafile = sys.argv[1:8]
cfg = {"platform": plat.rstrip("/"), "token": token, "scan": [s.strip() for s in scan.split(",") if s.strip()], "service": {}}
if check: cfg["service"]["check"] = check
if reload: cfg["service"]["reload"] = reload
if verify: cfg["service"]["verify"] = verify
if cafile: cfg["cafile"] = cafile
open("/etc/cert-agent/agent.json", "w").write(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n")
PY
chmod 600 /etc/cert-agent/agent.json

for n in run:30min:2min poll:1min:1min; do
  IFS=: read -r cmd every boot <<<"$n"
  name=cert-agent; [ $cmd = poll ] && name=cert-agent-poll
  cat > /etc/systemd/system/$name.service <<EOF
[Unit]
Description=Cert agent $cmd
[Service]
Type=oneshot
ExecStart=$VPY /opt/cert-agent/cert_agent.py $cmd
EOF
  cat > /etc/systemd/system/$name.timer <<EOF
[Unit]
Description=Cert agent $cmd every $every
[Timer]
OnBootSec=$boot
OnUnitActiveSec=$every
AccuracySec=1s
[Install]
WantedBy=timers.target
EOF
done
systemctl daemon-reload
systemctl enable -q --now cert-agent.timer cert-agent-poll.timer
echo "── 첫 수집 실행 ──"
if $VPY /opt/cert-agent/cert_agent.py run; then
  printf '\033[1;32m설치 완료\033[0m — 플랫폼 [수집 승인] 탭에서 결과를 확인하세요. 로그: journalctl -u cert-agent -u cert-agent-poll -f\n'
else
  die "첫 실행 실패: 플랫폼 주소·토큰·방화벽(플랫폼 포트)·cafile 을 확인하세요"
fi

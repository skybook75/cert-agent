#!/bin/bash
# 서버 Agent 갱신: 최신 cert_agent.py + 1분 poll 타이머('수집 실행' 요청·승인 배포 즉시 처리)
set -euo pipefail
cd /opt/cert-agent-src && git pull -q --ff-only
cp src/remote/cert_agent.py /opt/cert-agent/
cat > /etc/systemd/system/cert-agent-poll.service <<'U'
[Unit]
Description=Cert agent poll (run-now requests, approved deploys)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/cert-agent/cert_agent.py poll
U
cat > /etc/systemd/system/cert-agent-poll.timer <<'U'
[Unit]
Description=Cert agent poll every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
U
systemctl daemon-reload
systemctl enable --now cert-agent-poll.timer
echo "agent updated · poll timer: $(systemctl is-active cert-agent-poll.timer)"

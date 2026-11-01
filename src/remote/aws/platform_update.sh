#!/bin/bash
# 운영 중인 플랫폼 서버 갱신: 최신 소스 받기 → Node(유지보수 Agent JS 문법 검사용) → Claude 키 다시 읽기 → 정기 오케스트레이션 타이머 → 재시작
set -euo pipefail
SRC=/opt/cert-agent-src
P=$SRC/src
cd $SRC
git stash -q 2>/dev/null || true          # 유지보수 Agent 가 현장에서 바꾼 파일은 stash 에 보관
git pull -q --ff-only
command -v node >/dev/null || dnf install -y -q nodejs >/dev/null
python3 -c "import paramiko" 2>/dev/null || { dnf install -y -q python3-pip >/dev/null; pip3 install -q paramiko >/dev/null 2>&1 || true; }
T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/placement/region)
K=$(aws ssm get-parameter --region "$REGION" --name /cert-lab/anthropic-api-key --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
umask 077
{ [ -n "$K" ] && [ "$K" != None ] && echo "ANTHROPIC_API_KEY=$K"; echo "AWS_REGION=$REGION"; } > /etc/cert-platform/env
cat > /etc/systemd/system/cert-orch.service <<EOF
[Unit]
Description=Cert orchestrator daily run
[Service]
Type=oneshot
EnvironmentFile=/etc/cert-platform/env
ExecStart=/usr/bin/python3 $P/ops_agents.py orchestrate "일일 운영 점검" --db /var/lib/cert-platform/platform.db
EOF
cat > /etc/systemd/system/cert-orch.timer <<EOF
[Unit]
Description=Daily orchestration 09:05 KST
[Timer]
OnCalendar=*-*-* 00:05:00 UTC
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl disable --now cert-notify.timer 2>/dev/null || true     # 알림은 오케스트레이터가 실행
systemctl enable --now cert-orch.timer
systemctl restart cert-platform
sleep 6
echo "commit $(git rev-parse --short HEAD) · claude key: $([ -n "$K" ] && [ "$K" != None ] && echo set || echo none) · node $(node --version 2>/dev/null)"
curl -s -m5 localhost:8000/api/health

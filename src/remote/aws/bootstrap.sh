#!/bin/bash
# AWS 테스트 서버 부트스트랩 (Amazon Linux 2023) — CloudFormation user-data 가 호출
#   bootstrap.sh platform|web01|was01
# 소스: https://github.com/skybook75/cert-agent (src/ 폴더)
set -euxo pipefail
ROLE="$1"
SRC=/opt/cert-agent-src
dnf install -y python3 python3-cryptography python3-boto3 git openssl
[ -d $SRC ] || git clone --depth 1 https://github.com/skybook75/cert-agent $SRC
P=$SRC/src
hostnamectl set-hostname "$ROLE"

if [ "$ROLE" = platform ]; then
  install -d -m 700 /etc/cert-platform /var/lib/cert-platform
  # 비밀 값은 user-data·저장소에 두지 않음: 있으면 SSM Parameter Store(SecureString)에서 읽어 600 권한 파일에만 기록
  TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
  REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
  : > /etc/cert-platform/env; chmod 600 /etc/cert-platform/env
  K=$(aws ssm get-parameter --region "$REGION" --name /cert-lab/anthropic-api-key --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
  [ -n "$K" ] && [ "$K" != None ] && echo "ANTHROPIC_API_KEY=$K" >> /etc/cert-platform/env
  echo "AWS_REGION=$REGION" >> /etc/cert-platform/env
  cat > /etc/systemd/system/cert-platform.service <<EOF
[Unit]
Description=Cert management platform
After=network-online.target
[Service]
EnvironmentFile=/etc/cert-platform/env
ExecStart=/usr/bin/python3 $P/platform_server.py --host 0.0.0.0 --port 8000 --no-browser --db /var/lib/cert-platform/platform.db --vault-key /etc/cert-platform/vault.key --deploy /var/lib/cert-platform/deploy
Restart=always
[Install]
WantedBy=multi-user.target
EOF
  # 알림 메일 확인용 테스트 SMTP(받은 메일을 파일로 보관, 외부 발송 없음)
  cat > /etc/systemd/system/smtp-sink.service <<EOF
[Unit]
Description=Test SMTP sink
[Service]
ExecStart=/usr/bin/python3 $P/tests/smtp_sink.py 2525 /var/lib/cert-platform/mail.jsonl
Restart=always
[Install]
WantedBy=multi-user.target
EOF
  # 알림 Agent: 매일 09:05(KST) 만료 점검 → 미발송 알림을 이메일·문자·푸시로 발송
  cat > /etc/systemd/system/cert-notify.service <<EOF
[Unit]
Description=Cert notify agent
[Service]
Type=oneshot
EnvironmentFile=/etc/cert-platform/env
ExecStart=/usr/bin/python3 $P/agents.py notify --send --db /var/lib/cert-platform/platform.db
EOF
  cat > /etc/systemd/system/cert-notify.timer <<EOF
[Unit]
Description=Daily cert notify
[Timer]
OnCalendar=*-*-* 00:05:00 UTC
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now cert-platform smtp-sink cert-notify.timer
fi

if [ "$ROLE" = web01 ]; then
  dnf install -y nginx
  python3 $P/remote/aws/lab_certs.py web01 init
  cat > /etc/nginx/conf.d/shop.conf <<'EOF'
server {
  listen 443 ssl;
  server_name shop.demo.test;
  ssl_certificate     /etc/nginx/ssl/shop/fullchain.pem;
  ssl_certificate_key /etc/nginx/ssl/shop/privkey.key;
  location / { return 200 "shop.demo.test (cert lab)\n"; }
}
EOF
  nginx -t
  systemctl enable --now nginx
fi

if [ "$ROLE" = was01 ]; then
  python3 $P/remote/aws/lab_certs.py was01 init
fi

# Agent 설치(설정은 플랫폼에서 서버 등록 후 agent-setup 으로)
install -d /opt/cert-agent
cp $P/remote/cert_agent.py /opt/cert-agent/
cat > /usr/local/bin/agent-setup <<EOF
#!/bin/bash
# agent-setup <플랫폼 URL> <토큰>   — 토큰은 플랫폼 관리 탭에서 서버 등록 시 1회 표시
set -e
install -d -m 700 /etc/cert-agent
case "$ROLE" in
  web01) SCAN='["/etc/nginx/ssl"]'; SVC='{"check":"nginx -t","reload":"systemctl reload nginx","verify":"127.0.0.1:443"}';;
  was01) SCAN='["/opt/tomcat/conf/ssl"]'; SVC='{}';;
  *) SCAN='["/etc/pki/tls/certs"]'; SVC='{}';;
esac
printf '{"platform":"%s","token":"%s","scan":%s,"service":%s}\n' "\$1" "\$2" "\$SCAN" "\$SVC" > /etc/cert-agent/agent.json
chmod 600 /etc/cert-agent/agent.json
cat > /etc/systemd/system/cert-agent.service <<'U'
[Unit]
Description=Cert agent run
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/cert-agent/cert_agent.py run
U
cat > /etc/systemd/system/cert-agent.timer <<'U'
[Unit]
Description=Cert agent every 30 min
[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
[Install]
WantedBy=timers.target
U
systemctl daemon-reload; systemctl enable --now cert-agent.timer
python3 /opt/cert-agent/cert_agent.py run
EOF
chmod 755 /usr/local/bin/agent-setup
echo "bootstrap $ROLE done" > /var/log/cert-lab-bootstrap.done

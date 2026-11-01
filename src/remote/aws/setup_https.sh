#!/bin/bash
# 플랫폼 서버에 HTTPS 적용: Caddy(자동 Let's Encrypt) → 127.0.0.1:8000
# 도메인: cert-platform.<공인IP를-로>.sslip.io  (무료 와일드카드 DNS, 고정 IP 와 함께 사용)
set -euo pipefail
T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -s -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/public-ipv4)
DOMAIN="cert-platform.${IP//./-}.sslip.io"
if ! command -v caddy >/dev/null; then
  curl -fsSL -o /tmp/caddy.tgz https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz
  tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy
  useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy 2>/dev/null || true
fi
install -d /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
  encode gzip
  header Strict-Transport-Security "max-age=31536000"
  reverse_proxy 127.0.0.1:8000
}
EOF
cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy
After=network-online.target
[Service]
User=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=XDG_DATA_HOME=/var/lib/caddy
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable caddy
systemctl restart caddy
sleep 15
curl -s -o /dev/null -w "https://$DOMAIN -> HTTP %{http_code}\n" "https://$DOMAIN/" || journalctl -u caddy -n 20 --no-pager
echo "DOMAIN=$DOMAIN"

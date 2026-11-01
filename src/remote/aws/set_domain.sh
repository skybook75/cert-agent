#!/bin/bash
# 플랫폼 도메인 지정(Caddy 가 Let's Encrypt 인증서 자동 발급) + 시나리오 결과(캡처·영상) 열람 경로
#   set_domain.sh certagent.duckdns.org
set -euo pipefail
DOMAIN="$1"
T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -s -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/public-ipv4)
ALT="cert-platform.${IP//./-}.sslip.io"
install -d -m 755 /var/lib/cert-lab-run/out
[ -f /var/lib/cert-lab-run/results_path ] || head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n' > /var/lib/cert-lab-run/results_path
RP=$(cat /var/lib/cert-lab-run/results_path)
chmod 755 /var/lib/cert-lab-run
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN, $ALT {
  encode gzip
  header Strict-Transport-Security "max-age=31536000"
  handle_path /lab-$RP/* {
    root * /var/lib/cert-lab-run/out
    file_server browse
  }
  reverse_proxy 127.0.0.1:8000
}
EOF
systemctl restart caddy
for i in $(seq 18); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/api/health" || true)
  [ "$code" = 200 ] && break
  sleep 10
done
echo "https://$DOMAIN -> $code"
echo "RESULTS https://$DOMAIN/lab-$RP/"

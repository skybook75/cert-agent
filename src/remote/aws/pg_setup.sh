#!/bin/bash
# 플랫폼 저장소를 SQLite → PostgreSQL 로 이전 (플랫폼 서버에서 root 로 1회 실행, 다시 실행해도 안전)
#   1) PostgreSQL 설치·초기화 — 이 서버 안에서만 접속(127.0.0.1), SCRAM-SHA-256 인증
#   2) 전용 계정·DB 생성, 접속 정보는 /etc/cert-platform/db.env(600)에만 저장
#   3) 플랫폼 잠시 중지 → 전 문서 복사 → 건수·내용 전수 검증 → 통과할 때만 전환 (실패 시 기존 SQLite 로 재시작)
#   4) 매일 03:30(KST) pg_dump 백업, 14일 보관
#   되돌리기: /etc/systemd/system/cert-platform.service.d/db.conf 삭제 → systemctl daemon-reload → restart (SQLite 파일은 그대로 보존)
set -euo pipefail
P=/opt/cert-agent-src/src
SQLITE=/var/lib/cert-platform/platform.db
ENVF=/etc/cert-platform/db.env

if ! command -v pg_ctl >/dev/null && ! ls /usr/bin/postgresql-setup >/dev/null 2>&1; then
  dnf install -y -q postgresql16-server >/dev/null 2>&1 || dnf install -y -q postgresql15-server >/dev/null
fi
[ -f /var/lib/pgsql/data/PG_VERSION ] || postgresql-setup --initdb >/dev/null
HBA=/var/lib/pgsql/data/pg_hba.conf
sed -i -E 's/^(host\s+all\s+all\s+(127\.0\.0\.1\/32|::1\/128)\s+)(ident|md5|trust)/\1scram-sha-256/' "$HBA"
CONF=/var/lib/pgsql/data/postgresql.conf
grep -q "^listen_addresses = 'localhost'" "$CONF" || echo "listen_addresses = 'localhost'" >> "$CONF"
grep -q "^password_encryption = scram-sha-256" "$CONF" || echo "password_encryption = scram-sha-256" >> "$CONF"
systemctl enable -q --now postgresql
systemctl restart postgresql
sleep 2

if [ ! -f "$ENVF" ]; then
  PW=$(openssl rand -hex 24)
  sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='certplatform') THEN CREATE ROLE certplatform LOGIN; END IF; END \$\$;
ALTER ROLE certplatform PASSWORD '$PW';
SQL
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='certplatform'" | grep -q 1 || sudo -u postgres createdb -O certplatform certplatform
  umask 077
  echo "CERT_DB=postgresql://certplatform:$PW@127.0.0.1:5432/certplatform" > "$ENVF"
  unset PW
fi
set -a; . "$ENVF"; set +a

# 이전 + 검증 (플랫폼 중지 상태에서 — 이전 중 쓰기 없음)
systemctl stop cert-platform
cp -p "$SQLITE" "$SQLITE.pre-pg-$(date +%Y%m%d%H%M)"
if ! python3 "$P/tools/migrate_to_pg.py" "$SQLITE" > /var/lib/cert-platform/migrate_result.json; then
  echo "MIGRATION_CHECK_FAILED → SQLite 로 재시작"; cat /var/lib/cert-platform/migrate_result.json | cut -c1-400
  systemctl start cert-platform; exit 1
fi
cut -c1-600 /var/lib/cert-platform/migrate_result.json

# 플랫폼·정기 점검이 PostgreSQL 을 쓰도록 전환 (--db 지정 제거, 접속 정보는 환경 파일로만 전달)
sed -i 's# --db /var/lib/cert-platform/platform.db##' /etc/systemd/system/cert-platform.service
for u in cert-platform cert-orch; do
  mkdir -p /etc/systemd/system/$u.service.d
  printf '[Service]\nEnvironmentFile=%s\n' "$ENVF" > /etc/systemd/system/$u.service.d/db.conf
done
sed -i 's# --db /var/lib/cert-platform/platform.db##' /etc/systemd/system/cert-orch.service 2>/dev/null || true

# 매일 백업 (pg_dump 사용자 지정 형식, 14일 보관)
install -d -o postgres -g postgres -m 700 /var/lib/pgsql/backups
cat > /etc/systemd/system/cert-pgbackup.service <<'EOF'
[Unit]
Description=Cert platform PostgreSQL daily backup
[Service]
Type=oneshot
User=postgres
ExecStart=/bin/bash -c 'pg_dump -Fc certplatform -f /var/lib/pgsql/backups/certplatform-$(date +%%Y%%m%%d).dump && find /var/lib/pgsql/backups -name "*.dump" -mtime +14 -delete'
EOF
cat > /etc/systemd/system/cert-pgbackup.timer <<'EOF'
[Unit]
Description=Cert platform PostgreSQL backup 03:30 KST
[Timer]
OnCalendar=*-*-* 18:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable -q --now cert-pgbackup.timer
systemctl start cert-platform
systemctl start cert-pgbackup.service || echo 'BACKUP_FIRST_RUN_FAILED'
sleep 5
echo "health: $(curl -s -m5 localhost:8000/api/health)"
echo "backup: $(ls -la /var/lib/pgsql/backups | tail -1)"
echo "pg: $(sudo -u postgres psql -tAc "select version()" | cut -c1-40) · docs $(sudo -u postgres psql -d certplatform -tAc 'select count(*) from docs')"

#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# 인증서 관리 플랫폼 — 폐쇄망 설치 스크립트 (RHEL / Rocky / Alma 8·9, Amazon Linux 2023)
#
#   sudo ./install/platform_install.sh                      # 기본: SQLite, http://<서버IP>:8000
#   sudo ./install/platform_install.sh --db postgres        # PostgreSQL(같은 서버, 127.0.0.1 전용)
#   sudo ./install/platform_install.sh --vector             # Qdrant 벡터 DB + 임베딩(하이브리드 검색)
#   sudo ./install/platform_install.sh --https /path/server.crt /path/server.key
#                                                           # nginx 로 HTTPS(사내 인증서) → 플랫폼은 127.0.0.1 전용
#   옵션은 함께 쓸 수 있습니다. 다시 실행해도 안전합니다(설정·데이터·키는 유지).
#
# 인터넷 연결은 쓰지 않습니다. OS 패키지(python3, postgresql-server, nginx)는 사내 저장소(yum/dnf 미러·ISO)에서,
# 그 외 파일은 이 묶음 폴더(bin/, wheels/, models/)에서 설치합니다.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

B="$(cd "$(dirname "$0")/.." && pwd)"          # 묶음 최상위 폴더
APP=/opt/cert-agent                            # 프로그램
DATA=/var/lib/cert-platform                    # 데이터(DB·배포 폴더·모델)
ETC=/etc/cert-platform                         # 설정·비밀 값(600)
SVC_USER=certplat
PORT=8000
DB=sqlite; VECTOR=0; CRT=""; KEY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    --vector) VECTOR=1; shift;;
    --https) CRT="$2"; KEY="$3"; shift 3;;
    --port) PORT="$2"; shift 2;;
    -h|--help) sed -n 2,14p "$0"; exit 0;;
    *) echo "알 수 없는 옵션: $1"; exit 2;;
  esac
done

say(){ printf '\n\033[1;36m[%s]\033[0m %s\n' "$1" "$2"; }
die(){ printf '\n\033[1;31m[중단]\033[0m %s\n' "$1"; exit 1; }
[ "$(id -u)" = 0 ] || die "root 권한으로 실행하세요: sudo $0 $*"
[ -f "$B/src/platform_server.py" ] || die "묶음 폴더 구조가 다릅니다($B/src 없음). 압축을 푼 폴더 안에서 실행하세요."
case "$DB" in sqlite|postgres) ;; *) die "--db 는 sqlite 또는 postgres";; esac
if [ -n "$CRT" ]; then [ -f "$CRT" ] && [ -f "$KEY" ] || die "--https 인증서/키 파일이 없습니다: $CRT $KEY"; fi

# ── 1. Python 3.9 이상 ───────────────────────────────────────────────────────
say 1/7 "Python 확인"
PY=""
for c in python3.12 python3.11 python3.9 python3; do
  if command -v $c >/dev/null && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then PY=$(command -v $c); break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 이상이 없어 사내 저장소에서 설치합니다(dnf)."
  dnf install -y python3.11 2>/dev/null || dnf install -y python39 2>/dev/null || dnf install -y python3 || die "Python 3.9+ 설치 실패: 사내 dnf 저장소를 확인하세요"
  for c in python3.11 python3.9 python3; do command -v $c >/dev/null && $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' && { PY=$(command -v $c); break; }; done
fi
[ -n "$PY" ] || die "Python 3.9 이상을 찾지 못했습니다"
PYV=$($PY -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')
echo "사용: $PY ($($PY -V))"
# OS 패키지 cryptography 가 있으면 그대로 사용(없으면 아래 wheels/ 에서 설치)
dnf install -y "python${PYV:0:1}.${PYV:1}-cryptography" >/dev/null 2>&1 || dnf install -y python3-cryptography >/dev/null 2>&1 || true

# ── 2. 서비스 계정·폴더·프로그램 복사 ────────────────────────────────────────
say 2/7 "프로그램 설치 → $APP"
id $SVC_USER >/dev/null 2>&1 || useradd -r -s /sbin/nologin -d $DATA $SVC_USER
install -d -o $SVC_USER -g $SVC_USER -m 750 $APP $DATA $DATA/deploy $DATA/models
install -d -o $SVC_USER -g $SVC_USER -m 700 $ETC
if [ -d $APP/src ]; then cp -a $APP/src "$APP/src.bak.$(date +%Y%m%d%H%M%S)"; fi     # 업그레이드 시 이전 버전 보관
rm -rf $APP/src.new && cp -a "$B/src" $APP/src.new && rm -rf $APP/src && mv $APP/src.new $APP/src
chown -R $SVC_USER:$SVC_USER $APP    # 유지보수 Agent 가 승인된 수정만 반영(백업 후)하므로 서비스 계정 소유

# ── 3. 가상환경 + 오프라인 패키지 ───────────────────────────────────────────
say 3/7 "Python 가상환경(오프라인 패키지)"
[ -x $APP/venv/bin/python ] || $PY -m venv --system-site-packages $APP/venv
VPY=$APP/venv/bin/python
WH="$B/wheels/py$PYV"
pipoff(){ $VPY -m pip install -q --no-index --find-links "$WH" "$@"; }
if ! $VPY -c 'import cryptography' 2>/dev/null; then
  [ -d "$WH" ] || die "cryptography 가 없습니다. OS 패키지(python3-cryptography)를 설치하거나, 인터넷 PC에서 prepare_online 스크립트로 wheels/py$PYV 를 채워 오세요."
  pipoff cryptography || die "wheels/py$PYV 에서 cryptography 설치 실패"
fi
echo "cryptography $($VPY -c 'import cryptography; print(cryptography.__version__)')"
chown -R $SVC_USER:$SVC_USER $APP/venv

# ── 4. 설정 파일(비밀 값은 600 파일에만) ─────────────────────────────────────
say 4/7 "설정 파일 $ETC/env"
if [ ! -f $ETC/env ]; then
  umask 077
  cat > $ETC/env <<EOF
# 인증서 관리 플랫폼 환경 설정 — 수정 후: systemctl restart cert-platform
# 사내 LLM 게이트웨이(Anthropic Messages API 호환)가 있으면 아래 두 줄을 채우세요. 비워 두면 QA 챗봇은 가이드 원문 검색형 답변으로 동작합니다.
#ANTHROPIC_BASE_URL=https://llm-gateway.corp.local
#ANTHROPIC_API_KEY=
#CLAUDE_MODEL=claude-sonnet-4-6
# 메일은 플랫폼 '관리 > 메일(SMTP)' 화면에서 사내 SMTP 를 설정합니다. SMTP 계정 비밀번호가 필요하면:
#SMTP_PASSWORD=
# 사내 ntfy 서버를 쓰면(휴대폰 푸시):
#NTFY_URL=https://ntfy.corp.local
CERT_STATE_DIR=$DATA
EMBED_CACHE=$DATA/models
HF_HUB_OFFLINE=1
TZ=Asia/Seoul
EOF
fi
grep -q '^CERT_VECTOR=' $ETC/env || echo "CERT_VECTOR=$VECTOR" >> $ETC/env
[ "$VECTOR" = 1 ] && sed -i 's/^CERT_VECTOR=0/CERT_VECTOR=1/' $ETC/env
chown $SVC_USER:$SVC_USER $ETC/env; chmod 600 $ETC/env

# ── 5. 데이터베이스 ─────────────────────────────────────────────────────────
say 5/7 "데이터베이스: $DB"
DBOPT="--db $DATA/platform.db"
if [ "$DB" = postgres ]; then
  if ! command -v postgresql-setup >/dev/null; then
    dnf install -y postgresql-server || die "postgresql-server 설치 실패(사내 dnf 저장소 확인)"
  fi
  PGDATA=/var/lib/pgsql/data
  [ -f $PGDATA/PG_VERSION ] || postgresql-setup --initdb
  sed -i -E 's/^(host\s+all\s+all\s+(127\.0\.0\.1\/32|::1\/128)\s+)(ident|md5|trust)/\1scram-sha-256/' $PGDATA/pg_hba.conf
  grep -q "^listen_addresses = 'localhost'" $PGDATA/postgresql.conf || echo "listen_addresses = 'localhost'" >> $PGDATA/postgresql.conf
  grep -q "^password_encryption = scram-sha-256" $PGDATA/postgresql.conf || echo "password_encryption = scram-sha-256" >> $PGDATA/postgresql.conf
  systemctl enable -q --now postgresql; systemctl restart postgresql; sleep 2
  if [ ! -f $ETC/db.env ]; then
    PW=$(openssl rand -hex 24 2>/dev/null || $PY -c 'import secrets;print(secrets.token_hex(24))')
    sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='certplatform') THEN CREATE ROLE certplatform LOGIN; END IF; END \$\$;
ALTER ROLE certplatform PASSWORD '$PW';
SQL
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='certplatform'" | grep -q 1 || sudo -u postgres createdb -O certplatform certplatform
    ( umask 077; echo "CERT_DB=postgresql://certplatform:$PW@127.0.0.1:5432/certplatform" > $ETC/db.env )
    unset PW
  fi
  chown $SVC_USER:$SVC_USER $ETC/db.env; chmod 600 $ETC/db.env
  DBOPT=""     # CERT_DB(db.env) 를 플랫폼이 읽음
  # 매일 03:30 백업(14일 보관)
  install -d -o postgres -g postgres -m 700 /var/backups/cert-platform
  cat > /etc/cron.d/cert-platform-pgdump <<'EOF'
30 3 * * * postgres pg_dump -Fc certplatform > /var/backups/cert-platform/certplatform-$(date +\%Y\%m\%d).dump && find /var/backups/cert-platform -name '*.dump' -mtime +14 -delete
EOF
else
  # SQLite: 매일 03:30 파일 백업(14일 보관)
  install -d -o $SVC_USER -g $SVC_USER -m 700 /var/backups/cert-platform
  cat > /etc/cron.d/cert-platform-backup <<EOF
30 3 * * * $SVC_USER $VPY -c "import sqlite3,sys; s=sqlite3.connect('$DATA/platform.db'); d=sqlite3.connect('/var/backups/cert-platform/platform-'+__import__('time').strftime('%Y%m%d')+'.db'); s.backup(d)" && find /var/backups/cert-platform -name 'platform-*.db' -mtime +14 -delete
EOF
fi

# ── 6. 벡터 DB(선택) ────────────────────────────────────────────────────────
if [ "$VECTOR" = 1 ]; then
  say 6/7 "Qdrant 벡터 DB + 임베딩 모델"
  [ -x "$B/bin/qdrant" ] || die "bin/qdrant 가 없습니다"
  install -m 755 "$B/bin/qdrant" /usr/local/bin/qdrant
  id qdrant >/dev/null 2>&1 || useradd -r -s /sbin/nologin -d /var/lib/qdrant qdrant
  install -d -o qdrant -g qdrant /var/lib/qdrant /var/lib/qdrant/storage /var/lib/qdrant/snapshots /etc/qdrant
  cat > /etc/qdrant/config.yaml <<'EOF'
service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
storage:
  storage_path: /var/lib/qdrant/storage
  snapshots_path: /var/lib/qdrant/snapshots
telemetry_disabled: true
EOF
  cat > /etc/systemd/system/qdrant.service <<'EOF'
[Unit]
Description=Qdrant vector DB (local only)
After=network-online.target
[Service]
User=qdrant
WorkingDirectory=/var/lib/qdrant
ExecStart=/usr/local/bin/qdrant --config-path /etc/qdrant/config.yaml
Restart=always
MemoryMax=600M
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload; systemctl enable -q --now qdrant
  if ! $VPY -c 'import fastembed' 2>/dev/null; then
    [ -d "$WH" ] || die "wheels/py$PYV 가 없습니다(fastembed). 인터넷 PC에서 prepare_online 스크립트로 채워 오세요."
    pipoff fastembed || die "fastembed 오프라인 설치 실패"
  fi
  if [ -d "$B/models" ] && [ -n "$(ls -A "$B/models" 2>/dev/null)" ]; then cp -a "$B/models/." $DATA/models/; fi
  chown -R $SVC_USER:$SVC_USER $DATA/models $APP/venv
  for i in $(seq 30); do curl -sf http://127.0.0.1:6333/readyz >/dev/null && break; sleep 1; done
  sudo -u $SVC_USER env EMBED_CACHE=$DATA/models HF_HUB_OFFLINE=1 $VPY $APP/src/rag_vector.py index || die "가이드 색인 실패: models/ 폴더에 임베딩 모델이 있는지 확인하세요"
else
  say 6/7 "벡터 DB 생략(BM25 검색으로 동작 · 나중에 --vector 로 추가 가능)"
fi

# ── 7. 서비스 등록 ──────────────────────────────────────────────────────────
say 7/7 "서비스 등록(systemd)"
HOST=0.0.0.0; [ -n "$CRT" ] && HOST=127.0.0.1
EXTRA_ENV=""; [ "$DB" = postgres ] && EXTRA_ENV="EnvironmentFile=$ETC/db.env"
AFTER="network-online.target"; [ "$DB" = postgres ] && AFTER="$AFTER postgresql.service"; [ "$VECTOR" = 1 ] && AFTER="$AFTER qdrant.service"
cat > /etc/systemd/system/cert-platform.service <<EOF
[Unit]
Description=Cert management platform
After=$AFTER
[Service]
User=$SVC_USER
EnvironmentFile=$ETC/env
$EXTRA_ENV
WorkingDirectory=$APP/src
ExecStart=$VPY $APP/src/platform_server.py --host $HOST --port $PORT --no-browser $DBOPT --vault-key $ETC/vault.key --deploy $DATA/deploy
Restart=always
NoNewPrivileges=yes
ProtectSystem=full
ReadWritePaths=$APP $DATA $ETC
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/cert-notify.service <<EOF
[Unit]
Description=Cert notify agent (D-30/D-7/expired)
[Service]
Type=oneshot
User=$SVC_USER
EnvironmentFile=$ETC/env
$EXTRA_ENV
ExecStart=$VPY $APP/src/agents.py notify --send $DBOPT
EOF
cat > /etc/systemd/system/cert-notify.timer <<'EOF'
[Unit]
Description=Daily cert notify 09:05 KST
[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable -q cert-platform cert-notify.timer
systemctl restart cert-platform
systemctl start cert-notify.timer

# HTTPS(선택): nginx 리버스 프록시 + 사내 인증서
URL="http://$(hostname -I | awk '{print $1}'):$PORT"
if [ -n "$CRT" ]; then
  command -v nginx >/dev/null || dnf install -y nginx || die "nginx 설치 실패(사내 dnf 저장소 확인)"
  install -d -m 700 /etc/nginx/ssl/cert-platform
  install -m 644 "$CRT" /etc/nginx/ssl/cert-platform/server.crt
  install -m 600 "$KEY" /etc/nginx/ssl/cert-platform/server.key
  cat > /etc/nginx/conf.d/cert-platform.conf <<EOF
server {
  listen 443 ssl;
  server_name _;
  ssl_certificate     /etc/nginx/ssl/cert-platform/server.crt;
  ssl_certificate_key /etc/nginx/ssl/cert-platform/server.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  client_max_body_size 20m;
  add_header Strict-Transport-Security "max-age=31536000" always;
  location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 180s;
  }
}
server { listen 80; server_name _; return 301 https://\$host\$request_uri; }
EOF
  command -v setsebool >/dev/null && setsebool -P httpd_can_network_connect 1 2>/dev/null || true
  nginx -t && systemctl enable -q --now nginx && systemctl reload nginx
  URL="https://$(hostname -f 2>/dev/null || hostname)"
fi

# 방화벽(firewalld 사용 중일 때만)
if systemctl is-active -q firewalld 2>/dev/null; then
  if [ -n "$CRT" ]; then firewall-cmd -q --permanent --add-service=https --add-service=http; else firewall-cmd -q --permanent --add-port=$PORT/tcp; fi
  firewall-cmd -q --reload
fi

# 상태 확인
for i in $(seq 20); do curl -sf -o /dev/null http://127.0.0.1:$PORT/ && break; sleep 1; done
if curl -sf -o /dev/null http://127.0.0.1:$PORT/; then
  printf '\n\033[1;32m설치 완료\033[0m\n'
  echo "  접속 주소 : $URL"
  echo "  처음 접속 : '최초 관리자 계정 만들기' 화면이 뜹니다 → 바로 관리자 계정을 만드세요(첫 계정만 만들 수 있음)."
  echo "  상태 확인 : systemctl status cert-platform   로그: journalctl -u cert-platform -f"
  echo "  설정 파일 : $ETC/env  (LLM 게이트웨이·SMTP 비밀번호 등)"
  echo "  보관함 키 : $ETC/vault.key  ← DB 와 따로 백업하세요(잃어버리면 PFX 비밀번호를 다시 등록해야 함)"
else
  journalctl -u cert-platform -n 40 --no-pager || true
  die "플랫폼이 응답하지 않습니다. 위 로그를 확인하세요."
fi

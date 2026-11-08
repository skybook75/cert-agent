#!/bin/bash
# 벡터 DB 설치: Qdrant(127.0.0.1 전용) + 임베딩 모델(fastembed, CPU) → 가이드 색인 → 플랫폼 재시작
set -uo pipefail
SRC=/opt/cert-agent-src; P=$SRC/src
cd $SRC && git pull -q --ff-only || true
# 메모리 여유(t3.small 2GB): 스왑 1GB 없으면 추가
if ! swapon --show | grep -q .; then fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile && echo '/swapfile none swap defaults 0 0' >> /etc/fstab; fi
# Qdrant 바이너리
if [ ! -x /usr/local/bin/qdrant ]; then
  cd /tmp && curl -sSL -o qdrant.tgz https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-musl.tar.gz \
    && tar xzf qdrant.tgz && install -m 755 qdrant /usr/local/bin/qdrant && rm -f qdrant qdrant.tgz
fi
id qdrant >/dev/null 2>&1 || useradd -r -s /sbin/nologin -d /var/lib/qdrant qdrant
mkdir -p /var/lib/qdrant/storage /var/lib/qdrant/snapshots /etc/qdrant && chown -R qdrant: /var/lib/qdrant
cat > /etc/qdrant/config.yaml <<CFG
service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
storage:
  storage_path: /var/lib/qdrant/storage
  snapshots_path: /var/lib/qdrant/snapshots
telemetry_disabled: true
CFG
cat > /etc/systemd/system/qdrant.service <<UNIT
[Unit]
Description=Qdrant vector DB (local only)
After=network-online.target
[Service]
User=qdrant
WorkingDirectory=/var/lib/qdrant
ExecStart=/usr/local/bin/qdrant --config-path /etc/qdrant/config.yaml
Restart=always
MemoryMax=400M
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable -q --now qdrant
for i in $(seq 30); do curl -sf http://127.0.0.1:6333/readyz >/dev/null && break; sleep 1; done
echo "qdrant: $(/usr/local/bin/qdrant --version 2>/dev/null | head -1) ready=$(curl -s http://127.0.0.1:6333/readyz)"
# 임베딩 라이브러리 + 모델 캐시
python3 -c "import fastembed" 2>/dev/null || python3 -m pip install -q fastembed 2>&1 | grep -v WARNING | tail -2
mkdir -p /var/lib/cert-platform/models
python3 -c "import fastembed;print('fastembed', fastembed.__version__)"
cd $P && EMBED_CACHE=/var/lib/cert-platform/models python3 rag_vector.py index
systemctl restart cert-platform && sleep 2 && systemctl is-active cert-platform
free -m | head -3

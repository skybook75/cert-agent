#!/bin/bash
# 배포 묶음 만들기 (개발자용): dist/ 에 폐쇄망 설치 묶음(tar.gz)과 Agent 단독 묶음(zip)을 만든다.
#   ./offline/make_bundle.sh 1.0.0
set -euo pipefail
VER="${1:-1.0.0}"
R="$(cd "$(dirname "$0")/.." && pwd)"
QV=1.12.4
OUT="$R/dist"; N="cert-agent-offline-$VER"; W="$OUT/$N"
rm -rf "$W" && mkdir -p "$W"/{bin,wheels,models,windows,agent}

# 1) 소스 — 실행에 필요한 것만(테스트·실험 결과·AWS 실습용 스크립트 제외)
mkdir -p "$W/src"
tar -C "$R/src" --exclude='__pycache__' --exclude='./results' --exclude='./tests' --exclude='./remote/aws' \
    --exclude='*.db' --exclude='*.key' --exclude='*.sessions' --exclude='*.bat' --exclude='_gate*' -cf - . | tar -C "$W/src" -xf -
# 2) 설치 스크립트·가이드
cp -a "$R/offline/install" "$R/offline/prepare" "$W/"
cp "$R/offline/README.md" "$W/README.md"
echo "$VER" > "$W/VERSION"
chmod 755 "$W"/install/*.sh "$W"/prepare/*.sh
# 3) Qdrant(정적 빌드, x86_64) — 공식 GitHub 릴리스
QT="$OUT/.cache/qdrant-$QV.tgz"; mkdir -p "$OUT/.cache"
[ -f "$QT" ] || curl -fsSL -o "$QT" "https://github.com/qdrant/qdrant/releases/download/v$QV/qdrant-x86_64-unknown-linux-musl.tar.gz"
tar xzf "$QT" -C "$W/bin" qdrant && chmod 755 "$W/bin/qdrant"
# 4) 빈 폴더 안내(인터넷 PC에서 prepare_online 으로 채움)
printf '이 폴더는 인터넷 PC에서 prepare/prepare_online.(sh|ps1) 을 실행하면 채워집니다.\nOS 패키지 python3-cryptography 가 있고 벡터 DB 를 안 쓰면 비워 둬도 됩니다.\n' > "$W/wheels/README.txt"
printf '임베딩 모델(약 230MB) — 벡터 DB(--vector)를 쓸 때만 필요합니다. prepare_online 으로 채웁니다.\n' > "$W/models/README.txt"
printf 'Windows Agent 용 Python 설치 파일과 cryptography 휠 — prepare_online 으로 채웁니다.\n' > "$W/windows/README.txt"
# 5) Agent 단독 묶음(각 서버에 복사할 것)
cp "$R/src/remote/cert_agent.py" "$R/offline/install/agent_install.sh" "$R/offline/install/agent_install.ps1" "$W/agent/"
cp "$R/offline/AGENT_README.txt" "$W/agent/README.txt"
# PowerShell 5.1 이 한글을 바르게 읽도록 UTF-8 BOM 추가
for f in "$W"/install/*.ps1 "$W"/prepare/*.ps1 "$W"/agent/*.ps1; do
  head -c3 "$f" | grep -q $'\xef\xbb\xbf' || { printf '\xef\xbb\xbf' | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"; }
done
# 6) 체크섬·압축
( cd "$W" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS )
( cd "$OUT" && tar czf "$N.tar.gz" "$N" )
( cd "$W" && rm -f "$OUT/cert-agent-agent-$VER.zip" && zip -qr "$OUT/cert-agent-agent-$VER.zip" agent )
( cd "$OUT" && sha256sum "$N.tar.gz" "cert-agent-agent-$VER.zip" > "SHA256SUMS-$VER.txt" )
ls -l "$OUT"/*.tar.gz "$OUT"/*.zip "$OUT"/SHA256SUMS-*.txt
# 7) GitHub Pages 내려받기 폴더로 복사(저장소 download/)
mkdir -p "$R/download" && cp "$OUT/$N.tar.gz" "$OUT/cert-agent-agent-$VER.zip" "$OUT/SHA256SUMS-$VER.txt" "$R/download/"

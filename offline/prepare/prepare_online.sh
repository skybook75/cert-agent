#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# [인터넷이 되는 PC에서 1회 실행] 폐쇄망 반입용 추가 파일 채우기 (Linux / macOS / WSL)
#   ./prepare/prepare_online.sh            → wheels/py39·py311·py312, models/, windows/ 를 채움
#   ./prepare/prepare_online.sh --no-vector  (벡터 DB 를 안 쓰면: cryptography 만, 모델 생략)
#
# 필요: python3(3.9+) 와 pip, 인터넷. 채운 뒤 묶음 폴더 전체를 반입 매체로 옮기세요.
# 받는 곳: PyPI(휠), Hugging Face(임베딩 모델), python.org(Windows Agent 용 Python 설치 파일)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
B="$(cd "$(dirname "$0")/.." && pwd)"
VECTOR=1; [ "${1:-}" = "--no-vector" ] && VECTOR=0
PKGS="cryptography"; [ $VECTOR = 1 ] && PKGS="cryptography fastembed"
PYWIN=3.12.7
MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

for V in 3.9 3.11 3.12; do
  T="$B/wheels/py${V/./}"; mkdir -p "$T"
  echo "== Linux x86_64 휠: Python $V → $T"
  python3 -m pip download -q --only-binary=:all: --implementation cp --python-version "$V" \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_27_x86_64 --platform manylinux_2_24_x86_64 --platform linux_x86_64 \
    -d "$T" $PKGS
done

echo "== Windows Agent: Python $PYWIN 설치 파일 + cryptography 휠"
mkdir -p "$B/windows/wheels"
curl -fL -o "$B/windows/python-$PYWIN-amd64.exe" "https://www.python.org/ftp/python/$PYWIN/python-$PYWIN-amd64.exe"
python3 -m pip download -q --only-binary=:all: --implementation cp --python-version 3.12 --platform win_amd64 -d "$B/windows/wheels" cryptography

if [ $VECTOR = 1 ]; then
  echo "== 임베딩 모델: $MODEL → models/"
  TMPV=$(mktemp -d); python3 -m venv "$TMPV"; "$TMPV/bin/python" -m pip install -q fastembed
  "$TMPV/bin/python" - "$B/models" "$MODEL" <<'PY'
import sys
from fastembed import TextEmbedding
m = TextEmbedding(model_name=sys.argv[2], cache_dir=sys.argv[1])
print("model ok, dim =", len(next(iter(m.embed(["인증서 갱신 테스트"])))))
PY
  rm -rf "$TMPV"
fi
( cd "$B" && find wheels windows models -type f 2>/dev/null | sort | xargs sha256sum > SHA256SUMS.prepared )
echo "완료: $(du -sh "$B" | cut -f1) — 묶음 폴더 전체를 반입하세요. 반입 후 'sha256sum -c SHA256SUMS.prepared' 로 무결성 확인."

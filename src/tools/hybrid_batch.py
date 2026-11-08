"""QA 평가용: 질문 여러 개를 한 번에 하이브리드 검색(벡터 DB + BM25, RRF). stdin [{id,q,issuer}] → stdout {id: [chunk_id…]}"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_vector as R  # noqa: E402

out = {}
for x in json.load(sys.stdin):
    r = R.hybrid(x["q"], k=int(x.get("k", 4)), issuer=x.get("issuer"))
    out[x["id"]] = {"mode": r["mode"], "ids": [h["id"] for h in r["hits"]]}
print(json.dumps(out))

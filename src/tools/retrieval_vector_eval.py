"""검색 방식 비교: BM25(한글 2-gram) vs 벡터 DB(Qdrant, 다국어 임베딩) vs 하이브리드(RRF).
같은 평가셋(qa_eval_set.json 중 정답 가이드가 있는 20문항), 같은 청크(가이드 소단원 38개), 문서 단위 정답 판정.

  python3 tools/retrieval_vector_eval.py [결과.json]      (플랫폼 서버에서: Qdrant 실행 중이어야 함)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_vector as R  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
ev = [x for x in json.loads((BASE / "tests/qa_eval_set.json").read_text(encoding="utf-8")) if x["docs"]]
print("index:", json.dumps(R.index(), ensure_ascii=False))
doc = {c["id"]: c["doc"] for c in R.load_chunks()}
bm = R.BM25(R.load_chunks())


def docs_of(ranked, k):
    out = []
    for cid, _ in ranked:
        d = doc[cid]
        out.append(d)
    return out[:k]


methods = {
    "BM25 한글 2-gram": lambda q: bm.search(q, 10),
    "벡터 DB(Qdrant)": lambda q: R.vector_search(q, 10),
    "하이브리드(BM25+벡터, RRF)": lambda q: R.rrf(bm.search(q, 10), R.vector_search(q, 10)),
}
R.vector_search("warmup", 1)
rows = []
for name, fn in methods.items():
    t0 = time.time()
    res = {x["id"]: fn(x["q"]) for x in ev}
    ms = (time.time() - t0) / len(ev) * 1000
    row = {"method": name, "n": len(ev), "query_ms": round(ms, 1)}
    for k in (1, 2, 4):
        hit = sum(1 for x in ev if set(docs_of(res[x["id"]], k)) & set(x["docs"]))
        row[f"top{k}"] = round(hit / len(ev) * 100, 1)
    mrr = 0.0
    for x in ev:
        d = docs_of(res[x["id"]], 4)
        r = next((i for i, v in enumerate(d) if v in x["docs"]), -1)
        mrr += 1 / (r + 1) if r >= 0 else 0
    row["mrr"] = round(mrr / len(ev), 3)
    row["miss_top1"] = [f"{x['id']} {x['q']} → {docs_of(res[x['id']], 2)} (정답 {x['docs']})" for x in ev if docs_of(res[x["id"]], 1)[:1] and docs_of(res[x["id"]], 1)[0] not in x["docs"]]
    rows.append(row)
out = {"model": R.MODEL, "collection": R.COLLECTION, "rows": rows}
print(json.dumps(out, ensure_ascii=False, indent=1))
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

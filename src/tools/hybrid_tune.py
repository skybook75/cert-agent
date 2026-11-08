"""하이브리드 가중치 점검: 벡터 가중치 w 별 문서 단위 지표(20문항) + 청크 단위 기대 근거 포함 여부."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_vector as R
from fastembed import TextEmbedding
print("multilingual models:", [m["model"] for m in TextEmbedding.list_supported_models() if "multilingual" in m["model"].lower() or "m3" in m["model"].lower()])
BASE = Path(__file__).resolve().parent.parent
ev = [x for x in json.loads((BASE / "tests/qa_eval_set.json").read_text(encoding="utf-8")) if x["docs"]]
doc = {c["id"]: c["doc"] for c in R.load_chunks()}
bm = R.BM25(R.load_chunks())
B = {x["id"]: bm.search(x["q"], 10) for x in ev}
V = {x["id"]: R.vector_search(x["q"], 10) for x in ev}
q11 = next(x for x in json.loads((BASE / "tests/qa_eval_set.json").read_text(encoding="utf-8")) if x["id"] == "Q11")
b11, v11 = bm.search(q11["q"], 10), R.vector_search(q11["q"], 10)
print("Q11 bm25:", [c for c, _ in b11[:6]], "vec:", [c for c, _ in v11[:6]])
for w in (0.0, 0.3, 0.5, 0.7, 1.0):
    t1 = t4 = 0; mrr = 0
    for x in ev:
        f = R.rrf(B[x["id"]], V[x["id"]], weights=[1, w]) if w else B[x["id"]]
        d = [doc[c] for c, _ in f][:4]
        t1 += d[0] in x["docs"]; t4 += bool(set(d) & set(x["docs"]))
        r = next((i for i, v in enumerate(d) if v in x["docs"]), -1); mrr += 1 / (r + 1) if r >= 0 else 0
    f11 = [c for c, _ in (R.rrf(b11, v11, weights=[1, w]) if w else b11)][:4]
    print(f"w={w}: top1 {t1/len(ev)*100:.0f}% top4 {t4/len(ev)*100:.0f}% mrr {mrr/len(ev):.3f} | Q11 top4 {f11}")

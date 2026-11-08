"""RAG 검색: 벡터 DB(Qdrant) + BM25 하이브리드.

- 청크: 가이드 소단원 1개 = 1청크 (id G02-1 …, web/core.js buildIndex 와 같은 단위·같은 id)
- 임베딩: 다국어 문장 임베딩 모델(fastembed, ONNX, CPU) — 서버 안에서 계산, 외부 API 키 불필요
- 벡터 DB: Qdrant (127.0.0.1:6333, 코사인 유사도) — REST 로 호출
- BM25: 한글 2글자 묶음 토큰(web/core.js tokens 와 같은 규칙) + 발급기관 가중치
- 합치기: RRF(Reciprocal Rank Fusion, Cormack et al., 2009) — 점수 척도가 다른 두 검색의 순위만으로 결합

  python3 rag_vector.py index            # 가이드 → 임베딩 → Qdrant 색인 (가이드가 바뀌면 다시)
  python3 rag_vector.py search "질문"     # 하이브리드 검색 결과 확인
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
GUIDES = BASE / "guides" / "guides.json"
QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "cert_guides")
MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
RRF_K = 60

_lock = threading.Lock()
_model = None
_bm25 = None


# ───────── 공통: 청크
def load_chunks(path: Path = GUIDES) -> list[dict]:
    guides = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for g in guides:
        for i, s in enumerate(g["sections"]):
            out.append({"id": f"{g['id']}-{i + 1}", "doc": g["id"], "title": g["title"], "h": s["h"], "body": s["body"],
                        "applies": g.get("applies", [])})
    return out


def embed_text(c: dict) -> str:
    return f"{c['title']} > {c['h']}\n{c['body']}\n적용 대상: {', '.join(c['applies'])}"


# ───────── BM25 (web/core.js 와 같은 토큰 규칙)
def tokens(text: str) -> list[str]:
    t = str(text).lower()
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9.\-_]*", t):
        out.append(w)
        if "." in w:
            out += [p for p in w.split(".") if p]
    for run in re.findall(r"[가-힣]+", t):
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return out


class BM25:
    def __init__(self, chunks, k1=1.2, b=0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.toks = [tokens(f"{c['title']} {c['title']} {c['h']} {c['h']} {c['body']} {' '.join(c['applies'])}") for c in chunks]
        self.tf = []
        self.df: dict[str, int] = {}
        for tk in self.toks:
            f: dict[str, int] = {}
            for t in tk:
                f[t] = f.get(t, 0) + 1
            self.tf.append(f)
            for t in f:
                self.df[t] = self.df.get(t, 0) + 1
        self.N = len(chunks)
        self.avg = sum(len(t) for t in self.toks) / max(self.N, 1)

    def search(self, q: str, k=10, issuer: str | None = None, issuer_boost=1.5):
        qt = tokens(q)
        res = []
        for c, f, tk in zip(self.chunks, self.tf, self.toks):
            s = 0.0
            for t in qt:
                n = f.get(t)
                if n:
                    idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += idf * n * (self.k1 + 1) / (n + self.k1 * (1 - self.b + self.b * len(tk) / self.avg))
            if issuer and s > 0 and any(a.lower() in issuer.lower() for a in c["applies"]):
                s *= issuer_boost
            if s > 0:
                res.append((c["id"], s))
        res.sort(key=lambda x: -x[1])
        return res[:k]


# ───────── 임베딩 + Qdrant
def model():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("EMBED_CACHE", "/var/lib/cert-platform/models"))
        return _model


def embed(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in model().embed(texts)]


def _q(method: str, path: str, body=None, timeout=20):
    req = urllib.request.Request(QDRANT + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def guides_hash(chunks) -> str:
    return hashlib.sha256((MODEL + json.dumps([embed_text(c) for c in chunks], ensure_ascii=False)).encode()).hexdigest()[:16]


def index(force=False) -> dict:
    """가이드 청크를 임베딩해 Qdrant 컬렉션에 넣는다. 가이드·모델이 그대로면 건너뛴다."""
    chunks = load_chunks()
    h = guides_hash(chunks)
    try:
        info = _q("GET", f"/collections/{COLLECTION}")["result"]
        cur = (info.get("config", {}).get("params", {}) or {})
        same = info.get("points_count") == len(chunks) and _q("POST", f"/collections/{COLLECTION}/points/scroll",
                                                               {"limit": 1, "with_payload": ["guides_hash"]})["result"]["points"][0]["payload"].get("guides_hash") == h
        if same and not force:
            return {"collection": COLLECTION, "points": len(chunks), "hash": h, "reindexed": False}
        _ = cur
    except Exception:  # noqa: BLE001  컬렉션 없음
        pass
    t0 = time.time()
    vecs = embed([embed_text(c) for c in chunks])
    dim = len(vecs[0])
    try:
        _q("DELETE", f"/collections/{COLLECTION}")
    except Exception:  # noqa: BLE001
        pass
    _q("PUT", f"/collections/{COLLECTION}", {"vectors": {"size": dim, "distance": "Cosine"}})
    pts = [{"id": i + 1, "vector": v, "payload": {"chunk_id": c["id"], "doc": c["doc"], "title": c["title"], "h": c["h"],
                                                  "applies": c["applies"], "guides_hash": h}} for i, (c, v) in enumerate(zip(chunks, vecs))]
    _q("PUT", f"/collections/{COLLECTION}/points?wait=true", {"points": pts}, timeout=60)
    return {"collection": COLLECTION, "points": len(pts), "dim": dim, "model": MODEL, "hash": h, "reindexed": True,
            "seconds": round(time.time() - t0, 2)}


def vector_search(q: str, k=10) -> list[tuple[str, float]]:
    v = embed([q])[0]
    r = _q("POST", f"/collections/{COLLECTION}/points/search", {"vector": v, "limit": k, "with_payload": ["chunk_id"]})["result"]
    return [(p["payload"]["chunk_id"], float(p["score"])) for p in r]


def bm25() -> BM25:
    global _bm25
    if _bm25 is None:
        _bm25 = BM25(load_chunks())
    return _bm25


VEC_WEIGHT = float(os.environ.get("RAG_VEC_WEIGHT", "1.0"))


def rrf(*ranked: list[tuple[str, float]], k=RRF_K, weights=None) -> list[tuple[str, float]]:
    """가중 RRF: 점수 = Σ w / (k + 순위)"""
    s: dict[str, float] = {}
    weights = weights or [1.0] * len(ranked)
    for w, lst in zip(weights, ranked):
        for r, (cid, _) in enumerate(lst):
            s[cid] = s.get(cid, 0.0) + w / (k + r + 1)
    return sorted(s.items(), key=lambda x: -x[1])


FUSION = os.environ.get("RAG_FUSION", "cc")          # cc: 정규화 점수 볼록 결합 / rrf
ALPHA = float(os.environ.get("RAG_ALPHA", "0.6"))     # cc 에서 BM25 비중


def convex(b: list[tuple[str, float]], v: list[tuple[str, float]], alpha=ALPHA) -> list[tuple[str, float]]:
    """정규화 점수 볼록 결합(Bruch et al., 2023): α·BM25(min-max) + (1-α)·코사인(min-max). 후보에 없으면 0."""
    def norm(lst):
        if not lst:
            return {}
        hi, lo = max(x for _, x in lst), min(x for _, x in lst)
        return {c: (x - lo) / (hi - lo) if hi > lo else 1.0 for c, x in lst}
    nb, nv = norm(b), norm(v)
    s = {c: alpha * nb.get(c, 0.0) + (1 - alpha) * nv.get(c, 0.0) for c in set(nb) | set(nv)}
    return sorted(s.items(), key=lambda x: -x[1])


def hybrid(q: str, k=4, issuer: str | None = None, depth=10) -> dict:
    t0 = time.time()
    b = bm25().search(q, depth, issuer)
    t1 = time.time()
    try:
        v = vector_search(q, depth)
        err = None
    except Exception as e:  # noqa: BLE001  벡터 DB 장애 시 BM25 만으로 답한다
        v, err = [], str(e)[:120]
    t2 = time.time()
    fused = (convex(b, v) if FUSION == "cc" else rrf(b, v, weights=[1.0, VEC_WEIGHT])) if v else b
    br = {cid: i + 1 for i, (cid, _) in enumerate(b)}
    vr = {cid: i + 1 for i, (cid, _) in enumerate(v)}
    vs = dict(v)
    hits = [{"id": cid, "score": round(sc, 4), "rrf": round(sc, 5), "bm25_rank": br.get(cid), "vec_rank": vr.get(cid),
             "cosine": round(vs[cid], 3) if cid in vs else None} for cid, sc in fused[:k]]
    return {"mode": "hybrid" if v else "bm25", "hits": hits, "error": err,
            "ms": {"bm25": round((t1 - t0) * 1000, 1), "vector": round((t2 - t1) * 1000, 1)}}


def warmup():
    """플랫폼 기동 시 백그라운드로 모델 로드·색인 확인."""
    try:
        print("rag: vector index", json.dumps(index()), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"rag: vector index unavailable ({str(e)[:150]}) -> bm25 only", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "index"
    if cmd == "index":
        print(json.dumps(index(force="--force" in sys.argv), ensure_ascii=False))
    elif cmd == "search":
        print(json.dumps(hybrid(sys.argv[2], issuer=sys.argv[3] if len(sys.argv) > 3 else None), ensure_ascii=False, indent=1))

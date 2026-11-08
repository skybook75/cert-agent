"""검색 토큰 방식 비교: 띄어쓰기 단어 vs 한글 2글자 묶음(현재) vs 형태소 분석(Kiwi, 조사·어미 제거) vs 형태소+2글자

  pip install kiwipiepy        # 형태소 분석기(설치 필요)
  python3 tools/retrieval_morph_eval.py [결과.json]

같은 BM25(k1=1.2, b=0.75), 같은 색인 대상(제목·소제목 2배 가중 + 본문 + 적용 대상), 같은 평가셋(qa_eval_set.json 중 정답 가이드가 있는 20문항).
지표: 1위 적중률, 상위 2·4개 포함률, MRR.
"""
import json
import math
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
guides = json.loads((BASE / "guides/guides.json").read_text(encoding="utf-8"))
evalset = [x for x in json.loads((BASE / "tests/qa_eval_set.json").read_text(encoding="utf-8")) if x["docs"]]


def t_space(s):
    return [w for w in re.split(r"[\s,.?!()'\"]+", s.lower()) if w]


def _latin(s):
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9.\-_]*", s.lower()):
        out.append(w)
        if "." in w:
            out += [p for p in w.split(".") if p]
    return out


def t_bigram(s):                         # web/core.js tokens() 와 같은 규칙
    out = _latin(s)
    for run in re.findall(r"[가-힣]+", s.lower()):
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return out


KIWI = None
KEEP = ("NNG", "NNP", "XR", "SL", "SH", "SN")                  # 명사·어근·외국어·숫자 (조사 J*, 어미 E* 제외)


def t_morph(s):
    global KIWI
    if KIWI is None:
        from kiwipiepy import Kiwi
        KIWI = Kiwi()
    out = _latin(s)
    for t in KIWI.tokenize(s):
        if t.tag in KEEP and re.search(r"[가-힣]", t.form):
            out.append(t.form.lower())
        elif t.tag in ("VV", "VA"):                                # 동사·형용사는 원형 어간 (갱신하려면 → 갱신 + 하)
            out.append(t.form + "다")
    return out


def t_morph_bigram(s):
    return t_morph(s) + [x for x in t_bigram(s) if re.search(r"[가-힣]", x)]


def build(tok, rich=True):
    chunks = []
    for g in guides:
        for i, sec in enumerate(g["sections"]):
            txt = f"{g['title']} {g['title']} {sec['h']} {sec['h']} {sec['body']} {' '.join(g['applies'])}" if rich else f"{g['title']} {sec['h']} {sec['body']}"
            chunks.append({"doc": g["id"], "toks": tok(txt)})
    df = {}
    for c in chunks:
        for t in set(c["toks"]):
            df[t] = df.get(t, 0) + 1
    avg = sum(len(c["toks"]) for c in chunks) / len(chunks)
    return chunks, df, avg


def search(idx, tok, q, k):
    chunks, df, avg = idx
    N, qt, res = len(chunks), tok(q), []
    for c in chunks:
        tf = {}
        for t in c["toks"]:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in qt:
            f = tf.get(t)
            if f:
                s += math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(c["toks"]) / avg))
        if s > 0:
            res.append((s, c["doc"]))
    return [d for _, d in sorted(res, key=lambda x: -x[0])[:k]]


def evaluate(name, tok, rich=True):
    t0 = time.time()
    idx = build(tok, rich)
    t_build = time.time() - t0
    out = {"method": name, "n": len(evalset), "index_s": round(t_build, 3)}
    for k in (1, 2, 4):
        hit, mrr, miss = 0, 0.0, []
        for x in evalset:
            docs = search(idx, tok, x["q"], k)
            r = next((i for i, d in enumerate(docs) if d in x["docs"]), -1)
            if r >= 0:
                hit += 1
                mrr += 1 / (r + 1)
            else:
                miss.append(f"{x['id']} {x['q']} → {docs}")
        out[f"top{k}"] = round(hit / len(evalset) * 100, 1)
        if k == 4:
            out["mrr"] = round(mrr / len(evalset), 3)
            out["miss_top4"] = miss
    t1 = time.time()
    for x in evalset:
        search(idx, tok, x["q"], 4)
    out["query_ms"] = round((time.time() - t1) / len(evalset) * 1000, 2)
    return out


rows = [evaluate("띄어쓰기 단어(기준선)", t_space, rich=False), evaluate("한글 2글자 묶음(현재)", t_bigram)]
try:
    rows.append(evaluate("형태소 분석(Kiwi, 조사·어미 제거)", t_morph))
    rows.append(evaluate("형태소 + 2글자 묶음", t_morph_bigram))
    from kiwipiepy import Kiwi  # noqa: F401
    import kiwipiepy
    ver = kiwipiepy.__version__
except ImportError:
    ver = None
ex = "인증서를 갱신하려면 톰캣에 어떻게 적용해?"
demo = {"question": ex, "bigram": t_bigram(ex)}
if ver:
    demo["morph"] = t_morph(ex)
res = {"kiwipiepy": ver, "rows": rows, "token_example": demo}
print(json.dumps(res, ensure_ascii=False, indent=1))
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

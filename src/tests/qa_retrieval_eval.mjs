// QA 챗봇 검색(Retrieval) 평가: 기준선(띄어쓰기 단어 BM25, 상위 2개) vs 개선(한글 2글자 묶음 + 발급기관 가중치, 상위 4개)
import { createRequire } from "module";
import fs from "fs";
const require = createRequire(import.meta.url);
const C = require("../web/core.js");
const guides = require("../guides/guides.json");
const set = require("./qa_eval_set.json").filter(x => x.docs.length);
const certs = JSON.parse(process.env.CERTS || "[]");

// 기준선: 공백 단어 토큰 (조사 붙은 한국어는 매칭이 안 됨)
function baselineSearch(q, k) {
  const tok = s => String(s).toLowerCase().split(/[\s,.?!()'"]+/).filter(Boolean);
  const chunks = [];
  for (const g of guides) g.sections.forEach((s, i) => chunks.push({ id: `${g.id}-${i + 1}`, doc: g.id, toks: tok(`${g.title} ${s.h} ${s.body}`) }));
  const df = new Map(); for (const c of chunks) for (const t of new Set(c.toks)) df.set(t, (df.get(t) || 0) + 1);
  const avg = chunks.reduce((a, c) => a + c.toks.length, 0) / chunks.length, N = chunks.length;
  return chunks.map(c => { const tf = new Map(); c.toks.forEach(t => tf.set(t, (tf.get(t) || 0) + 1)); let s = 0;
    for (const t of tok(q)) { const f = tf.get(t); if (!f) continue; const idf = Math.log(1 + (N - df.get(t) + 0.5) / (df.get(t) + 0.5)); s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * c.toks.length / avg)); }
    return { chunk: c, score: s }; }).filter(x => x.score > 0).sort((a, b) => b.score - a.score).slice(0, k);
}
const index = C.buildIndex(guides);
function improved(q, k) { const m = C.findCerts(q, certs); return C.search(index, q, { k, issuer: m[0]?.issuer_cn }); }

function evaluate(fn, k) {
  let h1 = 0, hk = 0, mrr = 0; const miss = [];
  for (const x of set) {
    const docs = fn(x.q, k).map(h => h.chunk.doc);
    const rank = docs.findIndex(d => x.docs.includes(d));
    if (rank === 0) h1++; if (rank >= 0) { hk++; mrr += 1 / (rank + 1); } else miss.push(`${x.id} ${x.q} → [${docs.join(",")}]`);
  }
  const n = set.length;
  return { n, hit1: +(h1 / n * 100).toFixed(1), hitk: +(hk / n * 100).toFixed(1), mrr: +(mrr / n).toFixed(3), miss };
}
const base = evaluate(baselineSearch, 2), imp = evaluate(improved, 4), imp2 = evaluate(improved, 2);
console.log("기준선(단어, top2):", JSON.stringify({ ...base, miss: base.miss.length }));
base.miss.forEach(m => console.log("   ✗", m));
console.log("개선(2글자 묶음+발급기관 가중치, top2):", JSON.stringify({ ...imp2, miss: imp2.miss.length }));
console.log("개선(top4):", JSON.stringify({ ...imp, miss: imp.miss.length }));
imp.miss.forEach(m => console.log("   ✗", m));
fs.writeFileSync(new URL("../results/qa_retrieval.json", import.meta.url), JSON.stringify({ baseline_top2: base, improved_top2: imp2, improved_top4: imp }, null, 1));

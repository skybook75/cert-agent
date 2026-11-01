// Claude 답변 채점(규칙 기반): 사실 포함 · 인용 유효성 · 기대 가이드 인용 · 근거 없음 처리
import { createRequire } from "module"; import fs from "fs";
const require = createRequire(import.meta.url);
const C = require("../web/core.js");
const set = require("./qa_eval_set.json");
const P = JSON.parse(fs.readFileSync(process.argv[2])), A = JSON.parse(fs.readFileSync(process.argv[3]));
const rows = []; const by = Object.fromEntries(A.map(a => [a.id, a.answer]));
for (const x of set) {
  const p = P.find(o => o.id === x.id);
  if (p.blocked) { rows.push({ id: x.id, type: x.type, pass: x.type === "blocked", note: "가드레일 차단: " + p.blocked }); continue; }
  const ans = by[x.id] || ""; const low = ans.toLowerCase();
  const chk = C.checkAnswer(ans, p.provided.map(id => ({ chunk: { id } })));
  const factOk = x.facts.every(g => g.some(f => low.includes(f.toLowerCase())));
  const citedDocs = [...new Set(chk.cited.map(c => c.split("-")[0]))];
  const docOk = !x.docs.length || x.docs.some(d => citedDocs.includes(d));
  let pass, note;
  if (x.type === "unanswerable") {
    const invented = /\d{2,4}-\d{3,4}-\d{4}|\d[\d,]*\s*(원|달러)|\$\s?\d/.test(ans);
    pass = chk.noBasis && !invented; note = `근거 없음 안내 ${chk.noBasis ? "O" : "X"}, 지어낸 수치·연락처 ${invented ? "있음" : "없음"}`;
  } else if (x.type === "blocked") { pass = false; note = "차단되지 않음"; }
  else { pass = factOk && docOk && chk.invalid.length === 0 && chk.grounded;
    note = `사실 ${factOk ? "O" : "X"}, 기대 가이드 인용 ${docOk ? "O" : "X"}(${citedDocs.join(",") || "-"}), 잘못된 인용 ${chk.invalid.length}`; }
  rows.push({ id: x.id, type: x.type, q: x.q, pass, note, answer: ans });
}
const sum = (t) => { const r = rows.filter(x => !t || x.type === t); return `${r.filter(x => x.pass).length}/${r.length}`; };
rows.filter(r => !r.pass).forEach(r => console.log("✗", r.id, r.q, "|", r.note, "\n   ", (r.answer || "").slice(0, 300).replace(/\n/g, " ")));
const summary = { 전체: sum(), 가이드질문: sum("answer"), 인증서질문: sum("cert"), 근거없음: sum("unanswerable"), 차단: sum("blocked"),
  잘못된인용: rows.reduce((a, r) => a + (r.answer ? C.checkAnswer(r.answer, P.find(o => o.id === r.id).provided.map(id => ({ chunk: { id } }))).invalid.length : 0), 0) };
console.log(JSON.stringify(summary));
fs.writeFileSync(new URL(`../results/${process.argv[4] || "qa_generation"}.json`, import.meta.url), JSON.stringify({ summary, rows }, null, 1));

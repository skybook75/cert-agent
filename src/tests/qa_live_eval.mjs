// QA 챗봇 실측 평가(실제 Claude 호출): 28문항 → 답변·응답 시간·토큰·비용 기록 → qa_grade 로 채점
//   ANTHROPIC_API_KEY=… node tests/qa_live_eval.mjs [이름]      (CLAUDE_MODEL 미지정 시 claude-sonnet-4-6)
//   화면(QA 챗봇)과 같은 경로: 입력 가드레일 → 인증서 인식 → 가이드 검색(상위 4) → 프롬프트 → Claude
import { createRequire } from "module";
import fs from "fs";
import { execFileSync } from "child_process";
const require = createRequire(import.meta.url);
const C = require("../web/core.js");
const set = require("./qa_eval_set.json");
const guides = JSON.parse(fs.readFileSync(new URL("../guides/guides.json", import.meta.url)));
const seed = JSON.parse(fs.readFileSync(new URL("../web/seed.json", import.meta.url)));
const certs = Object.entries(seed.certificates).map(([id, c]) => ({ ...c, id }));
const NOW = Date.parse("2026-10-26T09:00:00+09:00");            // 평가 기준일 고정(만료 구간 판정 재현)
const KEY = process.env.ANTHROPIC_API_KEY, MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-4-6";
const NAME = process.argv[2] || "qa_live";
const PRICE = { in: 3 / 1e6, out: 15 / 1e6 };                    // USD/토큰 (Sonnet 계열 표준 단가)
const index = C.buildIndex(guides);

async function claude(prompt) {
  const t0 = Date.now();
  const r = await fetch("https://api.anthropic.com/v1/messages", { method: "POST",
    headers: { "x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({ model: MODEL, max_tokens: 900, messages: [{ role: "user", content: prompt }] }) });
  const j = await r.json(); if (!r.ok) throw new Error(`${r.status} ${JSON.stringify(j).slice(0, 200)}`);
  return { text: j.content.map((c) => c.text || "").join(""), ms: Date.now() - t0, in: j.usage.input_tokens, out: j.usage.output_tokens };
}

const P = [], A = [], M = [];
for (const x of set) {
  const blocked = C.qaGuard(x.q);
  if (blocked) { P.push({ id: x.id, blocked }); A.push({ id: x.id, answer: "" }); M.push({ id: x.id, blocked: true, ms: 0, in: 0, out: 0 }); continue; }
  const mentioned = C.findCerts(x.q, certs);
  const hits = C.withRenewalContext(index, x.q, C.search(index, x.q, { k: 4, issuer: mentioned[0]?.issuer_cn }), mentioned[0], 4);
  const prompt = C.buildPrompt(x.q, hits, mentioned, NOW);
  const r = await claude(prompt);
  P.push({ id: x.id, provided: hits.map((h) => h.chunk.id) }); A.push({ id: x.id, answer: r.text }); M.push({ id: x.id, ms: r.ms, in: r.in, out: r.out, chars: prompt.length });
  process.stdout.write(`${x.id} ${r.ms}ms in ${r.in} out ${r.out}\n`);
}
const dir = new URL("../results/", import.meta.url);
fs.writeFileSync(new URL(`${NAME}_prompts.json`, dir), JSON.stringify(P, null, 1));
fs.writeFileSync(new URL(`${NAME}_answers.json`, dir), JSON.stringify(A, null, 1));
const live = M.filter((m) => !m.blocked), ms = live.map((m) => m.ms).sort((a, b) => a - b);
const q = (p) => ms[Math.min(ms.length - 1, Math.floor(p * ms.length))];
const tin = live.reduce((a, m) => a + m.in, 0), tout = live.reduce((a, m) => a + m.out, 0);
const perf = { model: MODEL, questions: set.length, llm_calls: live.length, blocked_no_call: M.length - live.length,
  latency_ms: { median: q(0.5), p90: q(0.9), max: ms[ms.length - 1] },
  tokens: { input_avg: Math.round(tin / live.length), output_avg: Math.round(tout / live.length), input_total: tin, output_total: tout },
  cost_usd: { total: +(tin * PRICE.in + tout * PRICE.out).toFixed(4), per_question: +((tin * PRICE.in + tout * PRICE.out) / live.length).toFixed(5) } };
fs.writeFileSync(new URL(`${NAME}_perf.json`, dir), JSON.stringify({ perf, rows: M }, null, 1));
console.log(JSON.stringify(perf));
console.log(execFileSync("node", ["tests/qa_grade.mjs", `results/${NAME}_prompts.json`, `results/${NAME}_answers.json`, `${NAME}_grade`],
  { cwd: new URL("..", import.meta.url).pathname }).toString());

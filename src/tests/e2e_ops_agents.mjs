// AI 운영(오케스트레이터·유지보수 Agent·자가 진단·MCP) E2E — 플랫폼 사본에서 실행(코드 반영 테스트가 원본을 바꾸지 않도록)
//   node tests/e2e_ops_agents.mjs            (LLM 은 tests 용 모의 응답 CERT_LLM_MOCK 사용)
import { createRequire } from "module";
import { spawn, execFileSync, spawnSync } from "child_process";
import fs from "fs";
import path from "path";

const { chromium } = createRequire(import.meta.url)("playwright");
const SRC = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const W = "/tmp/cert_ops", COPY = path.join(W, "app"), PORT = 8791, BASE = `http://localhost:${PORT}/`;
fs.rmSync(W, { recursive: true, force: true }); fs.mkdirSync(W, { recursive: true });
execFileSync("sh", ["-c", `mkdir -p ${COPY} && cd ${SRC} && tar --exclude=results --exclude=__pycache__ --exclude=maint_backups --exclude='*.db' -cf - . | tar -xf - -C ${COPY}`]);
const MOCK = path.join(W, "mock.json");
fs.writeFileSync(MOCK, JSON.stringify({
  "어떤 파일을 봐야": { analysis: "배포 화면 제목에 단계 수를 표시해 달라는 UI 개선 요청이다.", files: ["web/index.html"] },
  "old 문자열을 new 로": { summary: "배포 화면 제목을 '배포 현황 (4단계)'로 바꿨습니다.", edits: [{ file: "web/index.html", old: '<div class="card"><h2>배포 현황</h2>', new: '<div class="card"><h2>배포 현황 (4단계)</h2>' }] },
  "도구 실행 순서": { steps: ["collector.status", "notifier.run", "maint.diagnose", "report.summary"], why: "수집 상태 확인 후 알림과 진단" },
}));
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: String(d) }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${String(d).slice(0, 220)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let srv;
const start = () => { srv = spawn("python3", [path.join(COPY, "platform_server.py"), "--db", path.join(W, "p.db"), "--port", String(PORT), "--no-browser", "--vault-key", path.join(W, "v.key"), "--deploy", path.join(W, "deploy")],
  { cwd: COPY, stdio: "ignore", env: { ...process.env, CERT_LLM_MOCK: MOCK } }); };
start(); await sleep(1500);
const browser = await chromium.launch();
const ctx = await browser.newContext({ bypassCSP: true }); await ctx.route(/fonts\./, (r) => r.abort());
const p = await ctx.newPage(); p.on("pageerror", (e) => console.log("pageerror", e.message));
const settle = async () => { await p.waitForLoadState("networkidle"); await sleep(300); };
const api = (method, url, body) => p.evaluate(async ({ method, url, body }) => { const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined }); return { status: r.status, body: await r.json().catch(() => null) }; }, { method, url, body });
try {
  await p.goto(BASE); await settle();
  await p.evaluate(async () => fetch("api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", name: "김관리", email: "admin@demo.test", password: "Admin2026" }) }));
  await p.goto(BASE); await settle(); await p.fill("#a-user", "admin"); await p.fill("#a-pw", "Admin2026"); await p.click("#auth-form button[type=submit]"); await settle();
  // 데이터: 만료 임박 인증서 1건(알림 없음) + 서버 오류 1건 유발
  await api("POST", "api/action", { action: "import.excel", summary: "seed", ops: [{ op: "set", coll: "certificates", id: "x_shop", data: { subject_cn: "shop.demo.test", issuer_cn: "Demo CA", not_after: new Date(Date.now() + 5 * 864e5).toISOString().slice(0, 19) + "+00:00", owner: "김담당", registered_from: "엑셀" } }] });
  await api("POST", "api/maint/client-error", { message: "TypeError: Cannot read properties of undefined (reading 'x')", where: "dash index.html:999", stack: "at renderDash" });
  await p.locator("#tabs button[data-tab=ops]").click(); await settle();
  ok("AI 운영 탭(관리자): 오케스트레이터 그래프·유지보수 채팅·자가 진단 표시", await p.isVisible("#orch-graph svg") && await p.isVisible("#mchat-form") && await p.isVisible("#diag-run"), (await p.textContent("#p-ops")).slice(0, 80));
  // 오케스트레이션 (LLM 계획)
  await p.click("[data-goal='일일 운영 점검']");
  await p.waitForFunction(() => document.querySelector("#orch-out")?.textContent.includes("최근 실행"), null, { timeout: 30000 }); await settle();
  const out = await p.textContent("#orch-out");
  ok("오케스트레이터: 계획 → Agent 순차 호출 → 결과 보고(알림 Agent가 만료 임박 알림 생성)", /정해진 점검 절차/.test(out) && /새 알림 1건/.test(out) && /수집 Agent/.test(out), out.slice(0, 200));
  ok("오케스트레이터 → Agent 호출이 MCP(tools/call)로 이뤄짐", /MCP 도구 호출/.test(out) && (out.match(/MCP tools\/call/g) || []).length >= 4, (out.match(/MCP tools\/call/g) || []).length + "개 단계");
  const runSt = await p.$$eval("#orch-graph .node.ok, #orch-graph .node.bad, #orch-graph .node.human", (n) => n.length);
  ok("그래프에 Agent별 실행 결과 색 표시", runSt >= 3, `상태 표시 노드 ${runSt}개`);
  // 자가 진단
  await p.click("#diag-run"); await settle(); await sleep(500);
  const diag = await p.textContent("#p-ops");
  ok("자가 진단: 화면 오류를 버그 리포트로, 이메일 누락을 데이터 품질 항목으로 제안", /화면 오류: TypeError/.test(diag) && /담당자 이메일 없는 인증서/.test(diag), (diag.match(/열림 \d+건/) || [""])[0]);
  // 유지보수 Agent 대화: 진단 → 코드 개선(테스트 게이트) → 반영 → 되돌리기
  await p.fill("#mchat-in", "진단해줘"); await p.click("#mchat-send"); await settle(); await sleep(800);
  ok("유지보수 Agent 대화: '진단해줘' → 열린 항목 요약", /자가 진단 결과 열린 항목/.test(await p.textContent("#mchat")));
  await p.fill("#mchat-in", "배포 화면 제목에 4단계라고 표시되게 바꿔줘"); await p.click("#mchat-send");
  await p.waitForFunction(() => /반영했습니다|반영하지 않았|보류/.test(document.querySelector("#mchat")?.textContent || ""), null, { timeout: 120000 }); await settle();
  const st = await api("GET", "api/ops/state"); const rep = st.body.reports[0];
  const changed = fs.readFileSync(path.join(COPY, "web/index.html"), "utf8").includes("배포 현황 (4단계)");
  ok("코드 개선: 분석 → 수정안 → 임시 사본 자동 테스트(컴파일·문법·기동·화면) 통과 → 반영 + 리포트", rep?.status === "반영" && changed && rep.tests.length >= 5 && rep.tests.every((t) => t.ok) && /배포 현황 \(4단계\)/.test(rep.diff),
    `${rep?.status} · 테스트 ${rep?.tests?.filter((t) => t.ok).length}/${rep?.tests?.length} · ${rep?.files}`);
  // 서버는 반영 후 스스로 재시작 → 화면 확인
  await sleep(3000); await p.goto(BASE); await settle();
  await p.locator("#tabs button[data-tab=deploy]").click(); await sleep(300);
  ok("재시작 후 실제 화면에 반영됨", /배포 현황 \(4단계\)/.test(await p.textContent("#p-deploy")));
  // 테스트 실패하는 수정안은 반영 안 함 (Self-Correction 후에도 실패)
  const bad = JSON.parse(fs.readFileSync(MOCK, "utf8")); bad["old 문자열을 new 로"] = { summary: "깨진 수정", edits: [{ file: "agents.py", old: "def now_iso() -> str:", new: "def now_iso() -> str\n" }] };
  fs.writeFileSync(MOCK, JSON.stringify(bad));
  const r2 = await api("POST", "api/ops/chat", { message: "알림 시각 표시를 바꿔줘" });
  const st2 = await api("GET", "api/ops/state"); const rep2 = st2.body.reports[0];
  ok("문법 오류 수정안 → 테스트 실패 → 2회 재시도 후에도 실패 → 반영하지 않음(서비스 영향 없음)", rep2.status === "미반영" && rep2.steps.filter((x) => /자동 테스트/.test(x.step)).length === 3 && fs.readFileSync(path.join(COPY, "agents.py"), "utf8").includes("def now_iso() -> str:"),
    `${rep2.status} · 시도 ${rep2.steps.filter((x) => /자동 테스트/.test(x.step)).length}회`);
  // 되돌리기
  await p.locator("#tabs button[data-tab=ops]").click(); await settle();
  await p.evaluate(() => document.querySelectorAll("#p-ops details").forEach((d) => (d.open = true)));
  p.once("dialog", (d) => d.accept());
  await p.click(`[data-rb="${rep.id}"]`); await sleep(3500); await p.goto(BASE); await settle();
  await p.locator("#tabs button[data-tab=deploy]").click(); await sleep(300);
  ok("반영 되돌리기 → 원래 화면으로 복구", !/배포 현황 \(4단계\)/.test(await p.textContent("#p-deploy")) && /배포 현황/.test(await p.textContent("#p-deploy")));
  // 진단 항목 자동 반영
  await p.locator("#tabs button[data-tab=ops]").click(); await settle();
  const st3 = await api("GET", "api/ops/state");
  const aud = (await api("GET", "api/db/audit")).body.map((a) => a.action);
  ok("감사 로그: 오케스트레이션·자가 진단·코드 개선 반영·되돌림", ["오케스트레이션 실행", "자가 진단", "코드 개선 반영", "코드 개선 되돌림"].every((k) => aud.includes(k)), [...new Set(aud)].filter((a) => /오케|진단|코드/.test(a)).join(", "));
  // 권한: 담당자는 접근 불가
  await api("POST", "api/users", { username: "own", name: "김담당", email: "own@demo.test", role: "담당자", password: "Temp2026a" });
  const c2 = await browser.newContext(); const p2 = await c2.newPage(); await p2.goto(BASE);
  await p2.fill("#a-user", "own"); await p2.fill("#a-pw", "Temp2026a"); await p2.click("#auth-form button[type=submit]"); await sleep(800);
  const deny = await p2.evaluate(async () => (await fetch("api/ops/state")).status);
  ok("권한: 담당자는 AI 운영 API 접근 불가(403)", deny === 403, deny);
  // MCP 서버 (stdio JSON-RPC)
  const mcpIn = [{ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }, { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list" }, { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "list_expiring", arguments: { days: 30 } } }].map((x) => JSON.stringify(x)).join("\n") + "\n";
  const mo = spawnSync("python3", [path.join(COPY, "mcp_server.py"), "--db", path.join(W, "p.db")], { input: mcpIn }).stdout.toString().trim().split("\n").map((l) => JSON.parse(l));
  ok("MCP 서버: initialize · tools/list(5개) · tools/call list_expiring → shop.demo.test", mo[0].result.serverInfo.name === "cert-platform" && mo[1].result.tools.length === 5 && mo[2].result.content[0].text.includes("shop.demo.test"),
    mo[1].result.tools.map((t) => t.name).join(", "));
  const miIn = [{ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }, { jsonrpc: "2.0", id: 2, method: "tools/list" }].map((x) => JSON.stringify(x)).join("\n") + "\n";
  const mi = spawnSync("python3", [path.join(COPY, "mcp_server.py"), "--db", path.join(W, "p.db"), "--profile", "internal"], { input: miIn }).stdout.toString().trim().split("\n").map((l) => JSON.parse(l));
  const inames = mi[1].result.tools.map((t) => t.name);
  ok("MCP 내부 프로필(오케스트레이터용): Agent 도구 9종 · 배포 실행 도구 없음", mi[0].result.serverInfo.name === "cert-platform-internal" && inames.length === 9 && !inames.some((n) => /deploy_run|delete|approve/.test(n)), inames.join(", "));
  await p.locator("#tabs button[data-tab=ops]").click(); await settle();
  fs.mkdirSync(path.join(SRC, "results", "acceptance", "shots"), { recursive: true });
  await p.locator("#p-ops").screenshot({ path: path.join(SRC, "results", "acceptance", "shots", "O01_ai_ops.png") });
} catch (e) { console.error(e); ok("실행 오류 없음", false, e.message); }
finally {
  console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
  fs.writeFileSync(path.join(SRC, "results", "e2e_ops_agents.json"), JSON.stringify(R, null, 1));
  await browser.close(); srv.kill();
  spawnSync("pkill", ["-f", `--port ${PORT}`]);
}

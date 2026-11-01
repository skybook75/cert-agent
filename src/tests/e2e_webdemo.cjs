// 웹 시연 모드 점검: 정적 서버 + window.claude(db·downloads) 모의 객체로 역할 전환·담당 범위·쓰기·내려받기 확인
const { chromium } = require("playwright"); const http = require("http"); const fs = require("fs"); const path = require("path");
const WEB = path.join(__dirname, "..", "web"), GUIDES = path.join(__dirname, "..", "guides", "guides.json");
const srv = http.createServer((q, r) => { const f = q.url === "/" ? path.join(WEB, "index.html") : q.url === "/guides.json" ? GUIDES : path.join(WEB, q.url.slice(1));
  if (!fs.existsSync(f) || q.url.startsWith("/api")) { r.writeHead(404); return r.end("no"); } r.writeHead(200, { "Content-Type": f.endsWith(".js") ? "text/javascript" : f.endsWith(".json") ? "application/json" : "text/html; charset=utf-8" });
  r.end(f.endsWith("index.html") ? "<!doctype html><meta charset=utf-8><body>" + fs.readFileSync(f, "utf8") : fs.readFileSync(f)); }).listen(8790);
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: d }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${d}`); };
const now = Date.now(), iso = (d) => new Date(now + d * 864e5).toISOString().slice(0, 19) + "+00:00";
const seed = { certificates: {
  c_pay: { subject_cn: "pay.example.co.kr", issuer_cn: "PoC Test CA", not_before: iso(-300), not_after: iso(200), fingerprint: "AA:01", owner: "박결제", renewal_status: "미조치", locations: ["검증/pay.crt"], public_key: "RSA 2048bit", signature_algorithm: "sha256WithRSAEncryption", san: "pay.example.co.kr" },
  c_api: { subject_cn: "api.example.co.kr", issuer_cn: "PoC Test CA", not_before: iso(-300), not_after: iso(20), fingerprint: "AA:02", owner: "이연동", renewal_status: "미조치", locations: ["a.pem", "b/a.pem"] } } };
(async () => {
  const b = await chromium.launch(); const c = await b.newContext(); await c.route(/fonts/, (r) => r.abort()); const p = await c.newPage();
  p.on("pageerror", (e) => console.log("pageerror", e.message));
  await p.addInitScript((seed) => {
    try { localStorage.setItem("demoSource", "shared"); } catch {}
    const store = JSON.parse(JSON.stringify(seed)); window.__store = store; window.__saved = [];
    const coll = (n) => ({ get: async () => ({ docs: Object.entries(store[n] || {}).map(([id, d]) => ({ id, data: () => JSON.parse(JSON.stringify(d)) })) }),
      doc: (id) => ({ set: async (d) => { (store[n] ||= {})[id] = d; }, update: async (d) => { (store[n] ||= {})[id] = { ...(store[n][id] || {}), ...d }; } }) });
    const caps = { db: { collection: coll }, downloads: { save: async (x) => { window.__saved.push({ name: x.filename, size: x.data.length || x.data.byteLength }); return { status: "saved" }; } }, user: { can: async () => true }, sample: null };
    window.claude = { use: async (n) => caps[n] ?? null };
  }, seed);
  await p.goto("http://localhost:8790/"); await p.waitForTimeout(800);
  ok("웹 시연 모드 시작(관리자 13개 탭)", (await p.$$("#tabs button")).length === 13, await p.textContent("#conn-text"));
  await p.selectOption("#d-role", "담당자"); await p.fill("#d-name", "박결제"); await p.press("#d-name", "Tab"); await p.waitForTimeout(300);
  await p.click("#tabs button[data-tab=certs]");
  const rows = await p.$$eval("#p-certs tbody tr[data-cert]", (t) => t.map((r) => r.children[0].firstChild.textContent));
  ok("역할 전환 → 담당자 9개 탭·본인 인증서만", (await p.$$("#tabs button")).length === 9 && rows.join() === "pay.example.co.kr", rows.join());
  await p.click("#p-certs tbody tr[data-cert]"); await p.fill("#d-depnote", "dev"); await p.click("#d-deploy"); await p.waitForTimeout(300);
  const dep = await p.evaluate(() => Object.values(window.__store.deployments || {})[0]);
  ok("담당자 배포 요청 기록", dep?.status === "요청" && dep.requested_by === "박결제(담당자)", dep?.stage_name);
  await p.selectOption("#d-role", "검토자"); await p.fill("#d-name", "이검토"); await p.press("#d-name", "Tab"); await p.waitForTimeout(300);
  await p.click("#tabs button[data-tab=deploy]"); await p.click("[data-rvpass]"); await p.waitForTimeout(300);
  await p.selectOption("#d-role", "승인자"); await p.fill("#d-name", "최승인"); await p.press("#d-name", "Tab"); await p.waitForTimeout(300);
  await p.click("#tabs button[data-tab=deploy]"); await p.click("[data-approve-dep]"); await p.waitForTimeout(300);
  const env = await p.evaluate(() => Object.values(window.__store.environments || {})[0]);
  ok("검토 → 승인 → 개발 환경 기록", env?.cert_id === "c_pay" && env.stage === "dev", JSON.stringify(env?.history?.map((h) => h.by)));
  await p.selectOption("#d-role", "조회자"); await p.waitForTimeout(300);
  await p.click("#tabs button[data-tab=report]"); await p.click("#rep-xlsx"); await p.waitForTimeout(300);
  const saved = await p.evaluate(() => window.__saved);
  ok("보고서 엑셀 내려받기(downloads 기능)", saved[0]?.name.endsWith(".xlsx") && saved[0].size > 2000, JSON.stringify(saved[0]));
  const blocked = await p.evaluate(async () => { try { await act("cert.manage", [{ op: "update", coll: "certificates", id: "c_pay", data: { owner: "x" } }]); return "allowed"; } catch (e) { return e.message; } });
  ok("조회자 쓰기 차단(화면 검사)", blocked.includes("권한"), blocked);
  // 공개 링크 방문자(로그인 없음): 공유 DB 없이 시연 데이터로 동작
  const p2 = await c.newPage(); p2.on("pageerror", (e) => console.log("pageerror2", e.message)); await p2.goto("http://localhost:8790/"); await p2.waitForTimeout(800);
  await p2.click("#tabs button[data-tab=certs]");
  const n2 = (await p2.$$("#p-certs tbody tr[data-cert]")).length, conn2 = await p2.textContent("#conn-text");
  await p2.click("#tabs button[data-tab=deploy]"); const dep2 = await p2.textContent("#p-deploy");
  ok("공개 방문자: 시연 데이터로 전체 화면 동작", n2 >= 10 && conn2.includes("시연 데이터") && dep2.includes("배포 완료"), `${conn2} · 인증서 ${n2}건`);
  // 웹 수집 Agent: 대시보드에서 검증용 13개 파일 → 승인 대기 → 승인
  await p2.selectOption("#d-role", "관리자"); await p2.waitForTimeout(200);
  await p2.click("#tabs button[data-tab=dash]"); const before = (await p2.$$eval("#b-disc", (b) => b.map((x) => x.textContent)))[0];
  await p2.click("#wc-sample"); await p2.waitForTimeout(200);
  const sel = await p2.textContent("#wc-sel");
  // PFX 비밀번호 보관함(웹): 보관함 생성 → 파일 범위로 등록(브라우저에서 RSA-OAEP 암호화) → 잠금 → 보관함 비밀번호로 열고 수집
  await p2.click("#tabs button[data-tab=vault]"); await p2.waitForTimeout(100);
  await p2.fill("#vc-p1", "Vault-Pass-2026"); await p2.fill("#vc-p2", "Vault-Pass-2026"); await p2.click("#vc-go");
  await p2.waitForSelector("#vs-form", { timeout: 20000 });
  await p2.selectOption("#vs-scope", "file"); await p2.fill("#vs-path", "검증\\pay-d7.pfx"); await p2.fill("#vs-label", "결제 서버 PFX");
  await p2.fill("#vs-p1", "Test#1234"); await p2.fill("#vs-p2", "Test#1234"); await p2.click("#vs-go"); await p2.waitForTimeout(600);
  const vt = await p2.textContent("#p-vault");
  const stored = await p2.evaluate(async () => { const d = (await S.db.collection("secrets").get()).docs.map((x) => x.data()); const v = (await S.db.collection("settings").get()).docs.map((x) => x.data()); return JSON.stringify({ d, v }); });
  ok("웹 보관함: 등록 → 암호문만 저장(RSA-OAEP, 개인키는 보관함 비밀번호로 암호화)", vt.includes("결제 서버 PFX") && vt.includes("파일 검증/pay-d7.pfx") && !stored.includes("Test#1234") && !stored.includes("Vault-Pass-2026") && stored.includes("RSA-OAEP-3072"), "평문·보관함 비밀번호 미포함");
  await p2.click("#tabs button[data-tab=dash]"); await p2.click("#vault-lock"); await p2.waitForTimeout(100);
  await p2.evaluate(() => ($("toast").hidden = true));
  await p2.fill("#vault-pass", "wrong-pass-123"); await p2.click("#vault-unlock"); await p2.waitForFunction(() => /보관함 비밀번호가/.test($("toast").textContent) && !$("toast").hidden, null, { timeout: 20000 }); const wt = await p2.textContent("#toast");
  await p2.waitForSelector("#vault-unlock:not([disabled])");
  await p2.fill("#vault-pass", "Vault-Pass-2026"); await p2.click("#vault-unlock");
  await p2.waitForSelector("#p-dash .pill.ok >> text=보관함 열림", { timeout: 20000 });
  ok("보관함 잠금·열기(틀린 보관함 비밀번호 거부)", wt.includes("맞지 않습니다"), wt);
  await p2.click("#wc-sample"); await p2.waitForTimeout(200);
  await p2.click("#run-collect");
  await p2.waitForFunction(() => /수집 완료|승인 대기 \d+건/.test(document.getElementById("agent-out")?.textContent || "") && document.getElementById("agent-out").textContent.includes("스캔"), null, { timeout: 20000 });
  const outTxt = await p2.textContent("#agent-out");
  await p2.click("#tabs button[data-tab=disc]"); await p2.waitForTimeout(200);
  const pend = (await p2.$$("#p-disc [data-approve]")).length, kinds = await p2.$$eval("#p-disc tbody tr td:first-child .pill", (x) => x.map((e) => e.textContent));
  ok("웹 수집 Agent 실행 → 승인 대기 등록", sel.includes("13개") && /파일 13개 스캔 → 승인 대기 5건.*이미 관리 중 6건, 실패 2건.*보관함 비밀번호로 연 PFX 1개/.test(outTxt) && pend === 5, `${outTxt.slice(0, 90)} · 대기 ${pend}건 [${[...new Set(kinds)].join(",")}]`);
  // 시연 데이터에는 같은 이름의 더 최신 인증서가 이미 있음 → 검증용 파일은 '이전 버전'으로 분류, 승인 막고 반려만
  const apDis = await p2.$$eval("#p-disc [data-approve]", (b) => b.every((x) => x.disabled));
  const certsBefore = await p2.evaluate(() => S.data.certificates.length);
  await p2.click("#p-disc [data-reject]"); await p2.waitForTimeout(300);
  const pend2 = (await p2.$$("#p-disc [data-approve]")).length, certsAfter = await p2.evaluate(() => S.data.certificates.length);
  ok("이전 버전 파일: 승인 차단(중복 방지) → 반려 처리", apDis && kinds.every((k) => k === "이전 버전") && pend2 === pend - 1 && certsAfter === certsBefore, `대기 ${pend} → ${pend2} · 인증서 ${certsBefore} → ${certsAfter}`);
  await p2.click("#tabs button[data-tab=hist]"); const h = await p2.textContent("#p-hist");
  ok("수집 실행 이력·감사 로그 기록(승인/반려 구분)", h.includes("pem (검증용 13개 파일)") && h.includes("(웹)") && h.includes("수집 반려"), "scan_runs·audit");
  await p2.click("#tabs button[data-tab=dash]"); await p2.screenshot({ path: "/tmp/claude-0/-home-claude/92d09200-f21f-5312-b63e-f3817c0470d9/scratchpad/wc_dash.png" });
  console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
  fs.writeFileSync(path.join(__dirname, "..", "results", "e2e_webdemo.json"), JSON.stringify(R, null, 1));
  await b.close(); srv.close();
})();

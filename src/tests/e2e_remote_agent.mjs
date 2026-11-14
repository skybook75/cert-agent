// 서버 Agent E2E: 원격 서버(web01·was01)를 흉내 낸 폴더에서 Agent 가 수집해 플랫폼에 올리고,
// PFX 비밀번호는 암호화 전달로 받고, 승인된 운영 배포를 실제 TLS 서버에 설치·검사·응답 확인, 잘못된 인증서는 자동 원복한다.
//   node tests/e2e_remote_agent.mjs
import { createRequire } from "module";
import { spawn, execFileSync } from "child_process";
import fs from "fs";
import path from "path";

const { chromium } = createRequire(import.meta.url)("playwright");
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const W = "/tmp/cert_remote", PORT = 8788, TLS = 9443, BASE = `http://localhost:${PORT}/`;
fs.rmSync(W, { recursive: true, force: true }); fs.mkdirSync(W, { recursive: true });
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: String(d) }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${String(d).slice(0, 200)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const py = (...a) => execFileSync("python3", a, { cwd: ROOT }).toString();
const agent = (host, cmd) => { try { return execFileSync("python3", ["remote/cert_agent.py", cmd], { cwd: ROOT, env: { ...process.env, CERT_AGENT_HOME: path.join(W, "agent_" + host) } }).toString(); } catch (e) { return String(e.stdout) + String(e.stderr); } };

py("tests/remote/make_remote.py", W, "init");
fs.mkdirSync(path.join(W, "scan"), { recursive: true });
const srv = spawn("python3", ["platform_server.py", "--db", path.join(W, "p.db"), "--scan", path.join(W, "scan"), "--port", String(PORT), "--no-browser", "--vault-key", path.join(W, "vault.key"), "--deploy", path.join(W, "deploy")], { cwd: ROOT, stdio: "ignore" });
const LIVE = path.join(W, "web01/etc/nginx/ssl/shop");
const tls = spawn("python3", ["tests/remote/tls_server.py", path.join(LIVE, "fullchain.pem"), path.join(LIVE, "privkey.key"), String(TLS)], { cwd: ROOT, stdio: "ignore" });
await sleep(1500);
const browser = await chromium.launch();
const page = async () => { const c = await browser.newContext({ bypassCSP: true }); await c.route(/fonts\./, (r) => r.abort()); const p = await c.newPage(); p.on("pageerror", (e) => console.log("pageerror", e.message)); return p; };
const settle = async (p) => { await p.waitForLoadState("networkidle"); await sleep(250); };
const api = (p, method, url, body) => p.evaluate(async ({ method, url, body }) => { const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined }); return { status: r.status, body: await r.json().catch(() => null) }; }, { method, url, body });
async function login(p, u, pw, np) { await p.goto(BASE); await settle(p); await p.fill("#a-user", u); await p.fill("#a-pw", pw); await p.click("#auth-form button[type=submit]"); await settle(p);
  if (np && await p.isVisible("#a-old")) { await p.fill("#a-old", pw); await p.fill("#a-pw", np); await p.fill("#a-pw2", np); await p.click("#auth-form button[type=submit]"); await settle(p); } }
const tab = async (p, name) => { await p.locator("#tabs button").filter({ hasText: new RegExp(`^${name}\\s*\\d*$`) }).first().click(); await sleep(200); };
const toast = async (p) => { await p.waitForSelector("#toast:not([hidden])", { timeout: 15000 }); const t = await p.textContent("#toast"); await p.evaluate(() => ($("toast").hidden = true)); return t; };

try {
  const A = await page();
  await A.goto(BASE); await settle(A);
  await A.evaluate(async () => fetch("api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", name: "김관리", email: "admin@demo.test", password: "Admin2026" }) }));
  await login(A, "admin", "Admin2026");
  for (const [u, n, r] of [["rev", "이검토", "검토자"], ["apr", "최승인", "승인자"]]) await api(A, "POST", "api/users", { username: u, name: n, email: `${u}@demo.test`, role: r, password: "Temp2026a" });
  // ── 서버 Agent 등록 (관리 탭) — 토큰은 한 번만 표시
  const regs = {};
  for (const [h, st] of [["web01", "prod"], ["was01", ""]]) {
    const r = await api(A, "POST", "api/agents", { host: h, name: h, os: "Linux", deploy_stage: st }); regs[h] = r.body;
    const scan = h === "web01" ? [path.join(W, "web01/etc/nginx/ssl")] : [path.join(W, "was01/opt/tomcat/conf/ssl")];
    fs.mkdirSync(path.join(W, "agent_" + h), { recursive: true });
    fs.writeFileSync(path.join(W, "agent_" + h, "agent.json"), JSON.stringify({ platform: BASE, token: r.body.token, scan,
      service: h === "web01" ? { check: `python3 tests/remote/check_pair.py ${path.join(LIVE, "fullchain.pem")} ${path.join(LIVE, "privkey.key")}`, reload: "true", verify: `127.0.0.1:${TLS}` } : {} }));
  }
  const ag = await api(A, "GET", "api/agents");
  ok("서버 Agent 등록(토큰 1회 표시, 목록엔 해시도 안 보임)", regs.web01.token && ag.body.length === 2 && !JSON.stringify(ag.body).includes(regs.web01.token) && !JSON.stringify(ag.body).includes("token_hash"), ag.body.map((a) => `${a.host}:${a.deploy_stage || "-"}`).join(", "));
  const bad = await fetch(BASE + "api/agent-api/deploy-tasks", { headers: { Authorization: "Agent " + "x".repeat(40) } }); await bad.text();
  ok("잘못된 토큰 거부", bad.status === 401, bad.status);
  // ── PFX 비밀번호: was01 폴더 범위로 보관함 등록
  const was = path.join(W, "was01/opt/tomcat/conf/ssl").replace(/^\//, "");
  const sec = await api(A, "POST", "api/secrets", { scope: "folder", scope_value: `was01/${was}`, label: "was01 Tomcat 키스토어", password: "Tomcat!2026" });
  ok("보관함 등록(호스트/경로 범위)", sec.status === 200, sec.body?.scope_value);
  // ── 수집
  const o1 = agent("web01", "collect"), o2 = agent("was01", "collect");
  console.log(o1 + o2);
  ok("web01 수집: 체인 2건(서버+중간 CA) 승인 대기, 개인키 파일은 대상 아님", /pending=2 /.test(o1), o1.match(/upload:[^\n]*/)?.[0]);
  ok("was01 수집: PFX 비밀번호를 Agent 공개키로 암호화 전달받아 열기", /vault: received wrapped secrets for 1\/1/.test(o2) && /pending=1 /.test(o2), o2.match(/upload:[^\n]*/)?.[0]);
  const dbTxt = execFileSync("python3", ["tools/dbdump.py", path.join(W, "p.db")], { cwd: ROOT }).toString();
  ok("DB·Agent 로그에 비밀번호 평문 없음", !dbTxt.includes("Tomcat!2026") && !o2.includes("Tomcat!2026"));
  await A.reload(); await settle(A); await tab(A, "수집 승인");
  const locs = await A.textContent("#p-disc");
  ok("위치는 '호스트:경로'로 기록", locs.includes("web01:") && locs.includes("was01:"), (locs.match(/web01:[^\s]*/) || [""])[0].slice(0, 80));
  const decideAll = async () => { let n = 0; while (await A.$("[data-approve]:not([disabled])")) { const id = await A.$eval("[data-approve]:not([disabled])", (b) => b.dataset.approve);
    const inp = await A.$(`[data-owner="${id}"]`); if (!(await inp.inputValue())) await inp.fill("김담당"); await A.click(`[data-approve="${id}"]`); await toast(A); await settle(A); n++; } return n; };
  const n1 = await decideAll();
  ok("승인 → 인증서 등록(공개 인증서 PEM 보관)", n1 === 3 && (await api(A, "GET", "api/db/certificates")).body.every((c) => c.pem?.includes("BEGIN CERTIFICATE")), `${n1}건`);
  // ── 갱신본 → 4단계 배포 → 서버 적용
  const fpNew = JSON.parse(py("tests/remote/make_remote.py", W, "renew")).renew;
  const o3 = agent("web01", "collect");
  ok("새 인증서를 incoming 에 두면 갱신본으로 인식", /renewal=1 /.test(o3), o3.match(/upload:[^\n]*/)?.[0]);
  await A.reload(); await settle(A); await tab(A, "수집 승인"); await decideAll();
  const RV = await page(), AP = await page();
  await login(RV, "rev", "Temp2026a", "Review2026"); await login(AP, "apr", "Temp2026a", "Approve2026");
  const pipeline = async (whenProd) => { const res = [];
    for (const st of ["개발", "테스트", "교육", "운영"]) {
      await A.reload(); await settle(A); await tab(A, "인증서"); await A.click("#p-certs tr[data-cert] >> text=shop.demo.test"); await sleep(200);
      if (await A.$eval("#d-deploy", (b) => b.disabled)) { res.push(st + ":요청불가 " + (await A.textContent("#cert-detail")).match(/검토 자동 검증[^\n]*/)?.[0]); break; }
      await A.click("#d-deploy"); await toast(A);
      await RV.reload(); await settle(RV); await tab(RV, "배포"); await RV.click("[data-rvpass]"); await toast(RV);
      await AP.reload(); await settle(AP); await tab(AP, "배포"); if (st === "운영" && whenProd) { await AP.selectOption("[data-whensel]", "custom"); await AP.fill("[data-when]", whenProd); }
      await AP.click("[data-approve-dep]"); const t = await toast(AP); res.push(st + ":" + (/반영|승인 완료/.test(t) ? "완료" : t)); }
    return res; };
  const p1 = await pipeline();
  ok("갱신본 4단계 승인(플랫폼)", p1.every((x) => x.endsWith("완료")) && p1.length === 4, p1.join(" "));
  const d1 = agent("web01", "deploy"); console.log(d1);
  const served = execFileSync("sh", ["-c", `echo | openssl s_client -connect 127.0.0.1:${TLS} -servername shop.demo.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256`]).toString().trim().split("=")[1];
  ok("배포 Agent: 설치 → 설정 검사 → 재적용 → 실제 응답 지문 = 배포본", /report: .*ok=true/.test(d1) && /verify: .* MATCH/.test(d1) && served === fpNew, `${served?.slice(0, 23)}… · ${d1.match(/verify:[^\n]*/)?.[0]}`);
  ok("체인 유지: 맨 앞 서버 인증서만 교체, 중간 CA 유지", (fs.readFileSync(path.join(LIVE, "fullchain.pem"), "utf8").match(/BEGIN CERTIFICATE/g) || []).length === 2);
  const env1 = (await api(A, "GET", "api/db/environments")).body.find((e) => e.stage === "prod");
  ok("플랫폼에 서버 적용 결과 기록", env1.applied?.web01?.ok === true, env1.applied?.web01?.detail);
  const d1b = agent("web01", "deploy");
  ok("이미 적용된 배포는 다시 하지 않음", /deploy: no approved task/.test(d1b));
  // ── 잘못된 인증서(키 불일치) → 설정 검사 실패 → 자동 원복
  py("tests/remote/make_remote.py", W, "badrenew");
  agent("web01", "collect"); await A.reload(); await settle(A); await tab(A, "수집 승인"); await decideAll();
  // 야간 작업 창: 운영 단계를 '작업 시각'으로 예약 승인 → 그 전에는 서버 Agent에 작업이 내려가지 않고, 시각이 지나면 수행
  const at = new Date(Math.ceil((Date.now() + 75e3) / 60e3) * 60e3), pad = (n) => String(n).padStart(2, "0");
  const whenStr = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`;
  const p2 = await pipeline(whenStr);
  const d2pre = agent("web01", "deploy");
  await A.reload(); await settle(A); await tab(A, "배포");
  const sched = await A.textContent("#p-deploy");
  ok("야간 작업 창: 작업 시각 예약 승인 → 시각 전에는 서버 Agent에 작업 없음 · 배포 화면에 '예약' 표시", /deploy: no approved task/.test(d2pre) && /예약 \d{4}-\d{2}-\d{2} \d{2}:\d{2} KST/.test(sched), `${p2.join(" ")} · ${(sched.match(/예약 [^작]*/) || [""])[0]}`);
  while (Date.now() < at.getTime() + 2000) await sleep(1000);
  const d2 = agent("web01", "deploy"); console.log(d2);
  const served2 = execFileSync("sh", ["-c", `echo | openssl s_client -connect 127.0.0.1:${TLS} -servername shop.demo.test 2>/dev/null | openssl x509 -noout -fingerprint -sha256`]).toString().trim().split("=")[1];
  const env2 = (await api(A, "GET", "api/db/environments")).body.find((e) => e.stage === "prod");
  ok("키가 맞지 않는 인증서: 설정 검사 실패 → 백업으로 자동 원복, 서비스는 기존 인증서 유지", /configtest: .* rc=[1-9]/.test(d2) && /rollback: failure detected/.test(d2) && served2 === fpNew && env2.applied?.web01?.ok === false,
    `${p2.join(" ")} · ${env2.applied?.web01?.detail?.slice(0, 120)}`);
  await A.reload(); await settle(A); await tab(A, "배포");
  const board = await A.textContent("#p-deploy");
  ok("배포 화면 '서버 적용' 열: 운영 web01 실패·원복 표시", board.includes("web01 실패·원복"), (board.match(/web01 [^\d]{2,8}/) || [""])[0]);
  await tab(A, "관리");
  await A.fill("#ag-host", "win01"); await A.selectOption("#ag-os", "Windows"); await A.click("#ag-form button[type=submit]"); await A.waitForSelector("#ag-token:not([hidden])");
  const adm = await A.textContent("#ag-card");
  ok("관리 탭: 서버 Agent 목록(최근 결과)·화면에서 등록 시 토큰과 설정 파일 1회 표시", /서버 Agent 3대/.test(adm) && adm.includes("배포 운영 shop.demo.test 실패·자동 원복") && /"token": "[A-Za-z0-9_-]{30,}"/.test(adm), adm.match(/서버 Agent \d대/)?.[0]);
  await A.locator("#ag-card").screenshot({ path: path.join(ROOT, "results", "acceptance", "shots", "R01_admin_agents.png") });
  const aud = (await api(A, "GET", "api/db/audit")).body.filter((a) => /서버/.test(a.action) || /PFX 비밀번호 전달/.test(a.action)).map((a) => a.action);
  ok("감사 로그: 서버 Agent 등록·비밀번호 암호화 전달·서버 적용·적용 실패", ["서버 Agent 등록", "PFX 비밀번호 전달(암호화)", "서버 적용", "서버 적용 실패"].every((k) => aud.includes(k)), [...new Set(aud)].join(", "));
} catch (e) { console.error(e); ok("실행 오류 없음", false, e.message); }
finally {
  console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
  fs.writeFileSync(path.join(ROOT, "results", "e2e_remote_agent.json"), JSON.stringify(R, null, 1));
  await browser.close(); srv.kill(); tls.kill();
}

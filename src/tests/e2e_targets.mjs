// 수집 대상 서버 목록 E2E: 등록(접속 비밀번호 암호화) · 권한(관리자 전체/담당자 본인) · 서버 Agent가 플랫폼 경로로 수집 · '수집 실행' 요청 → poll
import { createRequire } from "module";
import { spawn, execFileSync } from "child_process";
import fs from "fs";
import path from "path";

const { chromium } = createRequire(import.meta.url)("playwright");
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const W = "/tmp/cert_targets", PORT = 8794, BASE = `http://localhost:${PORT}/`;
fs.rmSync(W, { recursive: true, force: true }); fs.mkdirSync(W, { recursive: true });
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: String(d) }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${String(d).slice(0, 200)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
execFileSync("python3", ["tests/remote/make_remote.py", W, "init"], { cwd: ROOT });
const srv = spawn("python3", ["platform_server.py", "--db", path.join(W, "p.db"), "--port", String(PORT), "--no-browser", "--vault-key", path.join(W, "v.key"), "--deploy", path.join(W, "d")], { cwd: ROOT, stdio: "ignore" });
await sleep(1500);
const agent = (cmd) => { try { return execFileSync("python3", ["remote/cert_agent.py", cmd], { cwd: ROOT, env: { ...process.env, CERT_AGENT_HOME: path.join(W, "ag") } }).toString(); } catch (e) { return String(e.stdout) + String(e.stderr); } };
const browser = await chromium.launch();
const mk = async () => { const c = await browser.newContext(); await c.route(/fonts\./, (r) => r.abort()); return c.newPage(); };
const settle = async (p) => { await p.waitForLoadState("networkidle"); await sleep(300); };
const api = (p, method, url, body) => p.evaluate(async ({ method, url, body }) => { const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined }); return { status: r.status, body: await r.json().catch(() => null) }; }, { method, url, body });
async function login(p, u, pw, np) { await p.goto(BASE); await settle(p); await p.fill("#a-user", u); await p.fill("#a-pw", pw); await p.click("#auth-form button[type=submit]"); await settle(p);
  if (np && await p.isVisible("#a-old")) { await p.fill("#a-old", pw); await p.fill("#a-pw", np); await p.fill("#a-pw2", np); await p.click("#auth-form button[type=submit]"); await settle(p); } }
try {
  const A = await mk(); await A.goto(BASE); await settle(A);
  await A.evaluate(async () => fetch("api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", name: "김관리", email: "a@x.test", password: "Admin2026" }) }));
  await login(A, "admin", "Admin2026");
  for (const [u, n] of [["own", "김담당"], ["own2", "박담당"]]) await api(A, "POST", "api/users", { username: u, name: n, email: `${u}@x.test`, role: "담당자", password: "Temp2026a" });
  const reg = await api(A, "POST", "api/agents", { host: "web01", name: "web01", os: "Linux", deploy_stage: "" });
  fs.mkdirSync(path.join(W, "ag"), { recursive: true });
  fs.writeFileSync(path.join(W, "ag", "agent.json"), JSON.stringify({ platform: BASE, token: reg.body.token, scan: [], service: {} }));   // 로컬 경로 없음 → 플랫폼 목록만 사용
  // 담당자가 화면에서 등록
  const O = await mk(); await login(O, "own", "Temp2026a", "Own2026aa");
  await O.locator("#tabs button[data-tab=targets]").click(); await settle(O);
  await O.fill("#tg-name", "쇼핑몰 웹"); await O.fill("#tg-host", "web01"); await O.fill("#tg-paths", path.join(W, "web01/etc/nginx/ssl"));
  await O.click("#tg-form button[type=submit]"); await settle(O); await sleep(400);
  await O.fill("#tg-name", "레거시 WAS(SSH)"); await O.fill("#tg-host", "10.0.9.9"); await O.selectOption("#tg-method", "ssh");
  await O.fill("#tg-user", "certread"); await O.fill("#tg-pw", "Ssh!Secret-77"); await O.fill("#tg-paths", "/opt/was/ssl");
  await O.click("#tg-form button[type=submit]"); await settle(O); await sleep(400);
  const otxt = await O.textContent("#p-targets");
  ok("담당자: 수집 대상 2개 등록(서버 Agent · SSH 접속), 비밀번호는 암호화 표시만", /쇼핑몰 웹/.test(otxt) && /레거시 WAS/.test(otxt) && /🔒/.test(otxt) && !otxt.includes("Ssh!Secret-77"), (otxt.match(/수집 대상 \d+개[^)]*\)/) || [""])[0]);
  const db = execFileSync("python3", ["tools/dbdump.py", path.join(W, "p.db")], { cwd: ROOT }).toString();
  ok("DB에 접속 비밀번호 평문 없음(AES-256-GCM)", !db.includes("Ssh!Secret-77"));
  const list = (await api(O, "GET", "api/targets")).body;
  ok("API 응답에 암호문도 내보내지 않음(has_secret 만)", list.every((t) => !("enc" in t)) && list.some((t) => t.has_secret), list.map((t) => `${t.name}:${t.has_secret}`).join(", "));
  // 다른 담당자는 안 보임, 관리자는 전체
  const O2 = await mk(); await login(O2, "own2", "Temp2026a", "Own2026bb");
  const l2 = (await api(O2, "GET", "api/targets")).body;
  const del2 = await api(O2, "POST", `api/targets/${list[0].id}/delete`);
  const la = (await api(A, "GET", "api/targets")).body;
  ok("권한: 다른 담당자는 목록 0개·삭제 불가(404), 관리자는 전체(등록자 표시)", l2.length === 0 && del2.status === 404 && la.length === 2, `own2 ${l2.length}개 · 삭제 ${del2.status} · admin ${la.length}개`);
  const rv = await api(A, "POST", "api/users", { username: "rev", name: "이검토", email: "r@x.test", role: "검토자", password: "Temp2026a" });
  // 서버 Agent: 로컬 설정 없이 플랫폼 경로로 수집
  const o1 = agent("collect");
  ok("서버 Agent가 플랫폼에 등록된 경로를 받아 수집(로컬 설정 불필요)", /config: 1 scan path/.test(o1) && /pending=2 /.test(o1), o1.match(/upload:[^\n]*/)?.[0]);
  // '수집 실행' 요청 → poll
  await O.locator("#tabs button[data-tab=targets]").click(); await settle(O);
  const webRow = O.locator("#p-targets tr", { hasText: "쇼핑몰 웹" });
  await webRow.locator("[data-trun]").click(); await settle(O); await sleep(400);
  const queued = await O.textContent("#p-targets");
  const p1 = agent("poll");
  await O.reload(); await settle(O); await O.locator("#tabs button[data-tab=targets]").click(); await settle(O);
  const after = await O.locator("#p-targets tr", { hasText: "쇼핑몰 웹" }).textContent();
  ok("'수집 실행' → 요청 대기 → Agent poll 이 가져가 실행 → 최근 수집 결과 표시", /실행 요청됨/.test(queued) && /collect: run requested target=/.test(p1) && /수집 \d+개/.test(after), after.replace(/\s+/g, " ").slice(0, 160));
  const p2 = agent("poll");
  ok("같은 요청은 한 번만 실행", !/collect: run requested/.test(p2));
  // SSH 방식: 접속 불가 주소 → 실패 사유 기록
  const sshRow = O.locator("#p-targets tr", { hasText: "레거시 WAS" });
  await sshRow.locator("[data-trun]").click(); await settle(O); await sleep(500);
  let sres;
  for (let i = 0; i < 40; i++) { sres = (await api(O, "GET", "api/targets")).body.find((t) => t.method === "ssh"); if (/실패|성공|수집/.test(sres.last_result || "")) break; await sleep(500); }
  ok("SSH 방식 실행: 실패하면 사유를 최근 결과에 기록(서비스 영향 없음)", /실패/.test(sres.last_result || ""), sres.last_result);
  // 수정: 비밀번호 비우면 유지
  await sshRow.locator("[data-tedit]").click(); await settle(O); await O.fill("#tg-memo", "운영 WAS 3호기"); await O.click("#tg-form button[type=submit]"); await settle(O);
  const s2 = (await api(O, "GET", "api/targets")).body.find((t) => t.method === "ssh");
  ok("수정 시 비밀번호 칸을 비우면 기존 암호화 비밀번호 유지", s2.has_secret && s2.memo === "운영 WAS 3호기");
  // 검토자는 접근 불가
  const RVp = await mk(); await login(RVp, "rev", "Temp2026a", "Rev2026aa");
  ok("검토자는 수집 대상 탭·API 없음(403)", !(await RVp.$("#tabs button[data-tab=targets]")) && (await api(RVp, "GET", "api/targets")).status === 403);
  const aud = (await api(A, "GET", "api/db/audit")).body.map((a) => a.action);
  ok("감사 로그: 등록·수정·수집 실행", ["수집 대상 등록", "수집 대상 수정", "수집 실행 요청", "수집 실행(SSH)"].every((k) => aud.includes(k)), [...new Set(aud)].filter((a) => /수집 대상|수집 실행/.test(a)).join(", "));
  await A.locator("#tabs button[data-tab=targets]").click(); await settle(A);
  await A.locator("#p-targets").screenshot({ path: path.join(ROOT, "results", "acceptance", "shots", "T01_targets.png") });
} catch (e) { console.error(e); ok("실행 오류 없음", false, e.message); }
finally {
  console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
  fs.writeFileSync(path.join(ROOT, "results", "e2e_targets.json"), JSON.stringify(R, null, 1));
  await browser.close(); srv.kill();
}

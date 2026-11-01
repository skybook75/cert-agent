// 브라우저 E2E: 실제 PC 서버 + Chromium 으로 5개 역할의 업무 흐름 전체를 화면에서 실행
//   node tests/e2e_browser.mjs [작업폴더]
// 시나리오: 최초 설정 → 사용자·SMTP → 관리대장 등록·대조(SC-001) → 수집·검토·승인 → 알림·메일(SC-002)
//          → 담당자 조회 범위 → 4단계 배포·직무 분리·원복 → 갱신본 → 보고서(SC-003) → 조회자 → 보안
import { createRequire } from "module";
import { spawn, execFileSync } from "child_process";
import fs from "fs";
import path from "path";

const { chromium } = createRequire(import.meta.url)("playwright");
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const W = path.resolve(process.argv[2] || "/tmp/cert_e2e");
const PORT = 8766, SMTP = 2526, BASE = `http://localhost:${PORT}/`;
const SHOTS = path.join(ROOT, "results", "screens");
fs.rmSync(W, { recursive: true, force: true }); fs.mkdirSync(W, { recursive: true }); fs.mkdirSync(SHOTS, { recursive: true });
const R = [];
const ok = (name, cond, detail = "") => { R.push({ name, pass: !!cond, detail: String(detail) }); console.log(`${cond ? "PASS" : "FAIL"} ${name} — ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const py = (...a) => execFileSync("python3", a, { cwd: ROOT }).toString();

py("tests/make_fixture.py", W);
const MAIL = path.join(W, "mail.jsonl");
const sink = spawn("python3", ["tests/smtp_sink.py", String(SMTP), MAIL], { cwd: ROOT, stdio: "ignore" });
const srv = spawn("python3", ["platform_server.py", "--db", path.join(W, "p.db"), "--scan", path.join(W, "pem"), "--deploy", path.join(W, "deploy"), "--port", String(PORT), "--no-browser", "--vault-key", path.join(W, "keys", "vault.key")], { cwd: ROOT, stdio: ["ignore", "ignore", "pipe"] });
let srvErr = ""; srv.stderr.on("data", (d) => (srvErr += d));
await sleep(1500);

const browser = await chromium.launch();
const steps = {};   // 사람이 화면에서 한 조작 수 (KPI 산정용)
const ctx = async (who) => {
  const c = await browser.newContext({ viewport: { width: 1360, height: 900 }, acceptDownloads: true, locale: "ko-KR", timezoneId: "Asia/Seoul" });
  await c.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());   // 외부 글꼴은 테스트에서 제외
  const p = await c.newPage(); p.on("pageerror", (e) => console.log(`[pageerror:${who}]`, e.message));
  p.on("dialog", (d) => d.dismiss());
  return p;
};
const shot = (p, name) => p.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: false });
const tab = async (p, name) => { await p.locator("#tabs button").filter({ hasText: new RegExp(`^${name}\\s*\\d*$`) }).first().click(); await sleep(150); };
const toastText = async (p) => { await p.waitForSelector("#toast:not([hidden])", { timeout: 8000 }); const t = await p.textContent("#toast"); await p.evaluate(() => ($("toast").hidden = true)); return t; };
const settle = async (p) => { await p.waitForLoadState("networkidle"); await sleep(250); };
const count = (k) => (steps[k] = (steps[k] || 0) + 1);
async function login(p, user, pw, newPw) {
  await p.goto(BASE); await settle(p);
  await p.fill("#a-user", user); await p.fill("#a-pw", pw); await p.click("#auth-form button[type=submit]"); await settle(p);
  if (newPw && await p.isVisible("#a-old")) {
    await p.fill("#a-old", pw); await p.fill("#a-pw", newPw); await p.fill("#a-pw2", newPw); await p.click("#auth-form button[type=submit]"); await settle(p);
  }
}
const apiIn = (p, method, url, body, csrf = true) => p.evaluate(async ({ method, url, body, csrf }) => {
  const me = await (await fetch("api/me")).json().catch(() => ({}));
  const h = { "Content-Type": "application/json" }; if (csrf && me.csrf) h["X-CSRF-Token"] = me.csrf;
  const r = await fetch(url, { method, headers: h, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
}, { method, url, body, csrf });
const tabNames = (p) => p.$$eval("#tabs button", (bs) => bs.map((b) => b.dataset.tab));

try {
  // ───────── 1. 최초 설정·로그인
  const A = await ctx("admin");
  await A.goto(BASE); await settle(A);
  ok("최초 실행 시 관리자 생성 화면", await A.isVisible("text=최초 관리자 계정 만들기"));
  await shot(A, "01_setup");
  await A.fill("#a-user", "admin"); await A.fill("#a-name", "김관리"); await A.fill("#a-mail", "admin@example.co.kr");
  await A.fill("#a-pw", "short"); await A.fill("#a-pw2", "short"); await A.click("#auth-form button[type=submit]"); await sleep(300);
  ok("비밀번호 정책(8자·영문+숫자) 검사", (await A.textContent("#a-err")).length > 0 || await A.$eval("#a-pw", (e) => !e.checkValidity()), "짧은 비밀번호 거부");
  await A.fill("#a-pw", "Admin1234"); await A.fill("#a-pw2", "Admin1234"); await A.click("#auth-form button[type=submit]"); await settle(A);
  await login(A, "admin", "Admin1234");
  ok("관리자 로그인 → 탭 11개", (await tabNames(A)).length === 11, (await tabNames(A)).join(","));

  // ───────── 2. 사용자·SMTP
  await tab(A, "관리");
  const USERS = [["kim", "김담당", "kim@example.co.kr", "담당자"], ["pay", "박결제", "pay@example.co.kr", "담당자"], ["rev", "이검토", "rev@example.co.kr", "검토자"],
    ["apr", "최승인", "apr@example.co.kr", "승인자"], ["view", "정조회", "view@example.co.kr", "조회자"]];
  for (const [u, n, m, r] of USERS) {
    await A.fill("#u-user", u); await A.fill("#u-name", n); await A.fill("#u-mail", m); await A.selectOption("#u-role", r); await A.fill("#u-pw", "Temp1234");
    await A.click("#u-form button[type=submit]"); await toastText(A); await settle(A);
  }
  ok("사용자 5명 추가(역할별)", (await A.$$("[data-urole]")).length === 6, `${(await A.$$("[data-urole]")).length}명`);
  await A.check("#s-en"); await A.fill("#s-host", "127.0.0.1"); await A.fill("#s-port", String(SMTP)); await A.selectOption("#s-sec", "none");
  await A.fill("#s-from", "cert-platform@example.co.kr"); await A.click("#smtp-form button[type=submit]"); await toastText(A);
  await A.fill("#s-to", "admin@example.co.kr"); await A.click("#s-test"); const tt = await toastText(A);
  ok("SMTP 설정·테스트 발송", tt.includes("보냈습니다"), tt);
  await shot(A, "02_admin_users");

  // ───────── 3. 관리대장 등록 (SC-001)
  await tab(A, "관리대장");
  await A.setInputFiles("#xl-file", { name: "관리대장.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: fs.readFileSync(path.join(W, "관리대장.xlsx")) }); await sleep(500);
  const pv = await A.textContent("#p-ledger");
  ok("엑셀 형식 검증: 정상 4·오류 2", pv.includes("정상 4건") && pv.includes("오류 2건") && pv.includes("날짜 형식이 아닙니다") && pv.includes("필수 값이 비어"), "만료일 '2026-13-01', 담당자 누락 행");
  await shot(A, "03_ledger_validate");
  await A.click("#xl-commit"); const t3 = await toastText(A); await settle(A);
  ok("검증된 4건만 등록", t3.includes("신규 4건"), t3); count("관리대장 등록");
  ok("수집 전 대조: 미수집 4", (await A.textContent("#p-ledger")).includes("미수집 4"));

  // ───────── 4. 수집 Agent
  // PFX 비밀번호: 담당자·관리자가 플랫폼 보관함에 입력 → 서버가 AES-256-GCM 으로 암호화해 보관 → 수집 Agent 가 사용
  await tab(A, "대시보드");
  await A.click("#run-collect"); await settle(A); await sleep(300);
  const out0 = await A.textContent("#agent-out");
  await tab(A, "PFX 비밀번호");
  const needTxt = await A.textContent("#p-vault");
  ok("비밀번호 없는 PFX → '비밀번호가 필요한 파일'로 안내", needTxt.includes("검증/pay-d7.pfx") && needTxt.includes("비밀번호 필요") && out0.includes("실패 3건"), out0.slice(0, 80));
  await A.click("[data-vneed='0']"); await sleep(100);
  const pre = await A.inputValue("#vs-path");
  await A.fill("#vs-p1", "Wrong#999"); await A.fill("#vs-p2", "Wrong#999"); await A.click("#vs-go"); await toastText(A); await settle(A);
  await tab(A, "대시보드"); await A.click("#run-collect"); await settle(A); await sleep(300);
  const outW = await A.textContent("#agent-out");
  ok("틀린 비밀번호 → '비밀번호 불일치(보관함 1개 시도)'", outW.includes("비밀번호 불일치(보관함 1개 시도)"), outW.match(/pay-d7\.pfx\([^)]*\)\)?/)?.[0]);
  await tab(A, "PFX 비밀번호");
  await A.click("[data-vedit]"); await A.fill("#vr-pw", "Test#1234"); await A.click("[data-vsave]"); await toastText(A); await settle(A); count("PFX 비밀번호 등록");
  await tab(A, "대시보드");
  await A.click("#run-collect"); await settle(A); await sleep(400);
  const out = await A.textContent("#agent-out");
  const pendAll = (await apiIn(A, "GET", "api/db/discoveries")).body.filter((d) => d.status === "대기").length;
  ok("수집 Agent: 13개 파일 → 승인 대기 누적 10건, 실패 2건(재실행해도 중복 없음)", out.includes("파일 13개") && out.includes("승인 대기 1건") && out.includes("이미 관리 중 10건") && out.includes("실패 2건") && pendAll === 10, `승인 대기 누적 ${pendAll}건 · ${out.slice(0, 90)}`); count("수집 실행");
  ok("보관함 비밀번호로 PFX 열기(파일 범위 자동 채움 → 교체 후 성공)", pre === "검증/pay-d7.pfx" && out.includes("보관함 비밀번호로 연 PFX 1개"), pre);
  const pwLeak = execFileSync("sh", ["-c", `grep -c -a -e "Test#1234" -e "Wrong#999" ${path.join(W, "p.db")} || true`]).toString().trim();
  const secList = await apiIn(A, "GET", "api/secrets"), secRaw = await apiIn(A, "GET", "api/db/secrets");
  const enc = JSON.parse(execFileSync("python3", ["-c", `import sqlite3,json;print([json.loads(r[0]) for r in sqlite3.connect(${JSON.stringify(path.join(W, "p.db"))}).execute("select data from docs where coll='secrets'")][0]['enc'] and json.dumps([json.loads(r[0]) for r in sqlite3.connect(${JSON.stringify(path.join(W, "p.db"))}).execute("select data from docs where coll='secrets'")][0]['enc']))`]).toString());
  const keyInDb = execFileSync("sh", ["-c", `grep -c -a "$(cat ${path.join(W, "keys", "vault.key")})" ${path.join(W, "p.db")} || true`]).toString().trim();
  ok("DB에는 암호문만(AES-256-GCM), 평문·키 없음", pwLeak === "0" && keyInDb === "0" && enc.alg === "AES-256-GCM" && enc.ct && enc.nonce, `평문 ${pwLeak}건 · 키 ${keyInDb}건 · ${enc.alg} nonce ${enc.nonce}`);
  ok("API로 평문·암호문 조회 불가", secRaw.status === 403 && !JSON.stringify(secList.body).includes("Test#1234") && !JSON.stringify(secList.body).includes(enc.ct) && secList.body.items[0].use_count === 1,
    `api/db/secrets ${secRaw.status} · 목록 항목 ${Object.keys(secList.body.items[0]).join(",")}`);
  const audV = (await apiIn(A, "GET", "api/db/audit")).body.filter((a) => /PFX 비밀번호/.test(a.action));
  ok("등록·교체 이력 기록(값 없이)", audV.length === 2 && !JSON.stringify(audV).includes("Test#1234"), audV.map((a) => `${a.action}: ${a.detail}`).join(" / "));
  await shot(A, "04b_vault");
  await tab(A, "관리대장");
  const rc = await A.textContent("#p-ledger");
  ok("대조 결과: 일치 2·불일치 1·미수집 1·신규 7", rc.includes("일치 2") && rc.includes("불일치 1") && rc.includes("미수집 1") && rc.includes("신규 7"),
    rc.match(/(불일치|미수집|신규|갱신본|일치|확인 완료) \d+/g)?.join(" "));
  ok("불일치 변경 전·후 표시(api 만료일)", /만료일: \d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}/.test(rc));
  await shot(A, "04_reconcile");
  const certsBefore = (await apiIn(A, "GET", "api/db/certificates")).body.filter((c) => c.fingerprint).length;
  ok("승인 전 관리정보 미변경", certsBefore === 0, `수집 정보가 연결된 인증서 ${certsBefore}건`);

  // ───────── 5. 검토자: 수집 검토 의견
  const RV = await ctx("reviewer");
  await login(RV, "rev", "Temp1234", "Review1234");
  ok("임시 비밀번호 → 첫 로그인 변경 강제 후 진입", (await tabNames(RV)).length === 9, (await tabNames(RV)).join(","));
  await tab(RV, "수집 승인");
  ok("검토자는 승인 버튼 비활성", await RV.$eval("[data-approve]", (b) => b.disabled));
  let n5 = 0; while (await RV.$("[data-rv][data-v='적합']") && n5 < 15) { await RV.click("[data-rv][data-v='적합']"); await toastText(RV); await settle(RV); n5++; count("수집 검토"); }
  ok("검토 의견 10건 기록", n5 === 10, `${n5}건`);

  // ───────── 6. 승인자: 수집 승인
  const AP = await ctx("approver");
  await login(AP, "apr", "Temp1234", "Approve1234");
  await tab(AP, "수집 승인");
  const OWN = { "pay.example.co.kr": "박결제" };
  let n6 = 0;
  while (await AP.$("[data-approve]:not([disabled])") && n6 < 15) {
    const id = await AP.$eval("[data-approve]:not([disabled])", (b) => b.dataset.approve);
    const cn = await AP.$eval(`[data-approve="${id}"]`, (b) => b.closest("tr").children[1].firstChild.textContent);
    const inp = await AP.$(`[data-owner="${id}"]`);
    if (!(await inp.inputValue())) await inp.fill(OWN[cn] || "김담당");
    await AP.click(`[data-approve="${id}"]`); await toastText(AP); await settle(AP); n6++; count("수집 승인");
  }
  const certs6 = (await apiIn(AP, "GET", "api/db/certificates")).body;
  const portal = certs6.find((c) => c.subject_cn === "portal.example.co.kr"), api6 = certs6.find((c) => c.subject_cn === "api.example.co.kr");
  ok("10건 승인 → 인증서 11건(대장 미수집 1 포함)", n6 === 10 && certs6.length === 11, `승인 ${n6}, 인증서 ${certs6.length}`);
  ok("일치 건: 대장 담당자 유지 + 수집 메타 연결", portal.source === "관리대장+수집" && portal.owner === "김담당" && !!portal.fingerprint, `${portal.source} · ${portal.owner}`);
  ok("불일치 건: 수집 만료일로 대장 갱신, 차이 기록", api6.registry_diffs?.[0]?.label === "만료일" && api6.owner === "이연동", JSON.stringify(api6.registry_diffs?.[0]));
  const dsc = (await apiIn(AP, "GET", "api/db/discoveries")).body;
  ok("처리자는 세션 기준 기록(화면 값 무시)", dsc.every((d) => d.decided_by === "최승인(승인자)"), dsc[0]?.decided_by);

  // ───────── 7. 알림·메일 (SC-002)
  await A.reload(); await settle(A);
  await tab(A, "인증서"); await A.click(`#p-certs tr[data-cert] >> text=pay.example.co.kr`); await sleep(200);
  await A.fill("#d-mail", "pay@example.co.kr"); await A.click("#d-save"); await toastText(A); await settle(A);
  await tab(A, "대시보드"); await A.click("#run-notify"); const t7 = await toastText(A); await settle(A);
  const notis = (await apiIn(A, "GET", "api/db/notifications")).body;
  const lv = {}; notis.forEach((n) => (lv[n.level] = (lv[n.level] || 0) + 1));
  ok("알림 Agent: D-7·D-30·만료 단계별 생성", lv["D-7"] === 1 && lv["D-30"] === 1 && lv["만료"] >= 6, `${t7} ${JSON.stringify(lv)}`);
  await tab(A, "알림함"); await A.click("#mail-all"); const t7b = await toastText(A); await settle(A);
  const mails = fs.existsSync(MAIL) ? fs.readFileSync(MAIL, "utf8").trim().split("\n").map((l) => JSON.parse(l)) : [];
  const to = mails.flatMap((m) => m.to);
  ok("SMTP 발송: 이메일 등록 담당자에게 실제 발송", to.includes("pay@example.co.kr") && to.includes("lee@example.co.kr") && to.includes("kim@example.co.kr"), `${t7b} · 수신 ${[...new Set(to)].join(", ")}`);
  const payMail = mails.find((m) => m.to.includes("pay@example.co.kr"));
  ok("메일 본문: 만료일·남은 일수·갱신 안내", payMail && /만료일은 \d{4}-\d{2}-\d{2}/.test(payMail.body) && payMail.subject.includes("[D-7]"), payMail?.subject);
  const failed = (await apiIn(A, "GET", "api/db/notifications")).body.filter((n) => n.mail_status === "실패");
  ok("이메일 미등록 건은 발송하지 않고 사유 기록", failed.every((n) => /이메일/.test(n.mail_error)), `실패 ${failed.length}건: ${failed[0]?.mail_error || "-"}`);
  await shot(A, "05_notifications");

  // ───────── 8. 담당자 조회 범위 (SC-003)
  const PO = await ctx("owner");
  await login(PO, "pay", "Temp1234", "Owner1234");
  ok("담당자 탭 8개(관리대장·수집 승인·관리 없음, PFX 비밀번호 있음)", (await tabNames(PO)).length === 8 && (await tabNames(PO)).includes("vault"), (await tabNames(PO)).join(","));
  const poAll = await apiIn(PO, "POST", "api/secrets", { scope: "all", scope_value: "", label: "x", password: "Pw#1" });
  const poAdd = await apiIn(PO, "POST", "api/secrets", { scope: "folder", scope_value: "결제", label: "결제 폴더", password: "Pay#2026" });
  const poBad = await apiIn(PO, "POST", "api/secrets", { scope: "folder", scope_value: "../etc", label: "x", password: "Pw#1" });
  const poList = (await apiIn(PO, "GET", "api/secrets")).body.items;
  const adminId = secList.body.items[0].id;
  const poDelOther = await apiIn(PO, "POST", `api/secrets/${adminId}/delete`, {});
  const poDel = await apiIn(PO, "POST", `api/secrets/${poAdd.body.id}/delete`, {});
  ok("담당자: 본인 등록분만 조회·삭제, 전체 범위·상위 경로 차단", poAll.status === 403 && poAdd.status === 200 && poBad.status === 400 && poList.length === 1 && poDelOther.status === 404 && poDel.status === 200,
    `전체 ${poAll.status} · 등록 ${poAdd.status} · ../ ${poBad.status} · 목록 ${poList.length} · 타인 삭제 ${poDelOther.status} · 본인 삭제 ${poDel.status}`);
  await tab(PO, "인증서");
  const rowsPO = await PO.$$eval("#p-certs tbody tr[data-cert]", (t) => t.map((r) => r.children[0].firstChild.textContent));
  ok("담당자는 본인 인증서만 조회", rowsPO.length === 1 && rowsPO[0] === "pay.example.co.kr", rowsPO.join(","));
  const leak = await apiIn(PO, "GET", "api/db/discoveries"), leak2 = await apiIn(PO, "GET", "api/db/certificates"), leak3 = await apiIn(PO, "GET", "api/db/users");
  ok("API 조회도 담당 범위로 제한", leak.body.length === 0 && leak2.body.length === 1 && leak3.status === 403, `수집 ${leak.body.length}, 인증서 ${leak2.body.length}, 사용자 HTTP ${leak3.status}`);
  const other = await apiIn(PO, "POST", "api/action", { action: "cert.renewal", ops: [{ op: "update", coll: "certificates", id: portal.id, data: { renewal_status: "완료" } }] });
  const notAllowed = await apiIn(PO, "POST", "api/action", { action: "discovery.decide", ops: [{ op: "update", coll: "discoveries", id: dsc[0].id, data: { status: "승인" } }] });
  ok("타인 인증서 처리·권한 밖 작업 차단", other.status === 403 && notAllowed.status === 403, `${other.body.error} / ${notAllowed.body.error}`);
  await tab(PO, "QA 챗봇"); await PO.locator("#qa-examples button").first().click(); await sleep(400);
  const qa1 = await PO.textContent("#chat");
  await PO.fill("#qa-input", "portal.example.co.kr 인증서 만료일과 담당자 알려줘"); await PO.click("#qa-send"); await sleep(400);
  const qa2 = (await PO.$$eval("#chat .msg.bot", (m) => m.map((x) => x.textContent))).pop();
  ok("QA 챗봇도 담당 범위 정보만 사용", qa1.includes("pay.example.co.kr: 발급기관") && !qa2.includes("portal.example.co.kr: 발급기관"), "타인 인증서 정보 미노출");
  await tab(PO, "인증서"); await shot(PO, "06_owner_scope");

  // ───────── 9. 배포 검증·반려 (만료 임박 인증서는 배포 불가)
  await PO.click("#p-certs tbody tr[data-cert]"); await sleep(200);
  const preDis = await PO.$eval("#d-deploy", (b) => b.disabled), preWhy = await PO.textContent("#cert-detail");
  ok("요청 전 사전 검사: 만료 임박 인증서는 배포 요청 단계에서 차단·사유 표시", preDis && preWhy.includes("남은 기간 30일 초과") && preWhy.includes("갱신본"), preWhy.match(/검토 자동 검증을 통과하지 못해[^—]*/)?.[0]);
  // 화면을 거치지 않은 요청(API 직접 호출)도 검토 단계에서 걸러지는지 확인
  await PO.evaluate(async () => { const c = S.data.certificates.find((x) => x.subject_cn === "pay.example.co.kr" && !x.replaced_by); await act("deploy.request", C.requestDeployOps(c, "dev", actor(), nowIso(), "개발 서버 반영"), "API 직접 요청"); });
  count("배포 요청");
  await RV.reload(); await settle(RV); await tab(RV, "배포");
  const chk = await RV.textContent("#p-deploy");
  ok("검토자 자동 검증: 남은 5일 → '남은 기간 30일 초과' 실패", chk.includes("✗") && chk.includes("남은 기간 30일 초과"), "검토 통과 버튼 비활성");
  ok("검증 실패 시 통과 불가(반려만)", await RV.$eval("[data-rvpass]", (b) => b.disabled));
  await RV.fill("[data-rvnote]", "만료 임박 인증서, 갱신본으로 다시 요청"); await RV.click("[data-rvfail]"); await toastText(RV); await settle(RV); count("배포 검토");
  // portal(200일) 개발 배포
  const KO = await ctx("kim");
  await login(KO, "kim", "Temp1234", "Kim12345");
  await tab(KO, "인증서"); await KO.click("#p-certs tr[data-cert] >> text=portal.example.co.kr"); await sleep(200);
  await KO.click("#d-deploy"); await toastText(KO); count("배포 요청");
  await RV.reload(); await settle(RV); await tab(RV, "배포");
  await RV.click("[data-rvpass]"); await toastText(RV); count("배포 검토");
  await AP.reload(); await settle(AP); await tab(AP, "배포");
  await AP.click("[data-approve-dep]"); const t9 = await toastText(AP); await settle(AP); count("배포 승인");
  const devPortal = path.join(W, "deploy", "dev", "portal.example.co.kr");
  ok("승인 → 개발 환경 폴더에 인증서 파일 반영", fs.existsSync(path.join(devPortal, "web-normal.crt")) && fs.existsSync(path.join(devPortal, "DEPLOYED.json")), t9);

  // ───────── 10. 갱신본 수집·승인
  py("tests/make_fixture.py", W, "--renew", "pay.example.co.kr", "400");
  py("tests/make_fixture.py", W, "--renew", "portal.example.co.kr", "400");
  await A.reload(); await settle(A); await tab(A, "대시보드"); await A.click("#run-collect"); await settle(A); await sleep(300);
  ok("갱신본 2건 수집", (await A.textContent("#agent-out")).includes("승인 대기 2건"), await A.textContent("#agent-out"));
  await AP.reload(); await settle(AP); await tab(AP, "수집 승인");
  ok("갱신본으로 분류", (await AP.$$("#p-disc .pill.k-갱신본")).length === 2);
  while (await AP.$("[data-approve]:not([disabled])")) { await AP.click("[data-approve]:not([disabled])"); await toastText(AP); await settle(AP); count("수집 승인"); }
  const c10 = (await apiIn(A, "GET", "api/db/certificates")).body;
  const payOld = c10.find((c) => c.subject_cn === "pay.example.co.kr" && c.replaced_by), payNew = c10.find((c) => c.subject_cn === "pay.example.co.kr" && !c.replaced_by);
  const payAlerts = (await apiIn(A, "GET", "api/db/notifications")).body.filter((n) => n.cert_id === payOld?.id);
  ok("갱신본 승인 → 기존 갱신 완료·담당자 승계·알림 종료", payOld?.renewal_status === "완료" && payNew?.owner === "박결제" && payAlerts.every((n) => n.closed), `새 만료 ${payNew?.not_after?.slice(0, 10)}`);

  // ───────── 11. 갱신본 4단계 배포 + 직무 분리
  const stagesDone = [];
  for (const st of ["개발", "테스트", "교육", "운영"]) {
    await PO.reload(); await settle(PO); await tab(PO, "인증서"); await PO.click("#p-certs tbody tr[data-cert]"); await sleep(200);
    const label = await PO.textContent("#d-deploy");
    await PO.click("#d-deploy"); await toastText(PO); count("배포 요청");
    await RV.reload(); await settle(RV); await tab(RV, "배포");
    if (st === "개발") await shot(RV, "07_deploy_review");
    await RV.click("[data-rvpass]"); await toastText(RV); count("배포 검토");
    await AP.reload(); await settle(AP); await tab(AP, "배포"); await AP.click("[data-approve-dep]"); const t = await toastText(AP); count("배포 승인");
    stagesDone.push(`${label.replace(" 배포 요청", "")}:${t.includes("반영했습니다") ? "완료" : t}`);
  }
  const prodDir = path.join(W, "deploy", "prod", "pay.example.co.kr");
  ok("갱신본 개발→테스트→교육→운영 4단계 배포", stagesDone.every((s) => s.endsWith("완료")) && fs.existsSync(path.join(prodDir, "pay-renewed.crt")), stagesDone.join(" "));
  const dj = JSON.parse(fs.readFileSync(path.join(prodDir, "DEPLOYED.json"), "utf8"));
  ok("운영 반영 기록(지문·만료일)", dj.fingerprint === payNew.fingerprint, `${dj.file} · ${dj.not_after?.slice(0, 10)}`);
  // 직무 분리: 관리자가 요청한 건을 관리자가 검토·승인할 수 없음
  const portalNew = c10.find((c) => c.subject_cn === "portal.example.co.kr" && !c.replaced_by);
  const reqOps = [{ op: "set", coll: "deployments", id: `dp_${portalNew.id}_dev`, data: { cert_id: portalNew.id, subject_cn: portalNew.subject_cn, stage: "dev", stage_name: "개발", status: "요청", requested_by: "x", requested_at: new Date().toISOString(), note: "" } }];
  await apiIn(A, "POST", "api/action", { action: "deploy.request", ops: reqOps });
  const sod = await apiIn(A, "POST", "api/action", { action: "deploy.review", ops: [{ op: "update", coll: "deployments", id: `dp_${portalNew.id}_dev`, data: { status: "검토 통과", review: { checks: [], pass: true } } }] });
  ok("직무 분리: 요청자는 검토 불가(관리자 포함)", sod.status === 403 && sod.body.error.includes("직무 분리"), sod.body.error);
  const skip = await apiIn(A, "POST", "api/action", { action: "deploy.approve", ops: [{ op: "update", coll: "deployments", id: `dp_${portalNew.id}_dev`, data: { status: "배포 완료" } }, { op: "set", coll: "environments", id: "env_dev_x", data: {} }] });
  ok("상태 전이 검사: 검토 없이 승인 불가", skip.status === 409 || skip.status === 403, `${skip.status} ${skip.body.error}`);
  // 대장 갱신 후 portal 갱신본 개발 배포 → 원복
  await RV.reload(); await settle(RV); await tab(RV, "배포"); await RV.click("[data-rvpass]"); await toastText(RV); count("배포 검토");
  await AP.reload(); await settle(AP); await tab(AP, "배포"); await AP.click("[data-approve-dep]"); await toastText(AP); count("배포 승인");
  ok("갱신본 개발 반영(이전 파일 history 보관)", fs.existsSync(path.join(devPortal, "portal-renewed.crt")) && fs.readdirSync(path.join(devPortal, "history")).length >= 1);

  // ───────── 12. 원복
  await AP.reload(); await settle(AP); await tab(AP, "배포");
  const rbRow = AP.locator("#p-deploy tr", { hasText: "portal.example.co.kr" }).filter({ hasText: "개발" }).filter({ has: AP.locator("[data-rollback]") }).first();
  await rbRow.locator("[data-rbreason]").fill("개발 서버 TLS 핸드셰이크 오류"); await rbRow.locator("[data-rollback]").click(); const t12 = await toastText(AP); await settle(AP); count("배포 원복");
  const envs = (await apiIn(AP, "GET", "api/db/environments")).body;
  const devEnv = envs.find((e) => e.stage === "dev" && e.subject_cn === "portal.example.co.kr");
  ok("원복 → 직전 배포본 복구(파일·기록)", devEnv.cert_id === portal.id && fs.existsSync(path.join(devPortal, "web-normal.crt")) && !fs.existsSync(path.join(devPortal, "portal-renewed.crt")), t12);
  await shot(AP, "08_deploy_board");

  // ───────── 13. 보고서 (SC-003)
  await A.reload(); await settle(A); await tab(A, "보고서");
  await A.click("[data-quick='30d']"); await sleep(150);
  const n30 = (await A.$$("#p-report tbody tr[data-cert]")).length;
  await A.click("[data-quick='']"); await sleep(150);
  const rep = await A.textContent("#p-report");
  ok("보고서: 갱신 상태별·지연·중복 집계", rep.includes("갱신 완료") && rep.includes("갱신 지연") && rep.includes("같은 인증서 여러 위치"), `30일 이내 만료 ${n30}건`);
  await shot(A, "09_report");
  const [dl] = await Promise.all([A.waitForEvent("download"), A.click("#rep-xlsx")]);
  const xf = path.join(W, "report.xlsx"); await dl.saveAs(xf);
  const xl = JSON.parse(py("-c", `import openpyxl,json;wb=openpyxl.load_workbook(${JSON.stringify(xf)});print(json.dumps({'s':wb.sheetnames,'n':wb['갱신 현황'].max_row-1,'h':[c.value for c in wb['갱신 현황'][1]]},ensure_ascii=False))`));
  const uiRows = (await A.$$("#p-report tbody tr[data-cert]")).length;
  ok("보고서 엑셀: 시트 3개, 화면과 같은 건수", xl.s.join() === "요약,갱신 현황,중복 인증서" && xl.n === uiRows, `${xl.s.join("/")} · ${xl.n}건`);
  const [dl2] = await Promise.all([A.waitForEvent("download"), A.click("#rep-csv")]);
  const csv = fs.readFileSync(await dl2.path(), "utf8");
  ok("보고서 CSV(엑셀 수식 주입 방지 포함)", csv.startsWith("﻿인증서명") && csv.split("\r\n").length === uiRows + 1, `${csv.split("\r\n").length - 1}행`);

  // ───────── 14. 조회자
  const VO = await ctx("viewer");
  await login(VO, "view", "Temp1234", "View12345");
  await tab(VO, "인증서"); const vRows = (await VO.$$("#p-certs tbody tr[data-cert]")).length;
  await tab(VO, "대시보드");
  const vDis = await VO.$eval("#run-notify", (b) => b.disabled);
  const vAct = await apiIn(VO, "POST", "api/action", { action: "cert.manage", ops: [{ op: "update", coll: "certificates", id: portal.id, data: { owner: "x" } }] });
  ok("조회자: 전체 조회, 모든 변경 차단", vRows >= 9 && vDis && vAct.status === 403, `인증서 ${vRows}건 · ${vAct.body.error}`);

  // ───────── 14-1. QA 가이드 업로드(서버 모드) — 관리자만, 업로드 즉시 RAG 반영
  await tab(A, "QA 챗봇");
  await A.setInputFiles("#g-in", [{ name: "guide.md", mimeType: "text/markdown", buffer: fs.readFileSync(new URL("./guides/사내_인증서_교체_작업_절차_예시.md", import.meta.url)) },
    { name: "nginx.pdf", mimeType: "application/pdf", buffer: fs.readFileSync(new URL("./guides/Tomcat_인증서_교체_메모_CID예시.pdf", import.meta.url)) }]);
  await A.waitForFunction(() => /nginx\.pdf|U02/.test(document.getElementById("g-msg")?.textContent || ""), null, { timeout: 30000 });
  const gTxt = await A.textContent("#qa-guides");
  const vG = await apiIn(VO, "POST", "api/action", { action: "guide.manage", ops: [{ op: "set", coll: "guides", id: "g_x", data: { title: "x" } }] });
  ok("가이드 업로드(서버, .md·PDF): 관리자 저장·RAG 반영, 조회자 차단", gTxt.includes("U01") && gTxt.includes("U02") && gTxt.includes("업로드 2종") && vG.status === 403, `${(gTxt.match(/검색 문단 \d+개/) || [""])[0]} · 조회자 ${vG.status}`);

  // ───────── 15. 보안
  const anonR = await fetch(BASE + "api/db/certificates"); await anonR.text(); const anon = anonR.status;
  const noCsrf = await apiIn(A, "POST", "api/agent/notify", {}, false);
  const raw = await apiIn(A, "PUT", "api/db/certificates/c_x", { owner: "x" });
  const injected = await apiIn(A, "POST", "api/action", { action: "cert.manage", ops: [{ op: "update", coll: "certificates", id: portal.id, data: { owner: "x", fingerprint: "AA" } }] });
  const badId = await apiIn(A, "POST", "api/action", { action: "import.excel", ops: [{ op: "set", coll: "certificates", id: "c_forged", data: {} }] });
  ok("미로그인 401 · CSRF 없는 요청 403 · 직접 DB 쓰기 경로 없음", anon === 401 && noCsrf.status === 403 && raw.status === 404, `${anon} / ${noCsrf.status} / ${raw.status}`);
  ok("작업별 허용 필드·문서 ID 검사", injected.status === 403 && badId.status === 403, `${injected.body.error} / ${badId.body.error}`);
  const codes = [];
  for (let i = 0; i < 6; i++) { const r = await fetch(BASE + "api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "view", password: "wrong" + i }) }); await r.text(); codes.push(r.status); }
  ok("로그인 5회 실패 → 잠금", codes.slice(0, 5).every((c) => c === 401) && codes[5] === 429, codes.join(","));
  const hr = await fetch(BASE); await hr.text(); const hdr = hr.headers;
  ok("보안 헤더(CSP·X-Frame-Options·nosniff)", hdr.get("content-security-policy")?.includes("frame-ancestors 'none'") && hdr.get("x-frame-options") === "DENY" && hdr.get("x-content-type-options") === "nosniff");
  const hashOnly = py("-c", `import sqlite3;c=sqlite3.connect(${JSON.stringify(path.join(W, "p.db"))});print(sum(1 for (d,) in c.execute("select data from docs where coll='users'") if 'Temp1234' in d or 'Admin1234' in d))`).trim();
  ok("사용자 비밀번호는 해시만 저장", hashOnly === "0", `평문 ${hashOnly}건`);

  // ───────── 16. 감사 로그
  await A.reload(); await settle(A); await tab(A, "이력");
  const aud = await A.textContent("#p-hist");
  ok("감사 로그: 역할별 처리자 기록", ["최승인(승인자)", "이검토(검토자)", "박결제(담당자)", "배포 원복", "관리대장 등록", "로그인 실패"].every((k) => aud.includes(k)));
  await browser.close();
} catch (e) {
  console.error(e); ok("실행 오류 없음", false, e.message);
} finally {
  srv.kill(); sink.kill();
  if (srvErr.trim()) console.log("[server stderr]\n" + srvErr.slice(-2000));
  const pass = R.filter((x) => x.pass).length;
  console.log(JSON.stringify({ pass, total: R.length, steps }));
  fs.writeFileSync(path.join(ROOT, "results", "e2e_browser.json"), JSON.stringify({ at: new Date().toISOString(), pass, total: R.length, steps, results: R }, null, 1));
}

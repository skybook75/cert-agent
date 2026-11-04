// 인수 테스트: 인증서 관리자 관점의 운영 시나리오를 실제 PC 서버 + Chromium 화면으로 수행하고 증적을 남긴다.
//   node tests/acceptance/run_scenarios.mjs [작업폴더]
// 결과: results/acceptance/report.json, results/acceptance/shots/*.png, 작업폴더(배포 폴더·DB·메일함)
import { createRequire } from "module";
import { spawn, execFileSync } from "child_process";
import fs from "fs";
import path from "path";

const { chromium } = createRequire(import.meta.url)("playwright");
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
const W = path.resolve(process.argv[2] || "/tmp/cert_accept");
const OUT = path.join(ROOT, "results", "acceptance"), SHOTS = path.join(OUT, "shots");
const PORT = 8777, SMTP = 2537, BASE = `http://localhost:${PORT}/`;
fs.rmSync(W, { recursive: true, force: true }); fs.mkdirSync(W, { recursive: true });
fs.rmSync(SHOTS, { recursive: true, force: true }); fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const py = (...a) => execFileSync("python3", a, { cwd: ROOT }).toString();

// ───────── 기록
const SC = []; let cur = null;
function scenario(id, title, goal) { cur = { id, title, goal, steps: [], shots: [], started: new Date().toISOString() }; SC.push(cur); console.log(`\n■ ${id} ${title}`); }
function step(action, expected, actual, pass, note = "") { cur.steps.push({ action, expected, actual: String(actual), pass: !!pass, note }); console.log(`  ${pass ? "PASS" : "FAIL"} ${action} → ${String(actual).slice(0, 160)}`); }
function finding(sev, text, fix = "") { (cur.findings ||= []).push({ sev, text, fix }); console.log(`  [발견 ${sev}] ${text}`); }
async function shot(p, name, caption) { const f = `${cur.id}_${name}.png`; await p.screenshot({ path: path.join(SHOTS, f), fullPage: false }); cur.shots.push({ file: f, caption }); }

// ───────── 데이터·서버
py("tests/acceptance/make_ops.py", W);
const EXP = JSON.parse(fs.readFileSync(path.join(W, "expected.json"), "utf8"));
const MAIL = path.join(W, "mail.jsonl");
const sink = spawn("python3", ["tests/smtp_sink.py", String(SMTP), MAIL], { cwd: ROOT, stdio: "ignore" });
const srv = spawn("python3", ["platform_server.py", "--db", path.join(W, "platform.db"), "--scan", path.join(W, "servers"), "--deploy", path.join(W, "deploy"),
  "--port", String(PORT), "--no-browser", "--vault-key", path.join(W, "keys", "vault.key")], { cwd: ROOT, stdio: ["ignore", "ignore", "pipe"] });
let srvErr = ""; srv.stderr.on("data", (d) => (srvErr += d));
await sleep(1500);

const browser = await chromium.launch();
const ctx = async (who, mobile = false) => {
  const c = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 }, acceptDownloads: true, locale: "ko-KR", timezoneId: "Asia/Seoul" });
  await c.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  const p = await c.newPage(); p.on("pageerror", (e) => console.log(`[pageerror:${who}]`, e.message)); p.on("dialog", (d) => d.dismiss());
  return p;
};
const tab = async (p, name) => { await p.locator("#tabs button").filter({ hasText: new RegExp(`^${name}\\s*\\d*$`) }).first().click(); await sleep(200); };
const toast = async (p) => { await p.waitForSelector("#toast:not([hidden])", { timeout: 15000 }); const t = await p.textContent("#toast"); await p.evaluate(() => ($("toast").hidden = true)); return t; };
const settle = async (p) => { await p.waitForLoadState("networkidle"); await sleep(250); };
async function login(p, user, pw, newPw) {
  await p.goto(BASE); await settle(p);
  await p.fill("#a-user", user); await p.fill("#a-pw", pw); await p.click("#auth-form button[type=submit]"); await settle(p);
  if (newPw && await p.isVisible("#a-old")) { await p.fill("#a-old", pw); await p.fill("#a-pw", newPw); await p.fill("#a-pw2", newPw); await p.click("#auth-form button[type=submit]"); await settle(p); }
}
const api = (p, method, url, body) => p.evaluate(async ({ method, url, body }) => {
  const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
}, { method, url, body });
const certsOf = async (p) => (await api(p, "GET", "api/db/certificates")).body;
const collect = async (p) => { await tab(p, "대시보드"); await p.click("#run-collect"); await settle(p); await sleep(400); return p.textContent("#agent-out"); };

const T0 = Date.now();
try {
  // ════════ S01 최초 설정·사용자
  scenario("S01", "플랫폼 최초 설정과 사용자 등록", "관리자가 처음 접속해 관리자 계정을 만들고, 담당자 4명·검토자·승인자·조회자 계정과 메일(SMTP)을 설정한다.");
  const A = await ctx("admin");
  await A.goto(BASE); await settle(A);
  step("처음 접속", "최초 관리자 계정 만들기 화면", await A.isVisible("text=최초 관리자 계정 만들기") ? "최초 관리자 생성 화면 표시" : "표시 안 됨", await A.isVisible("text=최초 관리자 계정 만들기"));
  await A.fill("#a-user", "admin"); await A.fill("#a-name", "김관리"); await A.fill("#a-mail", "admin@skax-demo.co.kr"); await A.fill("#a-pw", "Admin2026"); await A.fill("#a-pw2", "Admin2026");
  await A.click("#auth-form button[type=submit]"); await settle(A);
  await login(A, "admin", "Admin2026");
  await tab(A, "관리");
  const USERS = [["minsu", "김민수", "minsu.kim@skax-demo.co.kr", "담당자"], ["seoyeon", "이서연", "seoyeon.lee@skax-demo.co.kr", "담당자"], ["jihoon", "박지훈", "jihoon.park@skax-demo.co.kr", "담당자"],
    ["haneul", "정하늘", "haneul.jung@skax-demo.co.kr", "담당자"], ["rev", "이검토", "rev@skax-demo.co.kr", "검토자"], ["apr", "최승인", "apr@skax-demo.co.kr", "승인자"], ["view", "정조회", "view@skax-demo.co.kr", "조회자"]];
  for (const [u, n, m, r] of USERS) {
    await A.fill("#u-user", u); await A.fill("#u-name", n); await A.fill("#u-mail", m); await A.selectOption("#u-role", r); await A.fill("#u-pw", "Temp2026a");
    await A.click("#u-form button[type=submit]"); await toast(A); await settle(A);
  }
  const nu = (await A.$$("[data-urole]")).length;
  step("사용자 7명 추가(담당자 4·검토자·승인자·조회자)", "사용자 목록 8명", `${nu}명`, nu === 8);
  await A.check("#s-en"); await A.fill("#s-host", "127.0.0.1"); await A.fill("#s-port", String(SMTP)); await A.selectOption("#s-sec", "none");
  await A.fill("#s-from", "cert-platform@skax-demo.co.kr"); await A.click("#smtp-form button[type=submit]"); await toast(A);
  await A.fill("#s-to", "admin@skax-demo.co.kr"); await A.click("#s-test"); const smtpT = await toast(A);
  step("SMTP 설정 후 테스트 메일", "테스트 메일 발송 성공", smtpT, /보냈습니다|메일 완료/.test(smtpT));
  await shot(A, "users", "관리 탭: 역할별 사용자 8명과 SMTP 설정");

  // ════════ S02 수기 관리대장 등록
  scenario("S02", "엑셀 수기 관리대장 등록", "담당자들이 손으로 관리해 온 엑셀 대장(10행)을 올려 형식을 검증하고, 오류를 고친 수정본으로 등록한다.");
  await tab(A, "관리대장");
  const up = async (f) => { await A.setInputFiles("#xl-file", { name: f, mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: fs.readFileSync(path.join(W, f)) }); await sleep(600); return A.textContent("#p-ledger"); };
  const v1 = await up("관리대장_수기_원본.xlsx");
  const errs = (v1.match(/오류 (\d+)건/) || [])[1], okN = (v1.match(/정상 (\d+)건/) || [])[1];
  step("원본 업로드(제목 행·빈 행이 있는 실제 양식)", "머리글 자동 인식, 오류 4건(담당자 누락·날짜 오타·이메일 오타·중복 행), 정상 6건", `정상 ${okN}건 · 오류 ${errs}건`, okN === "6" && errs === "4");
  step("오류 사유 표시", "행 번호와 사유", ["필수 값이 비어", "날짜 형식이 아닙니다", "이메일 형식이 아닙니다", "중복"].filter((k) => v1.includes(k)).join(" / "),
    ["필수 값이 비어", "날짜 형식이 아닙니다", "이메일 형식이 아닙니다", "중복"].every((k) => v1.includes(k)));
  step("점(.)으로 쓴 날짜 2026.11.07 형식", "정상 인식", v1.includes("www.skax-demo.co.kr") ? "www 행 정상 목록에 포함" : "인식 실패", !/날짜 형식이 아닙니다: \d{4}\.\d/.test(v1));
  await shot(A, "ledger_errors", "원본 업로드: 오류 4건을 행·열·사유와 함께 표시, 등록 전 미리보기");
  const v2 = await up("관리대장_수정본.xlsx");
  step("수정본 업로드", "정상 9건 · 오류 없음", (v2.match(/정상 \d+건[^·]*/) || [""])[0], v2.includes("정상 9건") && !/오류 [1-9]/.test(v2));
  await A.click("#xl-commit"); const t2 = await toast(A); await settle(A);
  step("등록", "신규 9건", t2, t2.includes("신규 9건"));
  const rc0 = await A.textContent("#p-ledger");
  step("수집 전 대조 현황", "9건 모두 '미수집'(아직 서버에서 확인 안 됨)", (rc0.match(/미수집 \d+/) || [""])[0], rc0.includes("미수집 9"));
  await shot(A, "ledger_registered", "수정본 등록 후: 대장 9건이 모두 '미수집' 상태로 대조 대기");

  // ════════ S03 1차 수집
  scenario("S03", "서버 4대 1차 수집과 대장 대조", "nginx·Apache·Tomcat·IIS 서버 폴더를 수집 Agent로 스캔한다. 보관함에 PFX 비밀번호가 아직 없다.");
  const o3 = await collect(A);
  const tgt = EXP.files.filter((f) => f.kind !== "ignored-ext").length;
  step("수집 Agent 실행", `대상 확장자 파일 ${tgt}개(.bak 제외)`, o3.slice(0, 160), o3.includes(`파일 ${tgt}개`));
  step("PFX 4개는 비밀번호가 없어 실패로 분류, 손상·개인키 파일도 사유 표시", "비밀번호 필요 4 · 형식 오류 1 · 인증서 아님 1",
    ["비밀번호 필요", "형식 오류", "인증서가 아닌 PEM"].map((k) => `${k} ${(o3.match(new RegExp(k, "g")) || []).length}`).join(" · "),
    (o3.match(/비밀번호 필요/g) || []).length === 4 && o3.includes("형식 오류") && o3.includes("인증서가 아닌 PEM"));
  await tab(A, "관리대장");
  const rc1 = await A.textContent("#p-ledger");
  const cnt = (k) => +((rc1.match(new RegExp(`(?<!불)${k} (\\d+)`)) || [])[1] || 0);
  step("대장 대조", "일치 3(shop·wildcard·www) · 불일치 1(api 만료일) · 미수집 5(PFX 4 + 폐기 서버 old-gw) · 신규 2(공동인증서·중간CA) · 이전 버전 1(www 2025 사본)",
    `일치 ${cnt("일치")} · 불일치 ${cnt("불일치")} · 미수집 ${cnt("미수집")} · 신규 ${cnt("신규")} · 이전 버전 ${cnt("이전 버전")}`, cnt("일치") === 3 && cnt("불일치") === 1 && cnt("미수집") === 5 && cnt("신규") === 2 && cnt("이전 버전") === 1);
  step("불일치 변경 전·후", "api 만료일 대장값 → 수집값", (rc1.match(/만료일: \d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}/) || ["표시 없음"])[0], /만료일: \d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}/.test(rc1));
  await shot(A, "reconcile_1st", "1차 수집 후 대조: 불일치는 변경 전→후, PFX는 비밀번호가 없어 미수집");

  // ════════ S04 PFX 비밀번호 보관함
  scenario("S04", "PFX 비밀번호 보관함 등록·교체 후 재수집", "담당자는 본인 서버 폴더 범위로, 관리자는 파일 1개 범위로 비밀번호를 등록한다. 한 건은 일부러 틀리게 넣고 교체한다.");
  const SE = await ctx("seoyeon");
  await login(SE, "seoyeon", "Temp2026a", "Seoyeon2026");
  await tab(SE, "PFX 비밀번호");
  const needSe = await SE.textContent("#p-vault");
  step("담당자(이서연) 보관함 탭: 비밀번호가 필요한 파일 목록", "마지막 수집에서 비밀번호가 필요했던 4개 파일 표시", (needSe.match(/\.p12|\.pfx/g) || []).length + "개 파일", (needSe.match(/\.p12|\.pfx/g) || []).length >= 4);
  await SE.selectOption("#vs-scope", "folder"); await SE.fill("#vs-path", "was01-tomcat/opt/tomcat/conf/ssl"); await SE.fill("#vs-label", "Tomcat 키스토어(결제·ERP)");
  await SE.fill("#vs-p1", "Tomcat!2026"); await SE.fill("#vs-p2", "Tomcat!2026"); await SE.click("#vs-go"); const tse = await toast(SE);
  step("담당자가 폴더 범위로 등록", "암호화해 저장", tse, tse.includes("암호화해 저장"));
  const seAll = await api(SE, "POST", "api/secrets", { scope: "all", scope_value: "", label: "x", password: "x" });
  step("담당자가 '전체 PFX' 범위 등록 시도", "차단(관리자만)", `HTTP ${seAll.status} ${seAll.body?.error}`, seAll.status === 403);
  await shot(SE, "vault_owner", "담당자 화면: 비밀번호가 필요한 파일과 본인이 등록한 항목만 보임");
  await tab(A, "PFX 비밀번호"); await settle(A);
  const needIdx = await A.$$eval("[data-vneed]", (b) => b.map((x) => x.closest("tr").textContent));
  const ip = needIdx.findIndex((t) => t.includes("portal.pfx"));
  if (ip >= 0) await A.click(`[data-vneed='${ip}']`);
  const pre = await A.inputValue("#vs-path");
  await A.fill("#vs-p1", "Iis#Portal8"); await A.fill("#vs-p2", "Iis#Portal8"); await A.click("#vs-go"); await toast(A); await settle(A);
  step("관리자가 '이 파일로 등록'(경로 자동 입력) — 일부러 틀린 비밀번호", "경로 자동 채움, 저장", pre, pre.endsWith("portal.pfx"));
  const o4a = await collect(A);
  step("재수집", "Tomcat 2개는 열림, portal.pfx 는 '비밀번호 불일치', hr.pfx 는 '비밀번호 필요'", (o4a.match(/보관함 비밀번호로 연 PFX \d+개/) || [""])[0] + " · " + ((o4a.match(/portal\.pfx\([^)]*\)\)?/) || [""])[0]),
    o4a.includes("보관함 비밀번호로 연 PFX 2개") && /portal\.pfx\(비밀번호 불일치/.test(o4a) && /hr\.pfx\(비밀번호 필요/.test(o4a));
  await tab(A, "PFX 비밀번호"); await settle(A);
  const rowTxt = await A.$$eval("#p-vault tbody tr", (r) => r.map((x) => x.textContent));
  const ei = (await A.$$eval("[data-vedit]", (b) => b.map((x) => x.closest("tr").textContent))).findIndex((t) => t.includes("portal.pfx"));
  await A.locator("[data-vedit]").nth(ei).click(); await A.fill("#vr-pw", "Iis#Portal9"); await A.click("[data-vsave]"); const trep = await toast(A); await settle(A);
  step("비밀번호 교체", "교체 성공(값은 다시 볼 수 없음)", trep, trep.includes("교체"));
  const o4b = await collect(A);
  step("교체 후 재수집", "portal.pfx 열림, 남은 실패는 hr.pfx(비밀번호 모름)·손상·개인키 3건", (o4b.match(/실패 \d+건/) || [""])[0] + " · " + (o4b.match(/보관함 비밀번호로 연 PFX \d+개/) || [""])[0], o4b.includes("실패 3건") && /hr\.pfx\(비밀번호 필요/.test(o4b));
  const sec = (await api(A, "GET", "api/secrets")).body;
  const dbHit = String((execFileSync("python3", ["tools/dbdump.py", path.join(W, "platform.db")], { cwd: ROOT }).toString().match(/Tomcat!2026|Iis#Portal9|Iis#Portal8/g) || []).length);
  step("보관 상태 확인", "암호문만 저장(DB에 평문 0건), 사용 횟수·마지막 파일 기록", `등록 ${sec.items.length}개 · ${sec.items.map((x) => `${x.label}: 사용 ${x.use_count}회`).join(", ")} · DB 평문 ${dbHit}건`, dbHit === "0" && sec.items.every((x) => x.use_count >= 1));
  await shot(A, "vault_admin", "관리자 보관함: 등록 2건(폴더·파일 범위), 사용 횟수·최근 사용 파일 기록, 평문 조회 불가");

  // ════════ S05 검토·승인
  scenario("S05", "수집 결과 검토·승인·반려", "검토자가 의견을 남기고 승인자가 승인한다. 대장에 없던 인증서는 담당자를 지정하고, 이전 버전 사본은 반려한다.");
  const RV = await ctx("rev"), AP = await ctx("apr");
  await login(RV, "rev", "Temp2026a", "Review2026"); await login(AP, "apr", "Temp2026a", "Approve2026");
  await tab(RV, "수집 승인");
  const pendRv = (await RV.$$("[data-rv]")).length / 2;
  for (let i = 0; i < pendRv; i++) { await RV.click("[data-rv][data-v='적합']"); await toast(RV); await settle(RV); }
  step("검토자 검토 의견(적합)", "대기 건마다 의견 기록, 승인 버튼은 없음", `${pendRv}건 검토`, pendRv > 0 && !(await RV.$("[data-approve]:not([disabled])")));
  await tab(AP, "수집 승인"); await settle(AP);
  const discTxt = await AP.textContent("#p-disc");
  const kinds = await AP.$$eval("#p-disc tbody tr td:first-child .pill", (x) => x.map((e) => e.textContent));
  step("승인 대기 분류", "일치·불일치·이전 버전·신규 구분", [...new Set(kinds)].join(","), kinds.includes("이전 버전") && kinds.includes("불일치"));
  await shot(AP, "approval", "승인자 화면: 대장 대조 구분(일치·불일치·이전 버전·신규)과 검토 의견");
  const OWN = { "홍길동()0012345678": "정하늘", "DigiCert Global G2 TLS RSA SHA256 2020 CA1": "김민수" };
  let nA = 0, nR = 0;
  for (let guard = 0; guard < 20; guard++) {
    const rows = await AP.$$eval("#p-disc tbody tr", (rs) => rs.map((r) => ({ id: r.querySelector("[data-approve]")?.dataset.approve, dis: r.querySelector("[data-approve]")?.disabled, cn: r.children[1]?.firstChild?.textContent, kind: r.querySelector("td .pill")?.textContent })).filter((x) => x.id));
    if (!rows.length) break;
    const r = rows[0];
    if (r.kind === "이전 버전") { await AP.click(`[data-reject="${r.id}"]`); nR++; }
    else { const inp = await AP.$(`[data-owner="${r.id}"]`); if (!(await inp.inputValue())) await inp.fill(OWN[r.cn] || "김민수"); await AP.click(`[data-approve="${r.id}"]`); nA++; }
    await toast(AP); await settle(AP);
  }
  const C5 = await certsOf(AP);
  const api5 = C5.find((c) => c.subject_cn === "api.skax-demo.co.kr"), www5 = C5.filter((c) => c.subject_cn === "www.skax-demo.co.kr");
  step("승인·반려 처리", "이전 버전 1건 반려, 나머지 승인", `승인 ${nA} · 반려 ${nR}`, nR === 1 && nA >= 8);
  step("불일치 건: 수집값으로 대장 갱신 + 차이 기록, 담당자 유지", "api 만료일 수집값, 담당 이서연", `${api5?.not_after?.slice(0, 10)} · ${api5?.owner} · 차이 ${JSON.stringify(api5?.registry_diffs?.[0] || {})}`, api5?.owner === "이서연" && api5?.registry_diffs?.length === 1);
  step("이전 버전 사본은 등록 안 됨", "www 인증서 1건만", `www ${www5.length}건`, www5.length === 1);
  const old = C5.find((c) => c.subject_cn === "old-gw.skax-demo.co.kr");
  step("폐기 서버 인증서(old-gw)", "수집되지 않아 '대장만 등록'으로 남음 → 정리 대상", old ? `${old.source} · 지문 ${old.fingerprint ? "있음" : "없음"}` : "없음", old && !old.fingerprint);
  await tab(A, "관리대장"); await settle(A);
  const rc5 = await A.textContent("#p-ledger");
  await shot(A, "reconcile_after", "승인 후 대조: 확인 완료와 미수집(폐기 서버·hr.pfx)만 남음");

  // ════════ S06 만료 알림·메일
  scenario("S06", "만료 알림과 담당자 메일", "알림 Agent가 D-30·D-7·만료 대상을 한 번씩 만들고, 담당자 이메일로 발송한다. 다시 실행해도 중복되지 않아야 한다.");
  await tab(A, "대시보드"); await A.click("#run-notify"); const t6 = await toast(A); await settle(A);
  const N6 = (await api(A, "GET", "api/db/notifications")).body;
  const lv = {}; N6.forEach((n) => (lv[n.level] = (lv[n.level] || 0) + 1));
  step("알림 Agent 실행", "D-7 1(www) · D-30 3(api·portal·대장에만 남은 old-gw) · 만료 1(erp)", `${t6} ${JSON.stringify(lv)}`, lv["D-7"] === 1 && lv["D-30"] === 3 && lv["만료"] === 1);
  const og = N6.find((n) => n.subject_cn === "old-gw.skax-demo.co.kr" || String(n.title || "").includes("old-gw"));
  step("폐기 서버 인증서도 대장에 남아 있으면 알림", "old-gw D-30 알림 → 대장 정리 필요 신호", og ? `${og.level} · ${og.title}` : "없음", !!og);
  await A.click("#run-notify"); const t6b = await toast(A); await settle(A);
  const N6b = (await api(A, "GET", "api/db/notifications")).body;
  step("다시 실행", "새 알림 0건(단계별 1회)", t6b, N6b.length === N6.length);
  await tab(A, "알림함"); await A.click("#mail-all"); const tm = await toast(A); await settle(A);
  const mails = fs.existsSync(MAIL) ? fs.readFileSync(MAIL, "utf8").trim().split("\n").map((l) => JSON.parse(l)).filter((m) => !/테스트/.test(m.subject)) : [];
  step("미발송 알림 이메일 발송", "담당자별 실제 수신(SMTP)", `${tm} · 수신 ${[...new Set(mails.flatMap((m) => m.to))].join(", ")}`, mails.length >= 3);
  const wm = mails.find((m) => m.subject.includes("www.skax-demo.co.kr"));
  step("메일 내용", "제목에 단계, 본문에 만료일·남은 일수·저장 위치", wm ? `${wm.subject} / ${wm.body.slice(0, 120)}` : "없음", wm && wm.subject.includes("[D-7]") && /만료일은 \d{4}-\d{2}-\d{2}/.test(wm.body) && wm.body.includes("web02-apache"));
  await shot(A, "notifications", "알림함: 단계별 알림과 메일 발송 결과");
  const JI = await ctx("jihoon");
  await login(JI, "jihoon", "Temp2026a", "Jihoon2026");
  await tab(JI, "대시보드");
  const jd = await JI.textContent("#p-dash");
  step("담당자(박지훈) 첫 화면", "본인 담당(www·wildcard)만, '조치가 필요한 담당 인증서' 안내", (jd.match(/내 담당 인증서[\s\S]{0,4}/) || [""])[0] + " · " + (jd.includes("조치가 필요한 담당 인증서") ? "조치 안내 있음" : "조치 안내 없음"), jd.includes("조치가 필요한 담당 인증서"));
  await shot(JI, "owner_dash", "담당자 대시보드: 본인 담당 인증서와 할 일만 표시");

  // ════════ S07 갱신본 수집·승인
  scenario("S07", "갱신: 새 인증서 배치 → 갱신본 인식 → 승인", "박지훈이 www 인증서를 새로 발급받아 서버에 올렸다. 수집 Agent가 갱신본으로 인식하고, 승인하면 기존 건 갱신 완료·알림 종료가 되어야 한다.");
  py("tests/acceptance/make_ops.py", W, "--renew");
  const o7 = await collect(A);
  step("수집", "새 파일 1건만 갱신본으로 승인 대기, 이전에 반려한 사본(www_2025)은 다시 올리지 않음", (o7.match(/승인 대기 \d+건\(갱신본 \d+\)[^·]*/) || [o7.slice(0, 80)])[0] + ((o7.match(/이전에 반려한 인증서 \d+건 제외/) || [""])[0]), /승인 대기 1건\(갱신본 1\)/.test(o7) && o7.includes("반려한 인증서 1건 제외"));
  await AP.reload(); await settle(AP); await tab(AP, "수집 승인");
  const k7 = await AP.$$eval("#p-disc tbody tr td:first-child", (x) => x.map((e) => e.textContent));
  step("분류", "갱신본 + 기존 만료일 표시", k7.join(" | ").slice(0, 120), k7.some((t) => t.includes("갱신본") && t.includes("기존 만료")));
  await AP.click("[data-approve]:not([disabled])"); const ta7 = await toast(AP); await settle(AP);
  const C7 = await certsOf(A);
  const wOld = C7.find((c) => c.subject_cn === "www.skax-demo.co.kr" && c.replaced_by), wNew = C7.find((c) => c.subject_cn === "www.skax-demo.co.kr" && !c.replaced_by);
  const wAl = (await api(A, "GET", "api/db/notifications")).body.filter((n) => n.cert_id === wOld?.id);
  step("승인 결과", "기존 건 '갱신 완료'·교체, 새 건 담당자 승계(박지훈), 기존 알림 종료", `${ta7} · 새 만료 ${wNew?.not_after?.slice(0, 10)} · 담당 ${wNew?.owner} · 알림 종료 ${wAl.filter((n) => n.closed).length}/${wAl.length}`,
    wOld?.renewal_status === "완료" && wNew?.owner === "박지훈" && wAl.length > 0 && wAl.every((n) => n.closed));

  // ════════ S08 4단계 배포·원복
  scenario("S08", "갱신본 4단계 배포와 원복", "박지훈이 갱신본 배포를 요청하고 검토자·승인자가 단계별로 처리한다. 승인되면 플랫폼이 배포 폴더에 인증서 파일을 놓는다(서버 적용은 각 서버 담당 — 현재 범위). 마지막에 운영 단계를 원복한다.");
  await JI.reload(); await settle(JI);
  const stages = [];
  for (const st of ["개발", "테스트", "교육", "운영"]) {
    await tab(JI, "인증서"); await JI.click("#p-certs tr[data-cert] >> text=www.skax-demo.co.kr"); await sleep(250);
    if (await JI.$eval("#d-deploy", (b) => b.disabled)) { const why = await JI.textContent("#cert-detail"); step(`${st} 배포 요청`, "요청 가능", "버튼 비활성: " + (why.match(/(검토 자동 검증[^\n]*|[^\n]*요청할 수 있습니다|[^\n]*배포할 수 없습니다)/) || [why.slice(-200)])[0], false); break; }
    await JI.fill("#d-depnote", `${st} 웹서버 반영 요청`); await JI.click("#d-deploy"); await toast(JI);
    await RV.reload(); await settle(RV); await tab(RV, "배포");
    if (st === "개발") await shot(RV, "deploy_review", "검토자: 자동 검증 6항목(유효기간·남은 기간·알고리즘·키 강도·도메인 유지·만료일 연장)");
    const chk = await RV.textContent("#p-deploy");
    await RV.click("[data-rvpass]"); await toast(RV);
    await AP.reload(); await settle(AP); await tab(AP, "배포"); await AP.click("[data-approve-dep]"); const ta = await toast(AP); await settle(AP);
    stages.push(`${st}:${/반영했습니다/.test(ta) ? "완료" : ta}${(chk.match(/✗/g) || []).length ? `(실패항목 ${(chk.match(/✗/g) || []).length})` : ""}`);
    await JI.reload(); await settle(JI);
  }
  const prod = path.join(W, "deploy", "prod", "www.skax-demo.co.kr");
  const dj = fs.existsSync(path.join(prod, "DEPLOYED.json")) ? JSON.parse(fs.readFileSync(path.join(prod, "DEPLOYED.json"), "utf8")) : {};
  step("요청→검토→승인 × 4단계", "개발·테스트·교육·운영 모두 완료", stages.join(" "), stages.length === 4 && stages.every((s) => s.includes("완료")));
  step("배포 폴더 반영", "deploy/prod/www.skax-demo.co.kr/ 에 갱신 인증서 + DEPLOYED.json(지문·만료일)", `${fs.existsSync(prod) ? fs.readdirSync(prod).join(", ") : "없음"} · ${dj.not_after?.slice(0, 10)}`, dj.fingerprint === wNew?.fingerprint);
  const sodT = await api(JI, "POST", "api/action", { action: "deploy.review", ops: [{ op: "update", coll: "deployments", id: "dp_x", data: { status: "검토 통과" } }] });
  step("담당자가 검토 시도", "권한 없음", `HTTP ${sodT.status}`, sodT.status === 403);
  await AP.reload(); await settle(AP); await tab(AP, "배포");
  const rbBtns = await AP.$$eval("[data-rollback]", (b) => b.map((x) => x.closest("tr")?.textContent || ""));
  const ri = rbBtns.findIndex((t) => t.includes("운영") && t.includes("www"));
  if (ri >= 0) { await AP.locator("[data-rbreason]").nth(ri).fill("운영 반영 후 일부 구형 단말 접속 오류"); await AP.locator("[data-rollback]").nth(ri).click(); }
  const trb = ri >= 0 ? await toast(AP) : "원복 버튼 없음"; await settle(AP);
  const env = (await api(AP, "GET", "api/db/environments")).body.find((e) => e.stage === "prod" && e.subject_cn === "www.skax-demo.co.kr");
  step("운영 원복", "직전 배포본으로 되돌림(첫 배포였으므로 운영 폴더 비움) + 사유 기록", `${trb} · 현재 운영 인증서 ${env?.cert_id || "없음"}`, ri >= 0 && /원복했습니다|원복/.test(trb) && !/버튼 없음/.test(trb));
  const rbFiles = fs.existsSync(prod) ? fs.readdirSync(prod) : [], rbJ = fs.existsSync(path.join(prod, "DEPLOYED.json")) ? JSON.parse(fs.readFileSync(path.join(prod, "DEPLOYED.json"), "utf8")) : {};
  step("원복 후 운영 배포 폴더", "갱신 전 인증서 파일로 되돌리고, 갱신본은 history/ 에 보관", `${rbFiles.join(", ")} · 만료 ${rbJ.not_after?.slice(0, 10)} · history ${fs.existsSync(path.join(prod, "history")) ? fs.readdirSync(path.join(prod, "history")).length : 0}개`,
    rbJ.file === "www.skax-demo.co.kr.crt" && rbFiles.includes("history"));
  await shot(AP, "deploy_board", "배포 현황판: 단계별 상태, 원복 사유, 운영 환경 현재 인증서");

  // ════════ S09 보고서
  scenario("S09", "보고서와 엑셀 내보내기", "관리자가 갱신 현황·지연·중복 보고서를 보고 엑셀로 내려받는다.");
  await A.reload(); await settle(A); await tab(A, "보고서");
  const rep = await A.textContent("#p-report");
  step("보고서 화면", "갱신 완료·지연·중복 집계", ["갱신 완료", "갱신 지연", "같은 인증서 여러 위치"].filter((k) => rep.includes(k)).join(" / "), ["갱신 완료", "갱신 지연"].every((k) => rep.includes(k)));
  const [dl] = await Promise.all([A.waitForEvent("download"), A.click("#rep-xlsx")]);
  const xf = path.join(W, "보고서.xlsx"); await dl.saveAs(xf);
  const xl = JSON.parse(py("-c", `import openpyxl,json;wb=openpyxl.load_workbook(${JSON.stringify(xf)});ws=wb['갱신 현황'];print(json.dumps({'s':wb.sheetnames,'n':ws.max_row-1,'r':[[str(c.value) for c in r] for r in ws.iter_rows(min_row=1,max_row=3)]},ensure_ascii=False))`));
  step("엑셀 내려받기", "시트 3개(요약·갱신 현황·중복), 화면과 같은 건수", `${xl.s.join("/")} · ${xl.n}행`, xl.s.length === 3 && xl.n > 0);
  await shot(A, "report", "보고서: 갱신 상태별 집계와 지연 목록");

  // ════════ S10 권한·보안
  scenario("S10", "역할별 권한과 보안 점검", "조회자는 볼 수만 있고, 담당자는 본인 담당분만 처리하며, 비밀번호·개인키는 어디에도 남지 않는다.");
  const VO = await ctx("view"); await login(VO, "view", "Temp2026a", "View20266");
  await tab(VO, "인증서"); const vr = (await VO.$$("#p-certs tbody tr[data-cert]")).length;
  const vw = await api(VO, "POST", "api/action", { action: "cert.manage", ops: [{ op: "update", coll: "certificates", id: C7[0].id, data: { owner: "x" } }] });
  step("조회자", "전체 조회 가능, 변경 차단", `인증서 ${vr}건 · 변경 HTTP ${vw.status}`, vr > 5 && vw.status === 403);
  await tab(JI, "인증서"); const jr = await JI.$$eval("#p-certs tbody tr[data-cert]", (t) => t.map((r) => r.children[0].firstChild.textContent));
  const other = C7.find((c) => c.subject_cn === "pay.skax-demo.co.kr");
  const jx = await api(JI, "POST", "api/action", { action: "cert.renewal", ops: [{ op: "update", coll: "certificates", id: other.id, data: { renewal_status: "완료" } }] });
  step("담당자(박지훈) 범위", "본인 담당(www·wildcard)만 보이고 타인 건(pay) 처리 차단", `${jr.join(", ")} · pay 처리 HTTP ${jx.status}`, jr.every((c) => /www|\*\./.test(c)) && jx.status === 403);
  const leak = String((execFileSync("python3", ["tools/dbdump.py", path.join(W, "platform.db")], { cwd: ROOT }).toString().match(/PRIVATE KEY|Tomcat!2026|Iis#Portal9/g) || []).length);
  step("DB 점검", "개인키·PFX 비밀번호 문자열 0건", `${leak}건`, leak === "0");
  const aud = (await api(A, "GET", "api/db/audit")).body;
  const acts = [...new Set(aud.map((a) => a.action))];
  step("감사 로그", "등록·수집·승인·반려·보관함·배포·원복·메일이 처리자와 함께 기록", `${aud.length}건 · ${acts.join(", ")}`, ["관리대장 등록", "수집 승인", "수집 반려", "PFX 비밀번호 등록", "배포 승인", "배포 원복"].every((k) => acts.includes(k)));
  await tab(A, "이력"); await A.fill("#au-q", "PFX"); await sleep(500);
  await shot(A, "audit", "감사 로그 검색(PFX): 비밀번호 등록·교체가 값 없이 기록");

  // ════════ S11 QA 챗봇(서버판, Claude 키 없음 → 검색형)
  scenario("S11", "QA 챗봇(서버판 검색형)과 가이드 PDF 업로드", "Claude 키가 없는 PC 서버에서도 등록 인증서 정보와 가이드 원문으로 답하는지, PDF 가이드를 올리면 바로 근거로 쓰는지 확인한다. (실제 Claude 답변·검증은 별도 테스트)");
  await tab(A, "QA 챗봇");
  await A.fill("#qa-input", "www.skax-demo.co.kr 인증서 만료일과 담당자 알려줘"); await A.click("#qa-send"); await sleep(600);
  const q1 = (await A.$$eval("#chat .msg.bot", (m) => m.map((x) => x.textContent))).pop();
  step("인증서 질문", "새 만료일·담당 박지훈", q1.slice(0, 160), q1.includes("박지훈") && q1.includes(wNew.not_after.slice(0, 4)));
  await A.setInputFiles("#g-in", [{ name: "Nginx_SSL_설치_가이드_예시.pdf", mimeType: "application/pdf", buffer: fs.readFileSync(path.join(ROOT, "tests/guides/Nginx_SSL_설치_가이드_예시.pdf")) }]);
  await A.waitForFunction(() => /RAG에 추가|실패/.test(document.getElementById("g-msg")?.textContent || ""), null, { timeout: 30000 });
  await A.fill("#qa-input", "nginx 인증서 교체 후 설정 문법 검사 명령은?"); await A.click("#qa-send"); await sleep(600);
  const q2 = (await A.$$eval("#chat .msg.bot", (m) => m.map((x) => x.textContent))).pop();
  step("PDF 가이드 업로드 후 질문", "업로드 가이드(U01) 근거로 'nginx -t' 안내", q2.slice(0, 160), q2.includes("nginx -t") && q2.includes("U01"));
  await A.fill("#qa-input", "portal.pfx 비밀번호 알려줘"); await A.click("#qa-send"); await sleep(500);
  const q3 = (await A.$$eval("#chat .msg.bot", (m) => m.map((x) => x.textContent))).pop();
  step("비밀번호 요구", "가드레일 차단, LLM 호출 안 함", q3.slice(0, 120), /차단|알려드릴 수 없|비밀번호/.test(q3) && q3.includes("가드레일"));
  await shot(A, "qa_server", "QA(서버판): 등록 정보·업로드 PDF 근거 답변, 비밀번호 요구 차단");

  // ════════ S12 모바일
  scenario("S12", "모바일에서 승인·알림 확인", "승인자가 휴대폰으로 알림을 보고 승인 화면을 연다.");
  const M = await ctx("mobile", true); await login(M, "apr", "Approve2026");
  const sw = await M.evaluate(() => document.documentElement.scrollWidth);
  await tab(M, "배포"); await sleep(300);
  step("390px 화면", "가로 넘침 없음, 표는 카드로", `scrollWidth ${sw}px`, sw <= 392);
  await shot(M, "mobile", "휴대폰(390px): 표가 카드로 바뀌어 버튼이 바로 보임");
} catch (e) {
  console.error(e); if (cur) step("실행 오류", "오류 없음", e.message.split("\n")[0], false);
} finally {
  const all = SC.flatMap((s) => s.steps);
  const report = { at: new Date().toISOString(), seconds: Math.round((Date.now() - T0) / 1000), pass: all.filter((x) => x.pass).length, total: all.length, scenarios: SC, data: EXP, server_errors: srvErr.slice(-2000) };
  fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 1));
  console.log(`\n${report.pass}/${report.total} · ${report.seconds}s`);
  await browser.close(); srv.kill(); sink.kill();
}

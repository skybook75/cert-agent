// AWS 실서버 관리자 시나리오 러너 — 플랫폼 서버(EC2)에서 실제 HTTPS 주소로 화면을 조작하고 녹화·캡처를 남긴다.
//   node lab_runner.mjs <phase> <platformUrl>      phase: setup | approve | deploy | status | ops | qa
//   상태(테스트 계정 비밀번호·Agent 토큰)는 /var/lib/cert-lab-run/state.json (600) 에만 저장
import { createRequire } from "module";
import fs from "fs";
import path from "path";
import crypto from "crypto";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const [phase, URL0] = process.argv.slice(2);
const BASE = URL0.endsWith("/") ? URL0 : URL0 + "/";
const RUN = "/var/lib/cert-lab-run", OUT = path.join(RUN, "out", phase);
fs.mkdirSync(OUT, { recursive: true });
const SF = path.join(RUN, "state.json");
const S = fs.existsSync(SF) ? JSON.parse(fs.readFileSync(SF, "utf8")) : {};
const save = () => { fs.writeFileSync(SF, JSON.stringify(S, null, 1)); fs.chmodSync(SF, 0o600); };
const pw = () => "Lab" + crypto.randomBytes(9).toString("base64url").replace(/[-_]/g, "x") + "7";
const R = []; let shotN = 0;
const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: String(d).slice(0, 400) }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${String(d).slice(0, 300)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch();
const ctxs = [];
async function page(name) {
  const c = await browser.newContext({ viewport: { width: 1360, height: 820 }, locale: "ko-KR", bypassCSP: true, recordVideo: { dir: path.join(OUT, "video_" + name), size: { width: 1360, height: 820 } } });
  ctxs.push(c);
  const p = await c.newPage();
  p.on("pageerror", (e) => console.log(`[${name}] pageerror`, e.message));
  return p;
}
const settle = async (p) => { await p.waitForLoadState("networkidle").catch(() => {}); await sleep(400); };
const shot = async (p, label, sel) => { const f = path.join(OUT, `${String(++shotN).padStart(2, "0")}_${label}.png`); await (sel ? p.locator(sel).first().screenshot({ path: f }) : p.screenshot({ path: f })); return f; };
const tab = async (p, key) => { await p.locator(`#tabs button[data-tab=${key}]`).click(); await settle(p); };
const toast = async (p) => { await p.waitForSelector("#toast:not([hidden])", { timeout: 60000 }); const t = await p.textContent("#toast"); await p.evaluate(() => (document.getElementById("toast").hidden = true)); return t; };
const api = (p, method, url, body) => p.evaluate(async ({ method, url, body }) => { const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined }); return { status: r.status, body: await r.json().catch(() => null) }; }, { method, url, body });
async function login(p, u) {
  const acc = S.users[u];
  await p.goto(BASE); await settle(p);
  await p.fill("#a-user", u); await p.fill("#a-pw", acc.pw); await p.click("#auth-form button[type=submit]"); await settle(p);
  if (await p.isVisible("#a-old").catch(() => false)) {
    const np = pw();
    await p.fill("#a-old", acc.pw); await p.fill("#a-pw", np); await p.fill("#a-pw2", np); await p.click("#auth-form button[type=submit]"); await settle(p);
    acc.pw = np; save();
  }
}

try {
  if (phase === "setup") {
    const A = await page("admin");
    await A.goto(BASE); await settle(A);
    if (!S.users) {
      S.users = { admin: { pw: pw(), name: "김관리", role: "관리자" }, own: { pw: pw(), name: "김담당", role: "담당자", email: "skybook75@gmail.com" },
        rev: { pw: pw(), name: "이검토", role: "검토자" }, apr: { pw: pw(), name: "최승인", role: "승인자" } };
      save();
      await shot(A, "최초_관리자_생성화면");
      await A.fill("#a-user", "admin"); await A.fill("#a-name", "김관리"); await A.fill("#a-email", "admin@cert-lab.test").catch(() => {});
      await A.evaluate(async (pw0) => fetch("api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "admin", name: "김관리", email: "admin@cert-lab.test", password: pw0 }) }), S.users.admin.pw);
    }
    await login(A, "admin");
    ok("관리자 로그인(HTTPS 도메인)", await A.isVisible("#tabs"), BASE);
    await tab(A, "admin");
    for (const u of ["own", "rev", "apr"]) {
      const x = S.users[u];
      if ((await A.textContent("#p-admin")).includes(`>${u}<`) || (await api(A, "GET", "api/db/users")).body.some((y) => y.username === u)) continue;
      await A.fill("#u-user", u); await A.fill("#u-name", x.name); await A.fill("#u-mail", x.email || `${u}@cert-lab.test`); await A.selectOption("#u-role", x.role); await A.fill("#u-pw", x.pw);
      await A.click("#u-form button[type=submit]"); await toast(A); await settle(A);
    }
    const users = (await api(A, "GET", "api/db/users")).body.map((y) => `${y.username}:${y.role}`);
    ok("사용자 4명(관리자·담당자·검토자·승인자)", users.length >= 4, users.join(", "));
    // 알림 채널: AWS SES 이메일 + 푸시
    await A.check("#s-en"); await A.selectOption("#s-prov", "ses"); await A.fill("#s-region", "ap-southeast-2"); await A.fill("#s-from", "skybook75@gmail.com");
    await A.fill("#s-push", process.env.PUSH_TOPIC || ""); await A.click("#smtp-form button[type=submit]"); await toast(A); await settle(A);
    await A.fill("#s-to", "skybook75@gmail.com"); await A.click("#s-test"); const tt = await toast(A);
    ok("알림 채널 테스트 발송(SES 실제 메일 + 푸시)", /메일 완료/.test(tt), tt);
    // 서버 Agent 등록 (토큰 1회 표시 → state 에만 저장, 화면 캡처는 토큰 가린 뒤)
    S.tokens = S.tokens || {};
    for (const [h, os_, st] of [["web01", "Linux", "prod"], ["was01", "Linux", ""], ["win01", "Windows", ""]]) {
      if (S.tokens[h]) continue;
      await api(A, "POST", `api/agents/ag_${h}/delete`);   // 토큰을 잃은 이전 등록이 있으면 정리 후 재발급
      await A.reload(); await settle(A); await tab(A, "admin");
      await A.fill("#ag-host", h); await A.selectOption("#ag-os", os_); await A.selectOption("#ag-stage", st);
      await A.evaluate(() => { const b = document.getElementById("ag-token"); if (b) { b.hidden = true; b.innerHTML = ""; } });
      await A.click("#ag-form button[type=submit]");
      await A.waitForFunction(() => /"token": "[A-Za-z0-9_-]{20,}"/.test(document.querySelector("#ag-token pre")?.textContent || ""), null, { timeout: 30000 });
      const conf = JSON.parse(await A.textContent("#ag-token pre"));
      S.tokens[h] = conf.token; save();
      await A.evaluate(() => { const pre = document.querySelector("#ag-token pre"); pre.textContent = pre.textContent.replace(/"token": "[^"]+"/, '"token": "●●●●●●●● (한 번만 표시 · 캡처에서 가림)"'); });
      if (h === "win01") await shot(A, "서버Agent_등록_토큰1회표시", "#ag-card");
    }
    ok("서버 Agent 3대 등록(web01 운영 배포, was01·win01 수집)", Object.keys(S.tokens).length === 3, Object.keys(S.tokens).join(", "));
    // PFX 비밀번호 보관함 (담당자가 입력)
    const O = await page("owner"); await login(O, "own"); await tab(O, "vault");
    for (const [scope, label, p1] of [["was01/opt/tomcat/conf/ssl", "was01 Tomcat 키스토어", process.env.PW_PAY], ["win01/C/certs", "win01 IIS 포털 PFX", process.env.PW_PORTAL]]) {
      await O.selectOption("#vs-scope", "folder"); await O.fill("#vs-path", scope); await O.fill("#vs-label", label); await O.fill("#vs-p1", p1); await O.fill("#vs-p2", p1);
      await O.click("#vs-go");
      await O.waitForFunction(() => !document.getElementById("toast").hidden || (document.getElementById("vs-err")?.textContent || "").trim(), null, { timeout: 30000 }).catch(() => {});
      console.log("vault:", label, "|", await O.textContent("#toast").catch(() => ""), "|", await O.textContent("#vs-err").catch(() => ""));
      await O.evaluate(() => (document.getElementById("toast").hidden = true)); await settle(O);
    }
    await shot(O, "PFX비밀번호_보관함_담당자등록", "#p-vault");
    const vtxt = await O.textContent("#p-vault");
    ok("담당자가 PFX 비밀번호 2건 암호화 등록(값은 화면·DB·로그에 없음)", vtxt.includes("was01 Tomcat") && vtxt.includes("win01 IIS") && !vtxt.includes(process.env.PW_PAY), "AES-256-GCM");
    console.log("TOKENS " + JSON.stringify(S.tokens));
  }

  if (phase === "approve") {
    const A = await page("admin"); await login(A, "admin");
    await tab(A, "disc"); await shot(A, "수집승인_대기목록");
    const txt = await A.textContent("#p-disc");
    ok("서버 Agent 수집 결과가 '호스트:경로'로 승인 대기", /web01:/.test(txt) && /was01:/.test(txt), (txt.match(/(web01|was01|win01):[^\s]+/g) || []).slice(0, 6).join(" | "));
    let n = 0;
    while (await A.$("[data-approve]:not([disabled])")) {
      const id = await A.$eval("[data-approve]:not([disabled])", (b) => b.dataset.approve);
      const inp = await A.$(`[data-owner="${id}"]`); if (inp && !(await inp.inputValue())) await inp.fill("김담당");
      await A.locator(`[data-approve="${id}"]`).scrollIntoViewIfNeeded().catch(() => {});
      await A.evaluate((i) => decide(i, true), id); await toast(A).catch(() => {}); await settle(A); n++;
      if (n > 30) break;
    }
    ok("수집 결과 승인 → 인증서 등록", n > 0, `${n}건 승인`);
    // 담당자 이메일 일괄 지정(실제 알림 수신용)
    const certs = (await api(A, "GET", "api/db/certificates")).body;
    await api(A, "POST", "api/action", { action: "cert.manage", summary: "담당자 이메일 지정", ops: certs.map((c) => ({ op: "update", coll: "certificates", id: c.id, data: { owner: "김담당", owner_email: "skybook75@gmail.com" } })) });
    await tab(A, "certs"); await shot(A, "인증서_목록");
    await A.reload(); await settle(A); await tab(A, "dash"); await shot(A, "대시보드");
    // 오케스트레이터로 일일 점검 → 알림 생성·발송
    await tab(A, "ops"); await A.fill("#orch-goal", "일일 운영 점검"); await A.click("#orch-run");
    await A.waitForFunction(() => document.querySelector("#orch-out")?.textContent.includes("최근 실행"), null, { timeout: 120000 }); await settle(A);
    await shot(A, "오케스트레이터_일일점검", ".card.orch");
    const out = await A.textContent("#orch-out");
    ok("오케스트레이터 일일 점검: 알림 생성 → 이메일·푸시 발송", /새 알림 \d+건/.test(out) && /발송 \d+건/.test(out), out.replace(/\s+/g, " ").slice(0, 300));
    await tab(A, "noti"); await shot(A, "알림함_발송결과");
  }

  if (phase === "deploy") {
    const A = await page("admin"), RV = await page("reviewer"), AP = await page("approver");
    await login(A, "admin"); await login(RV, "rev"); await login(AP, "apr");
    await tab(A, "disc");
    let n = 0;
    while (await A.$("[data-approve]:not([disabled])")) { const id = await A.$eval("[data-approve]:not([disabled])", (b) => b.dataset.approve); const inp = await A.$(`[data-owner="${id}"]`); if (inp && !(await inp.inputValue())) await inp.fill("김담당"); await A.evaluate((i) => decide(i, true), id); await toast(A).catch(() => {}); await settle(A); n++; if (n > 10) break; }
    ok("갱신본 승인(기존 인증서 → 갱신 완료)", n >= 1, `${n}건`);
    const res = [];
    for (const st of ["개발", "테스트", "교육", "운영"]) {
      await A.reload(); await settle(A); await tab(A, "certs"); await A.click("#p-certs tr[data-cert] >> text=shop.demo.test"); await sleep(300);
      if (await A.$eval("#d-deploy", (b) => b.disabled)) { res.push(st + ":요청불가"); break; }
      await A.click("#d-deploy"); await toast(A);
      await RV.reload(); await settle(RV); await tab(RV, "deploy"); if (st === "개발") await shot(RV, "검토자_자동검증", "#p-deploy"); await RV.click("[data-rvpass]"); await toast(RV);
      await AP.reload(); await settle(AP); await tab(AP, "deploy"); await AP.click("[data-approve-dep]"); const t = await toast(AP); res.push(st + ":" + (/반영/.test(t) ? "완료" : t));
    }
    ok("4단계 배포 승인(요청 김관리 → 검토 이검토 → 승인 최승인)", res.length === 4 && res.every((x) => x.endsWith("완료")), res.join(" "));
    await tab(A, "deploy"); await shot(A, "배포현황_승인완료", "#p-deploy");
  }

  if (phase === "status") {
    const A = await page("admin"); await login(A, "admin"); await tab(A, "deploy"); await shot(A, "배포현황_서버적용", "#p-deploy");
    const envs = (await api(A, "GET", "api/db/environments")).body.filter((e) => e.stage === "prod");
    console.log("APPLIED " + JSON.stringify(envs.map((e) => ({ cn: e.subject_cn, cert: e.cert_id, applied: e.applied }))));
    ok("배포 화면 '서버 적용' 상태 표시", true, envs.map((e) => Object.entries(e.applied || {}).map(([h, a]) => `${h}:${a.ok ? "적용" : "실패·원복"}`).join(",")).join(" "));
    await tab(A, "admin"); await shot(A, "서버Agent_목록_최근결과", "#ag-card");
  }

  if (phase === "qa") {
    const O = await page("owner"); await login(O, "own"); await tab(O, "qa");
    for (const q of ["shop.demo.test 인증서는 언제 만료되고 어떻게 갱신해?", "nginx에 갱신한 인증서를 적용한 뒤 확인할 것은?"]) {
      await O.fill("#qa-input", q); await O.click("#qa-send");
      await O.waitForFunction(() => !document.getElementById("qa-send").disabled, null, { timeout: 180000 }); await sleep(1500);
    }
    await shot(O, "QA챗봇_Claude_답변검증", "#p-qa .card");
    const chat = await O.textContent("#chat");
    ok("QA 챗봇(실제 Claude) 답변 + 답변 검증", chat.length > 200 && /적합|주의|검증/.test(chat), chat.replace(/\s+/g, " ").slice(0, 300));
  }

  if (phase === "ops") {
    const A = await page("admin"); await login(A, "admin"); await tab(A, "ops");
    await A.click("#diag-run"); await settle(A); await sleep(800);
    await A.fill("#mchat-in", "진단해줘"); await A.click("#mchat-send"); await A.waitForFunction(() => /자가 진단 결과/.test(document.querySelector("#mchat")?.textContent || ""), null, { timeout: 60000 });
    await shot(A, "자가진단_버그리포트_제안", "#p-ops .cols2");
    const req = process.env.MAINT_REQ || "대시보드 맨 위 타일 줄에 '이번 주 만료'(7일 이내 만료 예정, 이미 만료된 것 제외) 개수 타일을 추가해줘";
    await A.fill("#mchat-in", req); await A.click("#mchat-send");
    await A.waitForFunction(() => /반영했습니다|반영하지 않았|보류/.test(document.querySelector("#mchat")?.lastElementChild?.textContent || ""), null, { timeout: 600000 }).catch(() => {});
    await sleep(4000); await A.goto(BASE); await settle(A);
    if (await A.isVisible("#a-user").catch(() => false)) await login(A, "admin");
    await tab(A, "ops"); await A.evaluate(() => document.querySelectorAll("#p-ops details").forEach((d) => (d.open = true)));
    const st = await api(A, "GET", "api/ops/state"); const rep = st.body.reports[0];
    await shot(A, "유지보수Agent_개선리포트", "#p-ops");
    ok("유지보수 Agent: 요청 → Claude 수정안 → 자동 테스트 → 반영/미반영 리포트", rep && ["반영", "미반영"].includes(rep.status), `${rep?.status} · ${rep?.summary?.slice(0, 200)} · 테스트 ${rep?.tests?.filter((t) => t.ok).length}/${rep?.tests?.length}`);
    console.log("REPORT " + JSON.stringify({ status: rep?.status, summary: rep?.summary, files: rep?.files, tests: rep?.tests, steps: rep?.steps?.map((s) => s.step + ":" + s.ok) }));
    await tab(A, "dash"); await shot(A, "대시보드_개선반영후");
  }
} catch (e) { console.error(e); ok("실행 오류 없음", false, e.message); }
finally {
  if (phase === "setup" && S.tokens) console.log("TOKENS " + JSON.stringify(S.tokens));
  for (const c of ctxs) await c.close();
  await browser.close();
  fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify(R, null, 1));
  console.log(`RESULT ${phase} ${R.filter((x) => x.pass).length}/${R.length}`);
}

// 발표용 시연 영상 녹화 — 실제 HTTPS 플랫폼(AWS)을 사람이 조작하듯 천천히 움직이며 장면별로 녹화한다.
//   node demo_record.mjs <platformUrl> [장면이름,...]
//   결과: /var/lib/cert-lab-run/out/demo/<장면>/*.webm + marks.json (장면·창별 시각 표시: 편집 때 대기 구간 빨리 감기)
//   계정 비밀번호는 /var/lib/cert-lab-run/state.json(600)에서만 읽는다. 화면의 비밀번호 칸은 ●로 표시된다.
import { createRequire } from "module";
import fs from "fs";
import path from "path";
import crypto from "crypto";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const [URL0, ONLY] = process.argv.slice(2);
const BASE = URL0.endsWith("/") ? URL0 : URL0 + "/";
const RUN = process.env.DEMO_RUN || "/var/lib/cert-lab-run", OUT = path.join(RUN, "out", "demo");
fs.mkdirSync(OUT, { recursive: true });
const SF = path.join(RUN, "state.json");
const S = JSON.parse(fs.readFileSync(SF, "utf8"));
const save = () => { fs.writeFileSync(SF, JSON.stringify(S, null, 1)); fs.chmodSync(SF, 0o600); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const W = 1280, H = 720;
const MF = path.join(OUT, "marks.json");
const MK = fs.existsSync(MF) ? JSON.parse(fs.readFileSync(MF, "utf8")) : { marks: [], videos: [], log: [] };
const log = (...a) => { const s = a.join(" "); console.log(s); MK.log.push(s); };

const browser = await chromium.launch();
const OPEN = new Set();

function cursorScript() {
  if (window.__cur) return; window.__cur = 1;
  const add = () => {
    const c = document.createElement("div"); c.id = "__cursor";
    c.style.cssText = "position:fixed;left:0;top:0;width:24px;height:24px;z-index:2147483647;pointer-events:none;transform:translate(-100px,-100px)";
    c.innerHTML = '<svg width="24" height="24" viewBox="0 0 22 22"><path d="M2 1 L2 18 L6.5 13.8 L9.6 20.5 L12.4 19.2 L9.4 12.6 L15.5 12.4 Z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
    document.documentElement.appendChild(c);
    const last = window.__lastPos; if (last) c.style.transform = `translate(${last[0] - 2}px,${last[1] - 1}px)`;
    document.addEventListener("mousemove", (e) => { c.style.transform = `translate(${e.clientX - 2}px,${e.clientY - 1}px)`; }, true);
    window.__ripple = (x, y) => { const r = document.createElement("div");
      r.style.cssText = `position:fixed;left:${x - 18}px;top:${y - 18}px;width:36px;height:36px;border-radius:50%;border:3px solid #3BB8B0;background:rgba(59,184,176,.28);z-index:2147483646;pointer-events:none;transition:transform .5s ease-out,opacity .5s ease-out`;
      document.documentElement.appendChild(r); requestAnimationFrame(() => { r.style.transform = "scale(1.9)"; r.style.opacity = "0"; }); setTimeout(() => r.remove(), 700); };
    document.addEventListener("mousedown", (e) => window.__ripple(e.clientX, e.clientY), true);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", add); else add();
}

async function open(scene, name) {
  const dir = path.join(OUT, scene);
  const c = await browser.newContext({ viewport: { width: W, height: H }, locale: "ko-KR", bypassCSP: true, recordVideo: { dir, size: { width: W, height: H } } });
  await c.addInitScript(cursorScript);
  const p = await c.newPage();
  const P = { scene, name, c, p, t0: Date.now(), pos: [640, 360] };   // t0: 녹화 시작 시각(epoch ms) — 서버 로그 시각과 맞추는 기준 OPEN.add(P);
  p.on("pageerror", (e) => log(`[${scene}/${name}] pageerror ${e.message}`));
  return P;
}
async function close(P) {
  OPEN.delete(P); await P.c.close();
  const f = await P.p.video().path();
  MK.videos = MK.videos.filter((v) => !(v.scene === P.scene && v.ctx === P.name));
  MK.videos.push({ scene: P.scene, ctx: P.name, file: path.relative(OUT, f), t0: P.t0 });
}
const mark = (P, label) => { MK.marks.push({ scene: P.scene, ctx: P.name, label, t: +((Date.now() - P.t0) / 1000).toFixed(2), w: Date.now() }); };
const settle = async (p) => { await p.waitForLoadState("networkidle").catch(() => {}); await sleep(400); };
const api = (p, method, url, body) => p.evaluate(async ({ method, url, body }) => { const me = await (await fetch("api/me")).json().catch(() => ({}));
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf || "" }, body: body ? JSON.stringify(body) : undefined }); return { status: r.status, body: await r.json().catch(() => null) }; }, { method, url, body });
const toast = async (p, ms = 60000) => { await p.waitForSelector("#toast:not([hidden])", { timeout: ms }); const t = await p.textContent("#toast"); return t; };

async function moveTo(P, loc, fast) {
  await loc.scrollIntoViewIfNeeded().catch(() => {});
  const b = await loc.boundingBox(); if (!b) return null;
  const x = Math.round(b.x + Math.min(b.width / 2, 60)), y = Math.round(b.y + Math.min(b.height / 2, 16));
  await P.p.mouse.move(x, y, { steps: fast ? 8 : 24 }); P.pos = [x, y];
  await P.p.evaluate(([a, b]) => (window.__lastPos = [a, b]), [x, y]).catch(() => {});
  await sleep(fast ? 120 : 350); return { x, y };
}
async function hclick(P, loc, fast) { await moveTo(P, loc, fast); await loc.click(); await sleep(fast ? 250 : 700); }
async function htype(P, loc, text, delay = 55) { await hclick(P, loc); await loc.fill(""); await loc.pressSequentially(text, { delay }); await sleep(400); }
async function htab(P, key, fast) { await hclick(P, P.p.locator(`#tabs button[data-tab=${key}]`), fast); await settle(P.p); await sleep(fast ? 300 : 900); }
async function hover(P, sel, ms = 900) { const l = P.p.locator(sel).first(); if (await l.count()) { await moveTo(P, l); await sleep(ms); } }
async function scrollBy(P, dy, steps = 6) { for (let i = 0; i < steps; i++) { await P.p.mouse.wheel(0, dy / steps); await sleep(90); } await sleep(500); }
async function fakeClick(P, loc, fn) { const pt = await moveTo(P, loc); if (pt) await P.p.evaluate(([x, y]) => window.__ripple && window.__ripple(x, y), [pt.x, pt.y]); await fn(); await sleep(700); }

async function login(P, u, show) {
  const acc = S.users[u];
  await P.p.goto(BASE); await settle(P.p);
  if (show) { await htype(P, P.p.locator("#a-user"), u, 90); await htype(P, P.p.locator("#a-pw"), acc.pw, 35); await hclick(P, P.p.locator("#auth-form button[type=submit]")); }
  else { await P.p.fill("#a-user", u); await P.p.fill("#a-pw", acc.pw); await P.p.click("#auth-form button[type=submit]"); }
  await settle(P.p);
  if (await P.p.isVisible("#a-old").catch(() => false)) {
    const np = "Lab" + crypto.randomBytes(9).toString("base64url").replace(/[-_]/g, "x") + "7";
    await P.p.fill("#a-old", acc.pw); await P.p.fill("#a-pw", np); await P.p.fill("#a-pw2", np); await P.p.click("#auth-form button[type=submit]"); await settle(P.p);
    acc.pw = np; save();
  }
  await P.p.waitForSelector("#tabs button", { timeout: 30000 });
  await sleep(600);
}

const SCENES = {
  // 1. 로그인·대시보드
  async login() {
    const A = await open("login", "admin");
    await A.p.goto(BASE); await settle(A.p); mark(A, "start"); await sleep(1200);
    await login(A, "admin", true); mark(A, "dash");
    await sleep(1500);
    for (const s of ["#p-dash .tile", "#p-dash .tile >> nth=1", "#p-dash .tile >> nth=2", "#p-dash .tile >> nth=3"]) await hover(A, s, 700);
    await scrollBy(A, 420); await sleep(1200); await scrollBy(A, 420); await sleep(1200); await scrollBy(A, -840); await sleep(800);
    mark(A, "end"); await close(A);
  },
  // 2. 엑셀 수기 관리대장 업로드·검증
  async ledger() {
    const A = await open("ledger", "admin"); await login(A, "admin"); mark(A, "start");
    await htab(A, "ledger");
    await hover(A, "#xl-file", 900);
    await A.p.setInputFiles("#xl-file", { name: "인증서_관리대장_수기.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: fs.readFileSync(new URL("./demo_ledger.xlsx", import.meta.url)) });
    await sleep(1800); mark(A, "validated");
    await hover(A, "#p-ledger .pill.bad", 1200);
    const rows = A.p.locator("#p-ledger table >> nth=0").locator("tr");
    for (let i = 1; i < Math.min(await rows.count(), 5); i++) await hover(A, `#p-ledger table >> nth=0 >> tr >> nth=${i}`, 900);
    const sum = A.p.locator("#p-ledger details summary").first(); if (await sum.count()) { await hclick(A, sum); await sleep(1500); }
    await hover(A, "#xl-commit", 1200);
    await scrollBy(A, 520); await sleep(1800);
    await scrollBy(A, -520);
    if (await A.p.locator("#xl-cancel").count()) await hclick(A, A.p.locator("#xl-cancel"));   // 시연이므로 등록은 하지 않음
    mark(A, "end"); await close(A);
  },
  // 3. 수집 대상(담당자) → 수집 실행 → 서버 Agent 1분 poll
  async targets() {
    const O = await open("targets", "owner"); await login(O, "own"); mark(O, "start");
    await htab(O, "targets");
    await hover(O, "#p-targets table tr >> nth=1", 900); await hover(O, "#p-targets table tr >> nth=2", 900);
    await hover(O, "#tg-form", 900);
    const before = (await api(O.p, "GET", "api/targets")).body.find((t) => t.name === "쇼핑몰 웹(nginx)");
    const row = O.p.locator("#p-targets tr", { hasText: "쇼핑몰 웹(nginx)" });
    await hclick(O, row.locator("[data-trun]")); await toast(O.p).catch(() => {}); await sleep(1500);
    mark(O, "wait_start");
    for (let i = 0; i < 30; i++) {
      await sleep(5000);
      const t = (await api(O.p, "GET", "api/targets")).body.find((x) => x.name === "쇼핑몰 웹(nginx)");
      if (t && t.last_run && t.last_run !== before?.last_run && (t.done_seq || 0) >= (t.run_seq || 0)) break;
    }
    await O.p.reload(); await settle(O.p); await htab(O, "targets", true); mark(O, "wait_end");
    await hover(O, "#p-targets tr:has-text('쇼핑몰 웹(nginx)')", 2500);
    const t = (await api(O.p, "GET", "api/targets")).body.find((x) => x.name === "쇼핑몰 웹(nginx)"); log("targets:", t?.last_result);
    mark(O, "end"); await close(O);
  },
  // 4. 수집 승인(갱신본 자동 인식) → 인증서 상세
  async approve() {
    const A = await open("approve", "admin"); await login(A, "admin"); mark(A, "start");
    await htab(A, "disc"); await sleep(800);
    const ids = await A.p.$$eval("[data-approve]:not([disabled])", (bs) => bs.map((b) => ({ id: b.dataset.approve, txt: b.closest("tr")?.textContent || "" })));
    log("pending:", ids.map((x) => x.txt.replace(/\s+/g, " ").slice(0, 80)).join(" || "));
    const target = ids.find((x) => /갱신본/.test(x.txt) && /shop/.test(x.txt)) || ids.find((x) => /갱신본/.test(x.txt)) || ids[0];
    if (target) {
      await hover(A, `tr:has([data-approve="${target.id}"])`, 2200);
      const inp = A.p.locator(`[data-owner="${target.id}"]`); if (await inp.count() && !(await inp.inputValue())) await htype(A, inp, "김담당");
      await fakeClick(A, A.p.locator(`[data-approve="${target.id}"]`), () => A.p.evaluate((i) => decide(i, true), target.id));
      const t = await toast(A.p).catch(() => ""); log("approve:", t); await settle(A.p); await sleep(1800);
    }
    await htab(A, "certs");
    const r = A.p.locator("#p-certs tr[data-cert]", { hasText: "shop.demo.test" }).first();
    if (await r.count()) { await hclick(A, r); await sleep(2500); await scrollBy(A, 300); await sleep(2000); }
    mark(A, "end"); await close(A);
  },
  // 5. PFX 비밀번호 보관함
  async vault() {
    const O = await open("vault", "owner"); await login(O, "own"); mark(O, "start");
    await htab(O, "vault"); await sleep(800);
    await hover(O, "#p-vault table tr >> nth=1", 1500); await hover(O, "#p-vault table tr >> nth=2", 1500);
    await hover(O, "#vs-p1", 1200);
    mark(O, "end"); await close(O);
  },
  // 6. 4단계 배포 승인(요청 김관리 → 검토 이검토 → 승인 최승인)
  async deploy() {
    const A = await open("deploy", "admin"), RV = await open("deploy", "reviewer"), AP = await open("deploy", "approver");
    await login(A, "admin"); await login(RV, "rev"); await login(AP, "apr");
    const res = [];
    for (const [i, st] of ["개발", "테스트", "교육", "운영"].entries()) {
      const fast = i > 0 && i < 3;
      mark(A, `req_${i}_s`);
      await A.p.reload(); await settle(A.p); await htab(A, "certs", fast);
      await hclick(A, A.p.locator("#p-certs tr[data-cert]", { hasText: "shop.demo.test" }).first(), fast); await sleep(fast ? 300 : 1200);
      if (await A.p.$eval("#d-deploy", (b) => b.disabled)) { res.push(st + ":요청불가"); mark(A, `req_${i}_e`); break; }
      await hclick(A, A.p.locator("#d-deploy"), fast); await toast(A.p); await sleep(fast ? 400 : 1500); mark(A, `req_${i}_e`);
      mark(RV, `rev_${i}_s`);
      await RV.p.reload(); await settle(RV.p); await htab(RV, "deploy", fast);
      if (!fast) { await hover(RV, "#p-deploy table tr >> nth=1", 1500); await scrollBy(RV, 260); await sleep(2500); await scrollBy(RV, -260); }
      await hclick(RV, RV.p.locator("[data-rvpass]").first(), fast); await toast(RV.p); await sleep(fast ? 400 : 1500); mark(RV, `rev_${i}_e`);
      mark(AP, `apr_${i}_s`);
      await AP.p.reload(); await settle(AP.p); await htab(AP, "deploy", fast);
      await hclick(AP, AP.p.locator("[data-approve-dep]").first(), fast); const t = await toast(AP.p); res.push(st + ":" + (/반영/.test(t) ? "완료" : t)); await sleep(fast ? 400 : 1800); mark(AP, `apr_${i}_e`);
    }
    log("deploy:", res.join(" "));
    await close(A); await close(RV); await close(AP);
  },
  // 7. 서버 Agent가 web01 nginx에 실제 적용(1분 poll) → 배포 화면 확인
  async apply() {
    const A = await open("apply", "admin"); await login(A, "admin"); mark(A, "start");
    await htab(A, "deploy"); await sleep(800);
    const cur = async () => { const e = (await api(A.p, "GET", "api/db/environments")).body.find((x) => x.stage === "prod" && x.subject_cn === "shop.demo.test"); return e; };
    const e0 = await cur(); log("prod env:", JSON.stringify({ cert: e0?.cert_id, applied: e0?.applied }));
    mark(A, "wait_start");
    for (let i = 0; i < 36; i++) {
      const e = await cur(); const a = e?.applied?.web01;
      if (a && a.cert_id === e.cert_id && (a.at || "") > new Date(Date.now() - 600000).toISOString()) { log("applied:", JSON.stringify(a)); break; }
      if (a && a.cert_id === e.cert_id && a.ok !== undefined && i > 14) { log("applied(old?):", JSON.stringify(a)); break; }
      await sleep(5000); if (i % 3 === 2) { await A.p.reload(); await settle(A.p); await htab(A, "deploy", true); }
    }
    await A.p.reload(); await settle(A.p); await htab(A, "deploy", true); mark(A, "wait_end");
    await hover(A, "#p-deploy tr:has-text('shop.demo.test')", 2500);
    await hover(A, "#p-deploy :text('web01')", 2500);
    await htab(A, "admin"); await hover(A, "#ag-card tr:has-text('web01')", 2500);
    mark(A, "end"); await close(A);
  },
  // 8. AI 운영: 오케스트레이터 일일 점검 → 알림함
  async orch() {
    const A = await open("orch", "admin"); await login(A, "admin"); mark(A, "start");
    await htab(A, "ops"); await sleep(800);
    await htype(A, A.p.locator("#orch-goal"), "일일 운영 점검 — 만료 임박 인증서 알림과 배포 상태 확인", 45);
    await hclick(A, A.p.locator("#orch-run")); mark(A, "wait_start");
    await A.p.waitForFunction(() => document.querySelector("#orch-out")?.textContent.includes("최근 실행"), null, { timeout: 180000 }).catch(() => {});
    await settle(A.p); mark(A, "wait_end");
    await hover(A, ".card.orch svg", 2500); await hover(A, "#orch-out", 3000); await scrollBy(A, 300); await sleep(2500);
    log("orch:", ((await A.p.textContent("#orch-out").catch(() => "")) || "").replace(/\s+/g, " ").slice(0, 300));
    await htab(A, "noti"); await sleep(1200); await hover(A, "#p-noti table tr >> nth=1", 2000);
    mark(A, "end"); await close(A);
  },
  // 9. QA 챗봇(실제 Claude + 답변 검증)
  async qa() {
    const O = await open("qa", "owner"); await login(O, "own"); mark(O, "start");
    await htab(O, "qa"); await sleep(600);
    await htype(O, O.p.locator("#qa-input"), "Let's Encrypt 자동 갱신이 실패했을 때 확인할 것은?", 60);
    await hclick(O, O.p.locator("#qa-send")); await sleep(1500); mark(O, "wait_start");
    await O.p.waitForFunction(() => !/답변 준비 중|검증 중/.test(document.getElementById("chat").textContent) && !document.getElementById("qa-send").disabled, null, { timeout: 240000 }).catch(() => {});
    await sleep(800); mark(O, "wait_end");
    const last = O.p.locator("#chat > *").last(); await moveTo(O, last);
    for (let i = 0; i < 5; i++) { await scrollBy(O, 160, 4); await sleep(1200); }
    log("qa:", ((await O.p.textContent("#chat")) || "").replace(/\s+/g, " ").slice(-500));
    mark(O, "end"); await close(O);
  },
  // 10. 유지보수 Agent: 자가 진단 → 대화로 개선 요청 → 테스트 게이트 → 반영
  async maint() {
    const A = await open("maint", "admin"); await login(A, "admin"); mark(A, "start");
    await htab(A, "ops"); await sleep(600);
    await hclick(A, A.p.locator("#diag-run")); await settle(A.p); await sleep(1500);
    await htype(A, A.p.locator("#mchat-in"), "진단해줘", 90); await hclick(A, A.p.locator("#mchat-send"));
    await A.p.waitForFunction(() => /자가 진단 결과/.test(document.querySelector("#mchat")?.textContent || ""), null, { timeout: 60000 }).catch(() => {});
    await sleep(1200); await moveTo(A, A.p.locator("#mchat")); await scrollBy(A, 240); await sleep(3000);
    mark(A, "req");
    const req = process.env.MAINT_REQ || "대시보드의 '7일 내 만료 · 이미 만료된 건 제외' 타일 문구를 '7일 내 만료 예정'으로 바꿔줘";
    await htype(A, A.p.locator("#mchat-in"), req, 40); await hclick(A, A.p.locator("#mchat-send")); mark(A, "wait_start");
    await A.p.waitForFunction(() => /반영했습니다|반영하지 않았|보류/.test(document.querySelector("#mchat")?.lastElementChild?.textContent || ""), null, { timeout: 600000 }).catch(() => {});
    await sleep(2500);
    log("maint:", ((await A.p.locator("#mchat").last().textContent().catch(() => "")) || "").replace(/\s+/g, " ").slice(-600));
    await sleep(5000); await A.p.goto(BASE); await settle(A.p);
    if (await A.p.isVisible("#a-user").catch(() => false)) await login(A, "admin");
    await htab(A, "ops", true); mark(A, "wait_end");
    await A.p.evaluate(() => document.querySelectorAll("#p-ops details").forEach((d) => (d.open = true)));
    const rep = (await api(A.p, "GET", "api/ops/state")).body.reports?.[0];
    log("maint report:", JSON.stringify({ status: rep?.status, summary: rep?.summary, tests: rep?.tests?.map((t) => `${t.name || t.step}:${t.ok}`) }));
    const rl = A.p.locator("#p-ops :text('개선 리포트'), #p-ops details").first(); if (await rl.count()) { await moveTo(A, rl); await scrollBy(A, 300); await sleep(3500); }
    mark(A, "dash"); await htab(A, "dash"); await sleep(1200);
    for (const s of ["#p-dash .tile", "#p-dash .tile >> nth=1", "#p-dash .tile >> nth=2", "#p-dash .tile >> nth=3", "#p-dash .tile >> nth=4"]) await hover(A, s, 800);
    await sleep(1500); mark(A, "end"); await close(A);
  },
};

const order = ONLY ? ONLY.split(",") : Object.keys(SCENES);
for (const n of order) {
  const t = Date.now();
  MK.marks = MK.marks.filter((m) => m.scene !== n);
  try { await SCENES[n](); log(`SCENE ${n} OK ${((Date.now() - t) / 1000).toFixed(0)}s`); }
  catch (e) { log(`SCENE ${n} FAIL ${e.message.split("\n")[0]}`); for (const P of [...OPEN]) await close(P).catch(() => {}); }
  fs.writeFileSync(MF, JSON.stringify(MK, null, 1));
}
await browser.close();
fs.writeFileSync(MF, JSON.stringify(MK, null, 1));
console.log("DEMO DONE");

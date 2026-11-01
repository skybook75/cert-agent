// QA 챗봇: 가이드 업로드 → RAG 자동 구성 → 답변 → 답변 검증 AI 피드백 (Claude 는 모의 응답으로 대체해 흐름만 검증)
const { chromium } = require("playwright"); const http = require("http"); const fs = require("fs"); const path = require("path");
const WEB = path.join(__dirname, "..", "web"), GUIDES = path.join(__dirname, "..", "guides", "guides.json");
const srv = http.createServer((q, r) => { const u = decodeURIComponent(q.url.split("?")[0]); const f = u === "/" ? path.join(WEB, "index.html") : u === "/guides.json" ? GUIDES : path.join(WEB, u.slice(1));
  if (!fs.existsSync(f) || u.startsWith("/api")) { r.writeHead(404); return r.end("no"); } r.writeHead(200, { "Content-Type": /\.m?js$/.test(f) ? "text/javascript" : f.endsWith(".json") ? "application/json" : f.endsWith(".bcmap") ? "application/octet-stream" : "text/html; charset=utf-8" });
  r.end(f.endsWith("index.html") ? "<!doctype html><meta charset=utf-8><body>" + fs.readFileSync(f, "utf8") : fs.readFileSync(f)); }).listen(8791);
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: d }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${d}`); };
(async () => {
  const b = await chromium.launch(); const c = await b.newContext({ viewport: { width: 1300, height: 900 } }); await c.route(/fonts/, (r) => r.abort()); const p = await c.newPage();
  p.on("pageerror", (e) => console.log("pageerror", e.message));
  await p.addInitScript(() => {
    window.__prompts = [];
    const sample = async (prompt) => { window.__prompts.push(prompt);
      if (window.__mode === "bad" && !prompt.includes("[답변 검증 AI 피드백]")) return { text: "운영 교체 공지는 7일 전에 하고 openssl s_client -tls1_3 로 확인합니다 [U01-1] [U09-1]" };
      return { text: "운영 서버 인증서 교체 공지는 작업 3영업일 전까지 서비스 담당 부서와 고객센터에 합니다 [U01-1].\n공지에는 작업 일시, 대상 도메인, 예상 중단 시간, 원복 담당자 연락처를 적습니다 [U01-1]." }; };
    sample.json = async (prompt) => { window.__prompts.push(prompt);
      return window.__mode === "bad" && prompt.split("[답변]").pop().includes("7일 전에") ? { accuracy: 35, quality: 55, verdict: "부적합", unsupported: ["7일 전에 공지", "-tls1_3 옵션"], missing: ["공지 대상(고객센터)"], comment: "공지 시점이 근거(3영업일 전)와 다르고 근거에 없는 옵션을 안내했습니다." }
        : { accuracy: 95, quality: 88, verdict: "적합", unsupported: [], missing: [], comment: "근거와 일치하며 질문에 바로 답했습니다." }; };
    const caps = { db: null, sample, user: null, downloads: null };
    window.claude = { use: async (n) => caps[n] ?? null };
  });
  await p.goto("http://localhost:8791/"); await p.waitForTimeout(900);
  await p.click("#tabs button[data-tab=qa]");
  const before = await p.textContent("#qa-guides");
  const nBefore = +before.match(/검색 문단 (\d+)개/)[1];
  const files = ["사내_인증서_교체_작업_절차_예시.md", "GlobalSign_갱신_메모_예시.docx"].map((f) => ({ name: f, mimeType: f.endsWith(".md") ? "text/markdown" : "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: fs.readFileSync(path.join(__dirname, "guides", f)) }));
  await p.setInputFiles("#g-in", files); await p.waitForTimeout(800);
  const after = await p.textContent("#qa-guides"); const nAfter = +after.match(/검색 문단 (\d+)개/)[1];
  ok("가이드 업로드 → RAG 자동 구성(.md·.docx)", after.includes("업로드 2종") && after.includes("U01") && after.includes("U02") && nAfter > nBefore, `검색 문단 ${nBefore} → ${nAfter} · ${await p.textContent("#g-msg")}`);
  ok("Word 제목 스타일로 문단 분리·발급기관 자동 감지", after.includes("GlobalSign"), (after.match(/U02[^U]*/) || [""])[0].slice(0, 120));
  await p.fill("#qa-input", "운영 인증서 교체 작업 공지는 언제까지 누구에게 해야 해?"); await p.click("#qa-send"); await p.waitForTimeout(600);
  const prompt = await p.evaluate(() => window.__prompts[0]);
  ok("업로드 가이드가 검색되어 Claude 근거로 전달", /\[U01-\d\]/.test(prompt) && prompt.includes("3영업일"), (prompt.match(/\[U01-\d\][^\n]*/) || [""])[0]);
  await p.waitForSelector("#chat details.agentlog"); const v1 = await p.$$eval("#chat .verify", (e) => e.pop().textContent);
  ok("답변 검증 AI 피드백(적합) 답변 바로 아래 표시", v1.includes("적합") && v1.includes("정확도 95") && v1.includes("잘못된 인용 0"), v1.replace(/\s+/g, " ").slice(0, 140));
  const vp = await p.evaluate(() => window.__prompts[1]);
  ok("검증 AI는 별도 호출로 근거·답변을 받아 평가", vp.includes("답변 검증 AI") && vp.includes("[답변]") && vp.includes("3영업일"), "검증 프롬프트에 근거·답변 포함");
  await p.evaluate(() => (window.__mode = "bad"));
  await p.fill("#qa-input", "운영 인증서 교체 작업 공지는 언제 해?"); await p.click("#qa-send"); await p.waitForTimeout(600);
  await p.waitForSelector("#chat details.agentlog:nth-of-type(2)", { timeout: 5000 }).catch(() => {});
  const vs = await p.$$eval("#chat .verify", (e) => e.map((x) => x.textContent)); const v2 = vs[vs.length - 2] || "";
  const revisePrompt = await p.evaluate(() => window.__prompts.find((x) => x.includes("[답변 검증 AI 피드백]")) || "");
  const logTxt = await p.$$eval("#chat details.agentlog", (e) => e.pop().textContent);
  const sup = await p.$$eval("#chat .msg.superseded", (e) => e.length);
  ok("Self-Correction: 부적합 → 피드백 넣어 재작성 → 재검증 적합", revisePrompt.includes("근거로 확인되지 않음") && revisePrompt.includes("7일 전에 공지") && vs[vs.length - 1].includes("적합") && !vs[vs.length - 1].includes("부적합") && sup === 1 && logTxt.includes("Self-Correction 1회"),
    logTxt.replace(/\s+/g, " ").slice(0, 260));
  ok("잘못된 답변 → 부적합·근거 없는 내용·규칙 위반 표시", v2.includes("부적합") && v2.includes("7일 전에 공지") && v2.includes("잘못된 인용 1(U09-1)") && v2.includes("근거에 없는 수치 1(7일)"), v2.replace(/\s+/g, " ").slice(0, 200));
  await p.screenshot({ path: "/tmp/claude-0/-home-claude/92d09200-f21f-5312-b63e-f3817c0470d9/scratchpad/qa_verify.png", fullPage: false });
  // PDF 가이드: 브라우저 내장 글꼴 PDF + 한글 CID 글꼴 PDF(UniKS-UCS2-H) + 글자 없는 스캔본
  const pdfs = ["Nginx_SSL_설치_가이드_예시.pdf", "Tomcat_인증서_교체_메모_CID예시.pdf", "스캔본_예시.pdf"].map((f) => ({ name: f, mimeType: "application/pdf", buffer: fs.readFileSync(path.join(__dirname, "guides", f)) }));
  await p.setInputFiles("#g-in", pdfs); await p.waitForFunction(() => /스캔본_예시/.test(document.getElementById("g-msg")?.textContent || ""), null, { timeout: 30000 });
  const pmsg = await p.textContent("#g-msg");
  const secs = await p.evaluate(() => uploadedGuides().filter((g) => /\.pdf$/.test(g.sources[0])).map((g) => ({ code: g.code, title: g.title, n: g.sections.length, hs: g.sections.map((x) => x.h), text: g.sections.map((x) => x.body).join(" ") })));
  const ng = secs.find((g) => /Nginx/.test(g.title)), tc = secs.find((g) => /Tomcat/.test(g.title));
  ok("PDF 업로드 → 글자 추출·제목 단위 문단 분리", ng && ng.n >= 3 && ng.text.includes("cat domain.crt chain.crt > fullchain.pem") && ng.hs.some((h) => /설정 검사와 재시작/.test(h)), ng ? `${ng.code} ${ng.title} · 문단 ${ng.n}개 · 제목 ${ng.hs.join(" | ")}` : pmsg);
  ok("한글 CID 글꼴 PDF(내장 CMap 필요)도 읽기", tc && tc.text.includes("keytool -importkeystore") && tc.text.includes("키스토어"), tc ? `${tc.code} · ${tc.text.slice(0, 60)}` : pmsg);
  ok("스캔 이미지 PDF는 사유와 함께 거절", pmsg.includes("스캔본_예시.pdf: 글자를 읽을 수 없는 PDF"), pmsg.split("실패:")[1] || "");
  await p.evaluate(() => { window.__mode = "good"; window.__prompts = []; });
  await p.fill("#qa-input", "Nginx 인증서 교체 후 설정 문법은 무슨 명령으로 검사해?"); await p.click("#qa-send"); await p.waitForTimeout(600);
  const pp = await p.evaluate(() => window.__prompts[0] || "");
  ok("PDF 가이드 문단이 답변 근거로 검색됨", new RegExp(`\\[${ng?.code}-\\d\\]`).test(pp) && pp.includes("nginx -t"), (pp.match(new RegExp(`\\[${ng?.code}-\\d\\][^\\n]*`)) || [""])[0]);
  // 가이드 종수 클릭 → 목록
  await p.click("[data-glist=builtin]"); await p.waitForTimeout(150);
  const bl = await p.textContent("#g-list");
  await p.click("[data-glist=uploaded]"); await p.waitForTimeout(150);
  const ul = await p.textContent("#g-list");
  ok("'기본 가이드 12종'·'업로드 N종' 클릭 시 목록 표시", (bl.match(/G\d{2}/g) || []).length >= 12 && bl.includes("DigiCert") && /U0\d/.test(ul) && !ul.includes("G01"), `기본 ${new Set(bl.match(/G\d{2}/g)).size}종 · 업로드 ${new Set(ul.match(/U\d{2}/g)).size}종`);
  await p.screenshot({ path: "/tmp/claude-0/-home-claude/92d09200-f21f-5312-b63e-f3817c0470d9/scratchpad/qa_pdf.png", fullPage: false });
  await p.click("[data-gdel]"); await p.waitForTimeout(400);
  const del = await p.textContent("#qa-guides");
  ok("가이드 삭제 → RAG에서 제외", del.includes("업로드 3종"), (del.match(/검색 문단 \d+개/) || [""])[0]);
  console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
  fs.writeFileSync(path.join(__dirname, "..", "results", "e2e_qa_rag.json"), JSON.stringify(R, null, 1));
  await b.close(); srv.close();
})();

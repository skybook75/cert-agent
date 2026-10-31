/* 인증서 관리 플랫폼 — 공용 로직 (브라우저·Node 공용, 외부 의존성 없음)
 * - 상태 판정(KST, 만료 시각 기준)
 * - 수집 승인 처리(신규·갱신본)
 * - 알림 Agent(D-30·D-7·만료, 중복 방지, 이메일 초안)
 * - QA Agent(RAG): 가드레일 → 인증서 언급 감지 → 가이드 검색(BM25) → 프롬프트 → 답변 검증
 */
(function (root) {
  "use strict";
  const KST_MS = 9 * 3600 * 1000;
  const DAY = 86400000;

  // ───────────── 상태 판정
  function kstDay(ms) { return Math.floor((ms + KST_MS) / DAY); }
  function statusOf(notBefore, notAfter, now) {
    const n = now ?? Date.now(), na = Date.parse(notAfter), nb = Date.parse(notBefore);
    const days = kstDay(na) - kstDay(n);
    if (na <= n) return { status: "만료", days };
    if (nb > n) return { status: "사용 전", days };
    if (days <= 7) return { status: "D-7", days };
    if (days <= 30) return { status: "D-30", days };
    return { status: "정상", days };
  }
  function kstDate(iso) {
    const d = new Date(Date.parse(iso) + KST_MS);
    return d.toISOString().slice(0, 10);
  }

  // ───────────── 수집 승인 (discoveries → certificates)
  // 반환: 실행할 쓰기 목록(ops). 저장소 종류와 무관하게 같은 규칙.
  // 분류 순서: 이미 관리 중(지문) → 관리대장(엑셀) 등록 건과 대조(일치·불일치) → 같은 CN 의 갱신본 → 신규
  function classifyDiscovery(disc, certs) {
    if (certs.some(c => c.fingerprint && c.fingerprint === disc.fingerprint)) return { kind: "기존", match: null, diffs: [] };
    const reg = certs.filter(c => !c.fingerprint && !c.replaced_by);
    const ds = normSerial(disc.serial_number), dcn = String(disc.subject_cn || "").toLowerCase();
    const m = reg.find(c => c.serial_number && normSerial(c.serial_number) === ds) || reg.find(c => String(c.subject_cn || "").toLowerCase() === dcn);
    if (m) { const diffs = compareRegistry(m, disc); return { kind: diffs.length ? "불일치" : "일치", match: m.id, diffs }; }
    const prev = certs
      .filter(c => c.fingerprint && !c.replaced_by && c.subject_cn === disc.subject_cn && Date.parse(c.not_after) < Date.parse(disc.not_after))
      .sort((a, b) => Date.parse(b.not_after) - Date.parse(a.not_after))[0];
    if (prev) return { kind: "갱신본", match: prev.id, diffs: [] };
    // 같은 이름의 인증서를 이미 관리 중인데 만료일이 같거나 더 이른 파일 → 이전 버전(백업·잔존 파일). 승인하면 중복이 되므로 반려 대상
    const newer = certs
      .filter(c => c.fingerprint && !c.replaced_by && String(c.subject_cn || "").toLowerCase() === dcn && Date.parse(c.not_after) >= Date.parse(disc.not_after))
      .sort((a, b) => Date.parse(b.not_after) - Date.parse(a.not_after))[0];
    if (newer) return { kind: "이전 버전", match: newer.id, diffs: [] };
    return { kind: "신규", match: null, diffs: [] };
  }
  function normSerial(s) { return String(s || "").replace(/[^0-9a-f]/gi, "").toUpperCase().replace(/^0+(?=.)/, ""); }
  function normIssuer(s) { return String(s || "").toLowerCase().replace(/[^0-9a-z가-힣]/g, ""); }
  // 관리대장 값 vs 수집 값 (변경 전·후)
  function compareRegistry(reg, disc) {
    const diffs = [];
    const ri = normIssuer(reg.issuer_cn), di = normIssuer(disc.issuer_cn);
    if (ri && !(di.includes(ri) || ri.includes(di))) diffs.push({ field: "issuer_cn", label: "발급기관", before: reg.issuer_cn, after: disc.issuer_cn });
    if (reg.not_after && kstDate(reg.not_after) !== kstDate(disc.not_after)) diffs.push({ field: "not_after", label: "만료일", before: kstDate(reg.not_after), after: kstDate(disc.not_after) });
    if (reg.serial_number && normSerial(reg.serial_number) !== normSerial(disc.serial_number)) diffs.push({ field: "serial_number", label: "일련번호", before: reg.serial_number, after: disc.serial_number });
    if (reg.subject_cn && reg.subject_cn !== disc.subject_cn) diffs.push({ field: "subject_cn", label: "인증서명", before: reg.subject_cn, after: disc.subject_cn });
    return diffs;
  }

  function approveOps(disc, certs, actor, nowIso, extra) {
    const ops = [];
    const cls = classifyDiscovery(disc, certs);
    const id = "c_" + disc.fingerprint.replace(/:/g, "").slice(0, 16).toLowerCase();
    const base = {
      id, fingerprint: disc.fingerprint, subject_cn: disc.subject_cn, subject_dn: disc.subject_dn,
      issuer_cn: disc.issuer_cn, issuer_dn: disc.issuer_dn, not_before: disc.not_before, not_after: disc.not_after,
      serial_number: disc.serial_number, public_key: disc.public_key, signature_algorithm: disc.signature_algorithm,
      cert_type: disc.cert_type, san: disc.san, locations: disc.locations || [],
      owner: (extra && extra.owner) || "", owner_email: (extra && extra.owner_email) || "", system: (extra && extra.system) || "",
      renewal_status: "미조치", approved_by: actor, approved_at: nowIso, replaces: null, replaced_by: null, source: "수집",
    };
    if (cls.kind === "이전 버전") throw new Error("이미 더 최신 인증서를 관리 중인 이전 버전입니다. 반려 후 파일 정리를 권장합니다");
    if (cls.kind === "기존") {
      ops.push({ op: "update", coll: "discoveries", id: disc.id, data: { status: "중복", decided_by: actor, decided_at: nowIso } });
      return { kind: cls.kind, ops };
    }
    if (cls.kind === "일치" || cls.kind === "불일치") {        // 관리대장 등록 건에 수집 메타정보 반영(담당자·업무는 대장 값 유지)
      const reg = certs.find(c => c.id === cls.match);
      const meta = {};
      for (const k of ["fingerprint", "subject_cn", "subject_dn", "issuer_cn", "issuer_dn", "not_before", "not_after", "serial_number",
        "public_key", "signature_algorithm", "cert_type", "san"]) meta[k] = base[k];
      Object.assign(meta, { locations: base.locations, verified_at: nowIso, verified_by: actor, approved_by: actor, approved_at: nowIso,
        source: "관리대장+수집", registry_diffs: cls.diffs });
      if (extra && extra.owner) meta.owner = extra.owner;
      if (extra && extra.system) meta.system = extra.system;
      ops.push({ op: "update", coll: "certificates", id: reg.id, data: meta });
      ops.push({ op: "update", coll: "discoveries", id: disc.id, data: { status: "승인", kind: cls.kind, cert_id: reg.id, diffs: cls.diffs, decided_by: actor, decided_at: nowIso } });
      return { kind: cls.kind, ops, diffs: cls.diffs };
    }
    if (cls.kind === "갱신본") {
      const old = certs.find(c => c.id === cls.match);
      Object.assign(base, { owner: base.owner || old.owner, owner_email: base.owner_email || old.owner_email,
        system: base.system || old.system, replaces: old.id });
      ops.push({ op: "update", coll: "certificates", id: old.id,
        data: { renewal_status: "완료", replaced_by: id, renewed_at: nowIso } });
      ops.push({ op: "set", coll: "renewals", id: "r_" + old.id + "_" + id, data: {
        old_cert_id: old.id, new_cert_id: id, subject_cn: old.subject_cn, old_not_after: old.not_after,
        new_not_after: disc.not_after, renewed_at: nowIso, handled_by: actor, note: "수집 Agent 갱신본 감지 → 승인" } });
      ops.push({ op: "closeAlerts", cert_id: old.id });
    }
    ops.push({ op: "set", coll: "certificates", id, data: base });
    ops.push({ op: "update", coll: "discoveries", id: disc.id, data: { status: "승인", kind: cls.kind, cert_id: id, decided_by: actor, decided_at: nowIso } });
    return { kind: cls.kind, ops };
  }

  // ───────────── 알림 Agent
  const LEVEL_RANK = { "D-30": 1, "D-7": 2, "만료": 3 };
  const LEVEL_CODE = { "D-30": "d30", "D-7": "d7", "만료": "exp" };   // 문서 ID 는 ASCII 만 허용
  function notifyOps(certs, notifications, now, nowIso) {
    const ops = [];
    for (const c of certs) {
      if (c.replaced_by || c.renewal_status === "완료") continue;
      const { status, days } = statusOf(c.not_before, c.not_after, now);
      if (!LEVEL_RANK[status]) continue;
      const nid = "n_" + c.id + "_" + LEVEL_CODE[status];
      if (notifications.some(n => n.id === nid)) continue;           // 같은 단계 알림은 1회만
      const due = kstDate(c.not_after);
      const title = status === "만료" ? `[만료] ${c.subject_cn} 인증서가 만료되었습니다`
        : `[${status}] ${c.subject_cn} 인증서 만료 ${days}일 전`;
      const body = `${c.subject_cn} (발급: ${c.issuer_cn}) 인증서의 만료일은 ${due} 입니다` +
        (status === "만료" ? `. 이미 만료되어 서비스 장애 위험이 있습니다.` : `. 남은 기간은 ${days}일입니다.`) +
        ` 갱신 상태: ${c.renewal_status}. 저장 위치: ${(c.locations || []).join(", ") || "-"}`;
      const email = [
        `받는 사람: ${c.owner_email || c.owner || "(담당자 미지정)"}`,
        `제목: ${title}`,
        ``,
        `${c.owner || "담당자"}님, 안녕하세요.`,
        ``,
        body,
        `발급기관별 갱신 방법은 인증서 관리 플랫폼의 QA 챗봇이나 가이드를 참고해 주세요.`,
        `갱신을 마치면 새 인증서 파일을 지정 폴더에 두거나 플랫폼에 갱신 결과를 등록해 주세요.`,
      ].join("\n");
      ops.push({ op: "set", coll: "notifications", id: nid, data: {
        cert_id: c.id, level: status, rank: LEVEL_RANK[status], title, body, email_draft: email,
        recipient: c.owner || "", created_at: nowIso, read: false, closed: false } });
    }
    return ops;
  }

  // ───────────── QA Agent (RAG)
  const QA_BLOCK = [
    [/(시스템|system)\s*(프롬프트|prompt)/i, "시스템 지시문은 공개하지 않습니다."],
    [/(이전|위|모든)\s*(지시|명령|규칙).{0,8}(무시|잊)|ignore (all|previous|the above)/i, "지시를 바꾸는 요청은 처리하지 않습니다."],
    [/(비밀번호|패스워드|password)\s*(가|는|를|좀|이)?\s*(뭐|무엇|알려|보여|출력|말해)/i, "비밀번호는 저장하지 않으며 알려드릴 수 없습니다."],
    [/개인키\s*(의)?\s*(내용|값|원문|파일)\s*(을|를|좀)?\s*(보여|출력|알려|보내)/i, "개인키는 저장하지 않으며 제공할 수 없습니다."],
    [/(인증서|데이터|목록|알림).{0,12}(삭제해|지워|수정해|변경해|바꿔)/i, "챗봇은 조회·안내만 합니다. 변경은 플랫폼 화면에서 권한자가 처리합니다."],
    [/<script|(;|--)\s*(drop|delete|update)\s/i, "처리할 수 없는 입력입니다."],
  ];
  function qaGuard(q) {
    if (!q || !q.trim()) return "질문을 입력해 주세요.";
    if (q.length > 300) return "질문은 300자 이하로 입력해 주세요.";
    for (const [re, msg] of QA_BLOCK) if (re.test(q)) return msg;
    return null;
  }

  // 토큰: 영문·숫자 단어 + 한글 2글자 묶음(조사 변화에 강함)
  function tokens(text) {
    const t = String(text).toLowerCase();
    const out = [];
    for (const w of t.match(/[a-z0-9][a-z0-9.\-_]*/g) || []) { out.push(w); if (w.includes(".")) out.push(...w.split(".").filter(Boolean)); }
    for (const run of t.match(/[가-힣]+/g) || []) {
      if (run.length === 1) out.push(run);
      for (let i = 0; i < run.length - 1; i++) out.push(run.slice(i, i + 2));
    }
    return out;
  }
  function buildIndex(guides) {
    const chunks = [];
    for (const g of guides) g.sections.forEach((s, i) => chunks.push({
      id: `${g.id}-${i + 1}`, doc: g.id, title: g.title, h: s.h, text: s.body, applies: g.applies, sources: g.sources,
      toks: tokens(`${g.title} ${g.title} ${s.h} ${s.h} ${s.body} ${g.applies.join(" ")}`) }));
    const df = new Map();
    for (const c of chunks) for (const t of new Set(c.toks)) df.set(t, (df.get(t) || 0) + 1);
    const avg = chunks.reduce((a, c) => a + c.toks.length, 0) / chunks.length;
    return { chunks, df, avg, N: chunks.length };
  }
  function search(index, query, opts) {
    const o = Object.assign({ k: 4, issuer: null, k1: 1.2, b: 0.75, issuerBoost: 1.5 }, opts || {});
    const q = tokens(query);
    const scored = index.chunks.map(c => {
      const tf = new Map();
      for (const t of c.toks) tf.set(t, (tf.get(t) || 0) + 1);
      let s = 0;
      for (const t of q) {
        const f = tf.get(t); if (!f) continue;
        const idf = Math.log(1 + (index.N - index.df.get(t) + 0.5) / (index.df.get(t) + 0.5));
        s += idf * (f * (o.k1 + 1)) / (f + o.k1 * (1 - o.b + o.b * c.toks.length / index.avg));
      }
      if (o.issuer && s > 0 && c.applies.some(a => o.issuer.toLowerCase().includes(a.toLowerCase()))) s *= o.issuerBoost;
      return { chunk: c, score: s };
    }).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
    return scored.slice(0, o.k);
  }

  // 인증서 갱신 질문인데 그 발급기관 전용 가이드가 없으면 공통 절차·사내 절차를 근거에 포함
  function withRenewalContext(index, question, hits, cert, k) {
    if (!cert || !/갱신|교체|재발급|renew/i.test(question)) return hits;
    const issuer = String(cert.issuer_cn || "").toLowerCase();
    const hasOwn = index.chunks.some(c => c.applies.some(a => a !== "공통" && issuer.includes(a.toLowerCase())));
    const want = hasOwn ? ["G11-1"] : ["G11-1", "G04-2"];
    const req = want.map(id => hits.find(h => h.chunk.id === id) || { chunk: index.chunks.find(c => c.id === id), score: 0 })
      .filter(h => h.chunk);
    // 공동인증서(전자서명용) 가이드는 질문·인증서가 그쪽일 때만 근거로 씀
    const certText = (question + " " + cert.subject_cn + " " + (cert.locations || []).join(" ")).toLowerCase();
    const offTopic = h => h.chunk.applies.includes("공동인증서") && !/공동|signcert|전자서명|개인/.test(certText);
    const others = hits.filter(h => !want.includes(h.chunk.id) && !offTopic(h)).slice(0, Math.max(k - req.length, 0));
    const rank = h => { const i = hits.indexOf(h); return i < 0 ? 99 : i; };
    const out = others.concat(req).sort((a, b) => rank(a) - rank(b));
    return out.slice(0, k);
  }

  // 질문에 나온 인증서 찾기 (CN·SAN·파일명 일부)
  function findCerts(q, certs) {
    const t = q.toLowerCase();
    return certs.filter(c => {
      const keys = [c.subject_cn, ...(String(c.san || "").split(/,\s*/)), ...(c.locations || [])]
        .filter(Boolean).map(s => s.toLowerCase().replace(/^\*\./, ""));
      return keys.some(k => k.length >= 4 && t.includes(k));
    }).slice(0, 3);
  }

  function certFacts(c, now) {
    const s = statusOf(c.not_before, c.not_after, now);
    const label = { "D-7": "D-7 구간(만료 7일 이내)", "D-30": "D-30 구간(만료 30일 이내)", "만료": "만료됨", "정상": "정상(만료 31일 이상 남음)", "사용 전": "사용 전" }[s.status];
    return `- ${c.subject_cn}: 발급기관 ${c.issuer_cn}, 만료일 ${kstDate(c.not_after)}(KST), 상태 ${label}` +
      (s.status === "만료" ? `, 만료 후 ${-s.days}일 경과` : `, 남은 일수 ${s.days}일`) + `, 갱신 상태 ${c.renewal_status}, 담당자 ${c.owner || "미지정"}, 저장 위치 ${(c.locations || []).join(", ") || "-"}`;
  }

  function buildPrompt(question, hits, certsMentioned, now) {
    const ctx = hits.map(h => `[${h.chunk.id}] ${h.chunk.title} > ${h.chunk.h}\n${h.chunk.text}`).join("\n\n");
    const facts = certsMentioned.length ? certsMentioned.map(c => certFacts(c, now)).join("\n") : "(질문에 언급된 등록 인증서 없음)";
    return [
      "당신은 사내 '인증서 관리 플랫폼'의 QA 챗봇입니다. 관리자의 질문에 아래 [가이드]와 [등록 인증서 정보]만 근거로 한국어로 답하세요.",
      "규칙:",
      "1. 가이드에 없는 절차·명령·수치·연락처는 만들지 마세요. 근거가 없으면 '등록된 가이드에 해당 내용이 없습니다'라고 답하고, 확인할 곳(발급기관 등)만 안내하세요.",
      "2. 문장마다 근거 가이드 번호를 [G02-1] 형식으로 붙이세요. 등록 인증서 정보에서 온 내용은 [인증서]로 표시하세요.",
      "3. 만료일·남은 일수·담당자는 [등록 인증서 정보]의 값을 그대로 쓰세요.",
      "4. 비밀번호·개인키 값은 어떤 경우에도 묻거나 출력하지 마세요.",
      "5. 절차는 번호 목록으로 5단계 이내, 전체 12줄 이내로 간결하게. 마크다운 기호(**, #)는 쓰지 말고 일반 텍스트로 쓰세요.",
      "6. 상태 'D-7 구간'은 만료까지 7일 이내라는 뜻이며 남은 일수와 별개입니다.",
      "",
      "[등록 인증서 정보]",
      facts,
      "",
      "[가이드]",
      ctx || "(검색된 가이드 없음)",
      "",
      `[질문] ${question}`,
    ].join("\n");
  }

  // 답변 검증: 인용 번호가 실제 제공한 근거인지, 근거 없는 답이면 표시
  function checkAnswer(answer, hits) {
    const cited = [...new Set((answer.match(/\[[A-Z]\d{2}-\d+\]/g) || []).map(s => s.slice(1, -1)))];
    const allowed = new Set(hits.map(h => h.chunk.id));
    const invalid = cited.filter(c => !allowed.has(c));
    const noBasis = /가이드에 (해당 내용이|없)/.test(answer);
    return { cited, invalid, grounded: invalid.length === 0 && (cited.length > 0 || noBasis || /\[인증서\]/.test(answer)), noBasis };
  }

  // 검색형 대체 답변 (LLM 미연결·실패 시)
  function extractiveAnswer(question, hits, certsMentioned, now) {
    const lines = [];
    if (certsMentioned.length) lines.push("등록 인증서 정보:", ...certsMentioned.map(c => certFacts(c, now)), "");
    if (!hits.length) lines.push("등록된 가이드에서 관련 내용을 찾지 못했습니다. 발급기관 안내를 확인해 주세요.");
    else lines.push("관련 가이드:", ...hits.slice(0, 2).map(h => `[${h.chunk.id}] ${h.chunk.title} > ${h.chunk.h}\n${h.chunk.text}`));
    return lines.join("\n");
  }

  // ───────────── 역할·권한 (서버 platform_server.py 의 PERMS 와 같은 표 — 일치 테스트로 확인)
  const ROLES = ["관리자", "담당자", "검토자", "승인자", "조회자"];
  const PERMS = {
    "collect.run": ["관리자"], "notify.run": ["관리자"], "import.excel": ["관리자"], "cert.manage": ["관리자"],
    "discovery.review": ["관리자", "검토자"], "discovery.decide": ["관리자", "승인자"],
    "cert.renewal": ["관리자", "담당자"], "deploy.request": ["관리자", "담당자"],
    "deploy.review": ["관리자", "검토자"], "deploy.approve": ["관리자", "승인자"], "deploy.rollback": ["관리자", "승인자"],
    "noti.read": ["관리자", "담당자"], "mail.send": ["관리자"], "users.manage": ["관리자"], "settings.manage": ["관리자"],
    "audit.view": ["관리자", "검토자", "승인자", "조회자"], "guide.manage": ["관리자"],
    "secret.manage": ["관리자", "담당자"],
  };
  const OWNER_SCOPED = ["cert.renewal", "deploy.request", "noti.read"];   // 담당자는 본인 담당 인증서에만
  function can(role, action) { return (PERMS[action] || []).includes(role); }
  function isOwner(user, cert) {
    if (!user || !cert) return false;
    return (!!cert.owner && cert.owner === user.name) || (!!cert.owner_email && !!user.email && cert.owner_email.toLowerCase() === user.email.toLowerCase());
  }
  // 조회 범위: 담당자는 본인 인증서와 그 알림·배포·갱신 이력만
  function visibleCerts(user, certs) { return user && user.role === "담당자" ? certs.filter(c => isOwner(user, c)) : certs; }

  // ───────────── 관리대장(엑셀) 등록 — SC-001
  const IMPORT_COLUMNS = [
    { key: "subject_cn", label: "인증서명(CN)", req: true, alias: ["인증서명", "cn", "도메인", "인증서", "subject"] },
    { key: "issuer_cn", label: "발급기관", req: true, alias: ["발급자", "issuer", "ca"] },
    { key: "not_after", label: "만료일", req: true, alias: ["만료일자", "유효기간 종료", "not_after", "expiry"] },
    { key: "owner", label: "관리담당자", req: true, alias: ["담당자", "owner"] },
    { key: "owner_email", label: "담당자 이메일", alias: ["이메일", "email", "메일"] },
    { key: "system", label: "업무시스템", alias: ["업무", "시스템", "system"] },
    { key: "serial_number", label: "일련번호", alias: ["시리얼", "serial", "serial_number"] },
    { key: "renewal_status", label: "갱신상태", alias: ["갱신 상태", "renewal"] },
  ];
  const RENEWAL_STATES = ["미조치", "예정", "진행 중", "완료"];
  const normHead = (s) => String(s ?? "").toLowerCase().replace(/[\s()_\-]/g, "");
  function excelDate(v) {
    if (v instanceof Date && !isNaN(v)) return v.toISOString().slice(0, 10);
    if (typeof v === "number" && v > 20000 && v < 80000) return new Date(Math.round((v - 25569) * DAY)).toISOString().slice(0, 10);
    const m = String(v ?? "").trim().match(/^(\d{4})[-./]?\s*(\d{1,2})[-./]?\s*(\d{1,2})\.?$/);
    if (!m) return null;
    const y = +m[1], mo = +m[2], d = +m[3], dt = new Date(Date.UTC(y, mo - 1, d));
    return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d ? dt.toISOString().slice(0, 10) : null;
  }
  // rows: 2차원 배열(첫 번째로 머리글이 3개 이상 맞는 행을 머리글로 사용)
  function validateImport(rows) {
    const errors = [], records = [];
    let hi = -1, map = {};
    for (let i = 0; i < Math.min(rows.length, 10); i++) {
      const m = {};
      (rows[i] || []).forEach((h, ci) => {
        const n = normHead(h);
        const col = IMPORT_COLUMNS.find(c => normHead(c.label) === n || normHead(c.key) === n || c.alias.some(a => normHead(a) === n));
        if (col && m[col.key] === undefined) m[col.key] = ci;
      });
      if (Object.keys(m).length >= 3) { hi = i; map = m; break; }
    }
    if (hi < 0) return { records, errors: [{ row: 1, col: "-", msg: "머리글 행을 찾지 못했습니다. 양식의 머리글(인증서명(CN)·발급기관·만료일·관리담당자)을 사용하세요." }], missing: IMPORT_COLUMNS.filter(c => c.req).map(c => c.label) };
    const missing = IMPORT_COLUMNS.filter(c => c.req && map[c.key] === undefined).map(c => c.label);
    if (missing.length) return { records, errors: [{ row: hi + 1, col: "-", msg: `필수 열이 없습니다: ${missing.join(", ")}` }], missing };
    const seen = new Map();
    for (let i = hi + 1; i < rows.length; i++) {
      const r = rows[i] || [];
      const get = (k) => (map[k] === undefined ? null : r[map[k]]);
      const str = (k) => { const v = get(k); return v === null || v === undefined ? "" : String(v).trim(); };
      if (IMPORT_COLUMNS.every(c => !str(c.key))) continue;            // 빈 행
      const rowNo = i + 1, rec = { row: rowNo };
      const err = (col, msg) => errors.push({ row: rowNo, col, msg });
      for (const c of IMPORT_COLUMNS) if (c.req && !str(c.key)) err(c.label, "필수 값이 비어 있습니다");
      rec.subject_cn = str("subject_cn"); rec.issuer_cn = str("issuer_cn"); rec.owner = str("owner");
      rec.owner_email = str("owner_email"); rec.system = str("system");
      rec.serial_number = str("serial_number") ? normSerial(str("serial_number")) : "";
      const d = str("not_after") ? excelDate(get("not_after")) : null;
      if (str("not_after") && !d) err("만료일", `날짜 형식이 아닙니다: ${str("not_after")}`);
      rec.not_after = d ? `${d}T23:59:59+09:00` : null;
      if (rec.owner_email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(rec.owner_email)) err("담당자 이메일", `이메일 형식이 아닙니다: ${rec.owner_email}`);
      const rs = str("renewal_status");
      if (rs && !RENEWAL_STATES.includes(rs)) err("갱신상태", `허용 값: ${RENEWAL_STATES.join("·")}`);
      rec.renewal_status = rs || "미조치";
      if (rec.subject_cn && rec.subject_cn.length > 200) err("인증서명(CN)", "200자 이하로 입력하세요");
      const key = rec.subject_cn.toLowerCase() + "|" + rec.serial_number;
      if (rec.subject_cn && seen.has(key)) err("인증서명(CN)", `${seen.get(key)}행과 중복입니다`); else seen.set(key, rowNo);
      if (!errors.some(e => e.row === rowNo)) records.push(rec);
    }
    return { records, errors, missing: [] };
  }
  function hashId(s) {   // FNV-1a 두 번 → 16자리
    let h1 = 0x811c9dc5, h2 = 0x01000193;
    for (const ch of unescape(encodeURIComponent(String(s)))) { const c = ch.charCodeAt(0); h1 = Math.imul(h1 ^ c, 16777619) >>> 0; h2 = Math.imul(h2 ^ c, 2246822519) >>> 0; }
    return h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0");
  }
  // 대장 행 → 인증서 목록(관리정보). 이미 있는 인증서(일련번호 또는 CN)는 관리정보만 갱신.
  function importOps(records, certs, actor, nowIso, fileName) {
    const ops = []; let created = 0, updated = 0;
    const act = certs.filter(c => !c.replaced_by);
    for (const r of records) {
      const m = (r.serial_number && act.find(c => normSerial(c.serial_number) === r.serial_number)) ||
        act.find(c => String(c.subject_cn).toLowerCase() === r.subject_cn.toLowerCase());
      if (m) {
        ops.push({ op: "update", coll: "certificates", id: m.id, data: { owner: r.owner, owner_email: r.owner_email, system: r.system,
          registered_from: "관리대장", registry_row: r.row, ...(m.fingerprint ? {} : { issuer_cn: r.issuer_cn, not_after: r.not_after, serial_number: r.serial_number }) } });
        updated++;
      } else {
        const id = "x_" + hashId(`${r.subject_cn.toLowerCase()}|${r.serial_number}|${r.issuer_cn}`);
        ops.push({ op: "set", coll: "certificates", id, data: { subject_cn: r.subject_cn, issuer_cn: r.issuer_cn, not_after: r.not_after, not_before: null,
          serial_number: r.serial_number, owner: r.owner, owner_email: r.owner_email, system: r.system, renewal_status: r.renewal_status,
          fingerprint: null, locations: [], source: "관리대장", registered_from: "관리대장", registry_row: r.row,
          approved_by: actor, approved_at: nowIso, replaces: null, replaced_by: null } });
        act.push({ id, subject_cn: r.subject_cn, serial_number: r.serial_number });
        created++;
      }
    }
    ops.push({ op: "set", coll: "import_runs", id: "imp_" + nowIso.replace(/\D/g, "").slice(0, 14) + "_" + hashId(fileName + records.length).slice(0, 4),
      data: { at: nowIso, by: actor, file: fileName || "", total: records.length, created, updated } });
    return { ops, created, updated };
  }
  // 대조 현황: 승인 대기 수집 결과 + 관리대장 등록 건
  function reconcile(certs, discoveries) {
    const rows = [], matched = new Set();
    for (const d of discoveries.filter(x => x.status === "대기")) {
      const cls = classifyDiscovery(d, certs);
      if (cls.match) matched.add(cls.match);
      rows.push({ status: cls.kind, disc: d, cert: certs.find(c => c.id === cls.match) || null, diffs: cls.diffs });
    }
    for (const c of certs.filter(c => c.registered_from === "관리대장" && !c.replaced_by)) {
      if (matched.has(c.id)) continue;
      rows.push({ status: c.fingerprint ? "확인 완료" : "미수집", disc: null, cert: c, diffs: c.registry_diffs || [] });
    }
    const count = {}; rows.forEach(r => (count[r.status] = (count[r.status] || 0) + 1));
    return { rows, count };
  }

  // ───────────── 4단계 배포 (개발 → 테스트 → 교육 → 운영) 승인·원복
  const STAGES = [{ code: "dev", name: "개발" }, { code: "test", name: "테스트" }, { code: "edu", name: "교육" }, { code: "prod", name: "운영" }];
  const envId = (stage, cn) => "env_" + stage + "_" + hashId(String(cn).toLowerCase());
  const depId = (certId, stage) => "dp_" + certId + "_" + stage;
  function deployState(cert, deployments) {
    return STAGES.map(s => ({ ...s, dep: deployments.find(d => d.id === depId(cert.id, s.code)) || null }));
  }
  function canRequestStage(cert, stageCode, deployments) {
    const i = STAGES.findIndex(s => s.code === stageCode); if (i < 0) return { ok: false, why: "알 수 없는 단계" };
    const cur = deployments.find(d => d.id === depId(cert.id, stageCode));
    if (cur && ["요청", "검토 통과", "배포 완료"].includes(cur.status)) return { ok: false, why: `${STAGES[i].name} 단계가 이미 ${cur.status} 상태입니다` };
    if (i > 0) { const prev = deployments.find(d => d.id === depId(cert.id, STAGES[i - 1].code));
      if (!prev || prev.status !== "배포 완료") return { ok: false, why: `${STAGES[i - 1].name} 단계 배포 완료 후 요청할 수 있습니다` }; }
    if (!cert.fingerprint) return { ok: false, why: "수집·검증되지 않은 대장 등록 건은 배포할 수 없습니다" };
    if (cert.replaced_by) return { ok: false, why: "이미 갱신본으로 교체된 인증서입니다" };
    return { ok: true };
  }
  // 검토자 자동 검증 항목
  function deployChecks(cert, certs, envs, stageCode, now) {
    const s = statusOf(cert.not_before, cert.not_after, now), out = [];
    const add = (item, ok, detail) => out.push({ item, ok, detail });
    add("유효기간", s.status !== "만료" && s.status !== "사용 전", `${kstDate(cert.not_before)} ~ ${kstDate(cert.not_after)} (${s.status})`);
    add("남은 기간 30일 초과", s.days > 30, `남은 ${s.days}일`);
    const alg = String(cert.signature_algorithm || "").toLowerCase();
    add("서명 알고리즘", alg && !/sha1|md5/.test(alg), cert.signature_algorithm || "-");
    const pk = String(cert.public_key || ""), bits = +(pk.match(/(\d{3,5})bit/) || [0, 0])[1];
    const keyOk = /^RSA/.test(pk) ? bits >= 2048 : /^EC/.test(pk) ? !/secp192|secp224/.test(pk) : false;
    add("공개키 강도", keyOk, pk || "-");
    const env = envs.find(e => e.id === envId(stageCode, cert.subject_cn));
    const cur = env && certs.find(c => c.id === env.cert_id);
    if (cur && cur.id !== cert.id) {
      const sans = (x) => String(x.san || x.subject_cn || "").split(/,\s*/).filter(Boolean).map(v => v.toLowerCase());
      const missing = sans(cur).filter(v => !sans(cert).includes(v));
      add("도메인 유지(현재 배포본 대비)", missing.length === 0, missing.length ? `빠진 도메인: ${missing.join(", ")}` : `현재 ${kstDate(cur.not_after)} 만료본과 도메인 동일`);
      add("만료일 연장", Date.parse(cert.not_after) > Date.parse(cur.not_after), `${kstDate(cur.not_after)} → ${kstDate(cert.not_after)}`);
    } else add("현재 배포본", true, "이 환경에 배포된 같은 이름의 인증서 없음(최초 배포)");
    add("인증서 파일 위치", (cert.locations || []).length > 0, (cert.locations || [])[0] || "파일 위치 없음");
    return out;
  }
  function requestDeployOps(cert, stageCode, actor, nowIso, note) {
    const st = STAGES.find(s => s.code === stageCode);
    return [{ op: "set", coll: "deployments", id: depId(cert.id, stageCode), data: { cert_id: cert.id, subject_cn: cert.subject_cn, stage: stageCode,
      stage_name: st.name, status: "요청", requested_by: actor, requested_at: nowIso, note: note || "", review: null, approved_by: null, approved_at: null } }];
  }
  function reviewDeployOps(dep, checks, pass, actor, nowIso, comment) {
    return [{ op: "update", coll: "deployments", id: dep.id, data: { status: pass ? "검토 통과" : "검토 반려",
      review: { checks, pass, comment: comment || "", reviewed_by: actor, reviewed_at: nowIso } } }];
  }
  function approveDeployOps(dep, envs, actor, nowIso) {
    const id = envId(dep.stage, dep.subject_cn), env = envs.find(e => e.id === id);
    const history = [...((env && env.history) || []), { cert_id: dep.cert_id, action: "배포", by: actor, at: nowIso }].slice(-20);
    return [
      { op: "set", coll: "environments", id, data: { stage: dep.stage, stage_name: dep.stage_name, subject_cn: dep.subject_cn, cert_id: dep.cert_id,
        prev_cert_id: env ? env.cert_id : null, history, updated_at: nowIso } },
      { op: "update", coll: "deployments", id: dep.id, data: { status: "배포 완료", approved_by: actor, approved_at: nowIso, prev_cert_id: env ? env.cert_id : null } },
    ];
  }
  function rollbackOps(dep, envs, actor, nowIso, reason) {
    const id = envId(dep.stage, dep.subject_cn), env = envs.find(e => e.id === id);
    if (!env || env.cert_id !== dep.cert_id) return { ok: false, why: "현재 배포본이 아니어서 원복할 수 없습니다", ops: [] };
    const history = [...(env.history || []), { cert_id: env.prev_cert_id, action: "원복", by: actor, at: nowIso, reason: reason || "" }].slice(-20);
    return { ok: true, ops: [
      { op: "set", coll: "environments", id, data: { ...env, id: undefined, cert_id: env.prev_cert_id || null, prev_cert_id: null, history, updated_at: nowIso } },
      { op: "update", coll: "deployments", id: dep.id, data: { status: "원복", rolled_back_by: actor, rolled_back_at: nowIso, rollback_reason: reason || "", restored_cert_id: env.prev_cert_id || null } },
    ] };
  }

  // ───────────── 보고서 — SC-003
  function renewalReport(certs, now) {
    const rows = certs.filter(c => !c.replaced_by).map(c => {
      const s = statusOf(c.not_before, c.not_after, now);
      const delayed = (s.status === "만료" || s.status === "D-7") && ["미조치", "예정"].includes(c.renewal_status || "미조치");
      return { c, status: s.status, days: s.days, renewal: c.renewal_status || "미조치", delayed };
    }).sort((a, b) => a.days - b.days);
    const byRenewal = {}; RENEWAL_STATES.forEach(k => (byRenewal[k] = 0));
    const byStatus = { "만료": 0, "D-7": 0, "D-30": 0, "정상": 0, "사용 전": 0 };
    rows.forEach(r => { byRenewal[r.renewal] = (byRenewal[r.renewal] || 0) + 1; byStatus[r.status]++; });
    return { rows, byRenewal, byStatus, delayed: rows.filter(r => r.delayed).length };
  }
  function duplicateReport(certs) {
    const multiLoc = certs.filter(c => (c.locations || []).length > 1).map(c => ({ type: "같은 인증서 여러 위치", subject_cn: c.subject_cn, count: c.locations.length, locations: c.locations, cert_ids: [c.id] }));
    const byCn = new Map();
    certs.filter(c => c.fingerprint && !c.replaced_by).forEach(c => { const k = String(c.subject_cn).toLowerCase(); byCn.set(k, [...(byCn.get(k) || []), c]); });
    const sameCn = [...byCn.values()].filter(v => v.length > 1).map(v => ({ type: "같은 이름 복수 인증서", subject_cn: v[0].subject_cn, count: v.length,
      locations: v.flatMap(c => c.locations || []), cert_ids: v.map(c => c.id) }));
    return [...multiLoc, ...sameCn];
  }


  // ───────────── 가이드 업로드 → RAG 구성
  const ISSUER_HINTS = [["DigiCert", /digicert|디지서트/i], ["Sectigo", /sectigo|comodo|섹티고/i], ["Let's Encrypt", /let'?s ?encrypt|certbot/i], ["GlobalSign", /globalsign|글로벌사인/i],
    ["Thawte", /thawte/i], ["GeoTrust", /geotrust/i], ["Entrust", /entrust/i], ["공동인증서", /공동인증서|npki|공인인증서/i], ["IIS", /\biis\b/i], ["Tomcat", /tomcat|톰캣/i], ["Nginx", /nginx/i], ["Apache", /apache/i]];
  function detectApplies(text) { const hit = ISSUER_HINTS.filter(([, re]) => re.test(text)).map(([n]) => n); return hit.length ? hit.slice(0, 5) : ["공통"]; }
  // 텍스트 → 제목 단위 문단(300~900자). 제목이 없으면 빈 줄 기준 문단을 묶는다.
  function chunkGuide(title, text, opts) {
    const o = Object.assign({ min: 300, max: 900 }, opts || {});
    const lines = String(text).replace(/\r\n?/g, "\n").replace(/\t/g, " ").split("\n");
    const blank = (i) => i < 0 || i >= lines.length || !lines[i].trim();
    const isHead = (i) => { const l = lines[i].trim(); if (/^#{1,4}\s+\S/.test(l)) return true;
      return blank(i - 1) && blank(i + 1) && l.length > 0 && l.length <= 40 && !/^[-*•·]|^\d+\)/.test(l) && !/[.。,:;!?)]$|다$|요$/.test(l); };
    const blocks = []; let cur = { h: "", lines: [] };
    lines.forEach((raw, i) => {
      const l = raw.trimEnd();
      if (isHead(i)) { if (cur.lines.join("").trim() || cur.h) blocks.push(cur); cur = { h: l.trim().replace(/^#{1,4}\s+/, ""), lines: [] }; }
      else cur.lines.push(l);
    });
    blocks.push(cur);
    const sections = [];
    for (const b of blocks) {
      const paras = b.lines.join("\n").split(/\n\s*\n/).map((x) => x.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
      let buf = "", part = 0;
      const flush = () => { if (buf.trim()) { sections.push({ h: (b.h || title) + (part ? ` (${part + 1})` : ""), body: buf.trim() }); part++; buf = ""; } };
      for (const p of paras) {
        if (p.length > o.max) {   // 긴 문단은 문장 단위로 자름
          for (const sent of p.split(/(?<=[.!?。다요])\s+/)) { if ((buf + " " + sent).length > o.max) flush(); buf += (buf ? " " : "") + sent; }
        } else { if ((buf + "\n" + p).length > o.max && buf.length >= o.min) flush(); buf += (buf ? "\n" : "") + p; }
      }
      flush();
      if (!paras.length && b.h && sections.length === 0) continue;
    }
    // 너무 짧은 조각은 앞 조각에 붙임
    const merged = [];
    for (const sct of sections) { const prev = merged[merged.length - 1]; if (prev && sct.body.length < 120 && prev.body.length + sct.body.length < o.max * 1.3) prev.body += "\n" + sct.h + ": " + sct.body; else merged.push(sct); }
    if (merged.length > 1 && merged[0].body.length < 120) { merged[1].body = merged[0].h + ": " + merged[0].body + "\n" + merged[1].body; merged.shift(); }
    return merged;
  }

  // ───────────── 답변 검증 AI
  // 규칙 검사: 인용 번호 유효성, 근거에 없는 숫자·명령어·날짜
  function verifyRules(answer, hits, certsMentioned, now) {
    const chk = checkAnswer(answer, hits);
    const ctx = hits.map((h) => h.chunk.text + " " + h.chunk.h + " " + h.chunk.title).join(" ") + " " + certsMentioned.map((c) => certFacts(c, now)).join(" ");
    const norm = (x) => x.toLowerCase().replace(/[\s,]/g, "");
    const ctxN = norm(ctx);
    const body = answer.replace(/\[[A-Z]\d{2}-\d+\]|\[인증서\]/g, " ");
    const nums = [...new Set((body.match(/\d{4}-\d{2}-\d{2}|\d+(\.\d+)?\s*(영업일|일|시간|분|초|개월|년|%|bit|비트)/g) || []))].filter((n) => !ctxN.includes(norm(n)));
    const cmds = [...new Set((body.match(/\b(?:openssl|certbot|keytool|nginx|systemctl|certutil|Import-PfxCertificate)(?:\s+[A-Za-z0-9_\-./:=@"'*]+)*/gi) || []).map((x) => x.trim()))]
      .filter((cmd) => { const words = cmd.split(/\s+/).filter((w) => w.startsWith("-")); return words.some((w) => !ctx.includes(w)); });
    return { cited: chk.cited, invalid: chk.invalid, grounded: chk.grounded, noBasis: chk.noBasis, numbersNotInContext: nums, commandsNotInContext: cmds };
  }
  function buildVerifyPrompt(question, hits, certsMentioned, answer, now) {
    const ctx = hits.map((h) => `[${h.chunk.id}] ${h.chunk.title} > ${h.chunk.h}\n${h.chunk.text}`).join("\n\n");
    const facts = certsMentioned.length ? certsMentioned.map((c) => certFacts(c, now)).join("\n") : "(없음)";
    return [
      "당신은 인증서 관리 플랫폼 QA 챗봇의 '답변 검증 AI'입니다. 챗봇과는 별개로, [근거]와 [등록 인증서 정보]만을 기준으로 [답변]을 엄격하게 평가합니다.",
      "평가 기준:",
      "- accuracy(0~100): 답변의 각 주장·절차·명령어·수치가 근거와 일치하는가. 근거에 없는 내용을 지어냈거나 근거와 다르면 크게 감점. 근거가 없을 때 '없다'고 정직하게 답했으면 높은 점수.",
      "- quality(0~100): 질문에 직접 답했는가, 필요한 단계가 빠지지 않았는가, 순서가 맞는가, 간결한가, 문장마다 근거 번호를 붙였는가.",
      "- verdict: accuracy와 quality가 모두 80 이상이면 '적합', 하나라도 60 미만이면 '부적합', 그 외 '주의'.",
      "- unsupported: 근거로 뒷받침되지 않는 주장(원문 일부를 짧게 인용). 없으면 빈 배열.",
      "- missing: 근거에 있는데 질문에 필요한데 빠진 내용. 없으면 빈 배열.",
      "- comment: 사용자에게 보여줄 피드백 2문장 이내(한국어, 존댓말).",
      "JSON 객체 하나만 출력하세요: {\"accuracy\":숫자,\"quality\":숫자,\"verdict\":\"적합|주의|부적합\",\"unsupported\":[문자열],\"missing\":[문자열],\"comment\":\"문자열\"}",
      "", "[등록 인증서 정보]", facts, "", "[근거]", ctx || "(검색된 근거 없음)", "", `[질문] ${question}`, "", "[답변]", answer,
    ].join("\n");
  }
  function normalizeVerdict(v, rules) {
    const n = (x) => Math.max(0, Math.min(100, Math.round(Number(x) || 0)));
    const out = { accuracy: n(v && v.accuracy), quality: n(v && v.quality), unsupported: Array.isArray(v && v.unsupported) ? v.unsupported.slice(0, 5).map(String) : [],
      missing: Array.isArray(v && v.missing) ? v.missing.slice(0, 5).map(String) : [], comment: String((v && v.comment) || "").slice(0, 400) };
    // 규칙 검사에서 잘못된 인용이 나오면 정확도 상한을 둔다
    if (rules && rules.invalid.length) out.accuracy = Math.min(out.accuracy, 59);
    out.verdict = out.accuracy >= 80 && out.quality >= 80 ? "적합" : out.accuracy < 60 || out.quality < 60 ? "부적합" : "주의";
    return out;
  }
  // LLM 없이 규칙만으로 판정 (검색형 답변·검증 AI 호출 실패 시)
  function ruleVerdict(rules, extractive) {
    if (extractive) return { accuracy: null, quality: null, verdict: "참고", unsupported: [], missing: [], comment: "검색형 답변은 가이드 원문 문단을 그대로 보여주므로 내용 정확도는 원문과 같습니다. 질문에 맞는 부분을 직접 확인해 주세요.", ruleOnly: true };
    const bad = rules.invalid.length + rules.numbersNotInContext.length + rules.commandsNotInContext.length;
    return { accuracy: null, quality: null, verdict: bad ? "주의" : "참고", unsupported: [...rules.numbersNotInContext, ...rules.commandsNotInContext], missing: [],
      comment: bad ? "근거에서 확인되지 않는 번호·수치·명령어가 있습니다. 표시된 항목을 가이드 원문과 대조해 주세요." : "규칙 검사에서는 문제가 없었습니다. 검증 AI 평가는 받지 못했습니다.", ruleOnly: true };
  }

  // ───────────── PFX 비밀번호 보관함: 적용 범위 (vault.py 와 같은 규칙)
  function vaultMatches(sec, rel) {
    rel = String(rel).replace(/\\/g, "/"); const v = sec.scope_value || "";
    if (sec.scope === "all") return true;
    if (sec.scope === "file") return rel === v;
    if (sec.scope === "folder") return rel === v || rel.startsWith(v + "/");
    return false;
  }
  function vaultCandidates(secrets, rel) {
    const rank = { file: 0, folder: 1, all: 2 };
    const hits = secrets.filter((s) => s.active !== false && !s.deleted && s.enc && vaultMatches(s, rel));
    hits.sort((a, b) => String(b.last_used_at || "").localeCompare(String(a.last_used_at || "")));
    return hits.map((s, i) => [s, i]).sort((a, b) => (rank[a[0].scope] ?? 3) - (rank[b[0].scope] ?? 3) || (b[0].scope_value || "").length - (a[0].scope_value || "").length || a[1] - b[1]).map((x) => x[0]);
  }
  function normRel(p) {
    p = String(p || "").replace(/\\/g, "/").trim().replace(/^\/+|\/+$/g, "");
    if (/^[~]/.test(p) || p.includes(":") || p.split("/").includes("..")) throw new Error("적용 범위는 스캔 폴더 기준 상대 경로로 입력하세요");
    return p;
  }
  const scopeText = (s) => ({ all: "전체 PFX", folder: `폴더 ${s.scope_value}/`, file: `파일 ${s.scope_value}` }[s.scope] || "-");

  const api = { vaultMatches, vaultCandidates, normRel, scopeText, withRenewalContext, statusOf, kstDate, classifyDiscovery, approveOps, notifyOps, qaGuard, tokens, buildIndex, search,
    findCerts, certFacts, buildPrompt, checkAnswer, extractiveAnswer, LEVEL_RANK,
    ROLES, PERMS, OWNER_SCOPED, can, isOwner, visibleCerts, IMPORT_COLUMNS, RENEWAL_STATES, validateImport, importOps, excelDate, normSerial, hashId, reconcile,
    STAGES, envId, depId, deployState, canRequestStage, deployChecks, requestDeployOps, reviewDeployOps, approveDeployOps, rollbackOps,
    renewalReport, duplicateReport, detectApplies, chunkGuide, verifyRules, buildVerifyPrompt, normalizeVerdict, ruleVerdict };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.CertCore = api;
})(typeof window !== "undefined" ? window : globalThis);

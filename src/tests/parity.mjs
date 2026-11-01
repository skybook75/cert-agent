// 규칙 일치 테스트: 화면(core.js)과 서버(platform_server.py·agents.py)가 같은 권한표·알림 규칙을 쓰는지
import { createRequire } from "module"; import { execFileSync } from "child_process"; import fs from "fs"; import path from "path";
const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const C = require(path.join(ROOT, "web/core.js"));
const R = []; const ok = (n, c, d = "") => { R.push({ name: n, pass: !!c, detail: d }); console.log(`${c ? "PASS" : "FAIL"} ${n} — ${d}`); };
const py = (code) => JSON.parse(execFileSync("python3", ["-c", `import sys,json;sys.path.insert(0,${JSON.stringify(ROOT)});${code}`]).toString());
const srv = py("import platform_server as p;print(json.dumps({'perms':p.PERMS,'roles':p.ROLES,'owner':sorted(p.OWNER_SCOPED)},ensure_ascii=False))");
ok("역할 5종 일치", JSON.stringify(srv.roles) === JSON.stringify(C.ROLES), C.ROLES.join("·"));
ok("작업별 권한표 일치", JSON.stringify(srv.perms) === JSON.stringify(C.PERMS), `${Object.keys(C.PERMS).length}개 작업`);
ok("담당 범위 작업 일치", JSON.stringify(srv.owner) === JSON.stringify([...C.OWNER_SCOPED].sort()), C.OWNER_SCOPED.join(","));
// 알림 규칙: 같은 인증서 집합(대장 등록 건 포함)에 대해 같은 알림 ID
const now = Date.now(), d = (n) => new Date(now + n * 86400000 + 3600000).toISOString().slice(0, 19) + "+00:00";
const certs = [["c_a", -3], ["c_b", 2], ["c_c", 20], ["c_d", 90], ["x_e", 10, null]].map(([id, n, nb]) => ({ id, subject_cn: id + ".ex", issuer_cn: "CA", not_before: nb === null ? null : d(-300), not_after: d(n), renewal_status: "미조치", owner: "o", locations: [] }));
const tmp = path.join(ROOT, "results", ".parity.db"); fs.rmSync(tmp, { force: true });
const pyIds = py(`import agents;s=agents.Store(${JSON.stringify(tmp)});[s.set('certificates',c['id'],c) for c in ${JSON.stringify(certs).replace(/null/g, "None")}];agents.notify(s);print(json.dumps(sorted(n['id'] for n in s.list('notifications'))))`);
const jsIds = C.notifyOps(certs, [], now, new Date().toISOString()).map((o) => o.id).sort();
ok("알림 규칙 일치(대장 등록 건 포함)", JSON.stringify(pyIds) === JSON.stringify(jsIds), jsIds.join(", "));
fs.rmSync(tmp, { force: true });
console.log(JSON.stringify({ pass: R.filter((x) => x.pass).length, total: R.length }));
fs.writeFileSync(path.join(ROOT, "results", "parity.json"), JSON.stringify(R, null, 1));

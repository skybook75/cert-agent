# 녹화 당시 실제 로그 원문 → 장면별 로그 창 이벤트 (문구 수정·요약·시각 조정 없음)
#   web01·was01: journalctl -o short-iso 출력 줄 그대로
#   platform: audit 테이블 행(psql -tA -F '|') 그대로, systemd 저널 줄 그대로
#   오케스트레이터·유지보수 AI: DB에 저장된 실행 기록(orch_runs.steps[], maint report.steps[])의 JSON 원문 그대로
import json, re
from datetime import datetime
from pathlib import Path

H = Path(__file__).parent; L = H / "logs"
ep = lambda s: datetime.fromisoformat(re.sub(r"\+0000$", "+00:00", s)).timestamp()
MK = json.loads((H / "rec2/marks.json").read_text())
mk = {(m["scene"], m["ctx"], m["label"]): m["t"] for m in MK["marks"]}
dump = lambda o: json.dumps(o, ensure_ascii=False)          # PostgreSQL jsonb::text 와 같은 형식(", " / ": ")

lines = []
for r in (L / "audit.txt").read_text().splitlines():
    lines.append((ep(r.split("|")[0]), r))
for r in (L / "agents.txt").read_text().splitlines() + (L / "platform_journal.txt").read_text().splitlines():
    lines.append((ep(r.split(" ")[0]), r))
o = json.loads((L / "orch.txt").read_text())
for i, s in enumerate(o["steps"]):
    lines.append((ep(o["started_at"]), f"orch_runs or_20261104204255451725 steps[{i}] {dump(s)}"))
mr = json.loads((L / "maint.txt").read_text())
for i, s in enumerate(mr["steps"]):
    lines.append((ep(s["at"]), f"maint rp_20261104204416637641 steps[{i}] {dump(s)}"))

def t0(audit_at, mark, lag=1.0): return ep(audit_at) - (mark - lag)
A = {"login": t0("2026-11-04T20:54:15+00:00", mk[("login", "admin", "dash")]),
     "ledger": t0("2026-11-04T20:34:19+00:00", mk[("ledger", "admin", "start")]),
     "targets": t0("2026-11-04T20:34:49+00:00", mk[("targets", "owner", "start")]),
     "approve": t0("2026-11-04T20:38:25+00:00", mk[("approve", "admin", "start")]),
     "vault": t0("2026-11-04T20:38:52+00:00", mk[("vault", "owner", "start")]),
     "deploy": t0("2026-11-04T20:39:24+00:00", mk[("deploy", "admin", "req_0_e")], 1.5),
     "apply": t0("2026-11-04T20:41:23+00:00", mk[("apply", "admin", "start")]),
     "orch": t0("2026-11-04T20:42:45+00:00", mk[("orch", "admin", "start")]),
     "qa": t0("2026-11-04T20:43:18+00:00", mk[("qa", "owner", "start")]),
     "maint": t0("2026-11-04T20:43:51+00:00", mk[("maint", "admin", "start")])}
END = {s: max(m["t"] for m in MK["marks"] if m["scene"] == s) + 3 for s in A}
TITLE = "tail -f  journalctl(web01·was01) · audit(PostgreSQL)"
ev = {}
for s, z in A.items():
    sel = [[e, x] for e, x in sorted(lines, key=lambda a: a[0]) if z - 30 <= e <= z + END[s]]
    title = TITLE + (" · orch_runs" if s == "orch" else " · maint" if s == "maint" else "")
    ev[s] = {"title": title, "t0": z, "lines": sel}
    print(s, len(sel))
(H / "events_raw.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")

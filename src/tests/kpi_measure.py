"""기획서 KPI 측정 (Task 2 정량 목표)

  KPI 1  인증서 처리 속도: 인증서 100개 기준 60초 이내  → 수집 Agent(스캔·파싱·분류·승인 대기 등록) + 알림 Agent 실측
  KPI 2  만료 임박 알림 자동화율: D-30·D-7 알림 커버리지 100%  → 실제 만료일 기준 기대 판정 vs Agent 알림 대조
  KPI 3  사용자 업무시간 단축률 50% 이상  → 기획서 기준(회당 약 1시간) vs 플랫폼 처리(자동 처리 실측 + 화면 확인 단계)

  python tests/kpi_measure.py <작업폴더>
"""
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import agents  # noqa: E402

KST = timezone(timedelta(hours=9))
W = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cert_kpi")
W.mkdir(parents=True, exist_ok=True)
subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixture.py"), str(W), "--bulk", "100"], check=True, capture_output=True)
expected = json.loads((W / "bulk_expected.json").read_text(encoding="utf-8"))
folder = W / "bulk"


def approve_all(store):   # 화면의 승인과 같은 결과(신규 인증서 등록)를 일괄로 만든다
    for d in store.list("discoveries"):
        if d["status"] != "대기":
            continue
        cid = "c_" + d["fingerprint"].replace(":", "")[:16].lower()
        store.set("certificates", cid, {k: d[k] for k in ("fingerprint", "subject_cn", "issuer_cn", "not_before", "not_after", "serial_number", "locations")}
                  | {"owner": "담당자", "renewal_status": "미조치", "replaced_by": None})
        store.update("discoveries", d["id"], {"status": "승인"})


# KPI 1: 3회 측정(매번 새 DB)
runs = []
for i in range(3):
    db = W / f"kpi{i}.db"
    db.unlink(missing_ok=True)
    s = agents.Store(agents.resolve_db(str(db)))
    t0 = time.perf_counter()
    r = agents.collect(s, folder, ["Test#1234"], [str(folder)])
    t1 = time.perf_counter()
    approve_all(s)
    t2 = time.perf_counter()
    n = agents.notify(s)
    t3 = time.perf_counter()
    runs.append({"collect_s": round(t1 - t0, 3), "notify_s": round(t3 - t2, 3), "total_agent_s": round((t1 - t0) + (t3 - t2), 3),
                 "files": r["files"], "pending": r["new_pending"], "failed": r["failed"], "alerts": n["created"]})
med = statistics.median(x["total_agent_s"] for x in runs)

# KPI 2: 기대 판정(실제 만료일, KST 날짜 기준) vs 알림
now = datetime.now(timezone.utc)
def level(na):
    na = datetime.fromisoformat(na)
    days = (na.astimezone(KST).date() - now.astimezone(KST).date()).days
    return "만료" if na <= now else "D-7" if days <= 7 else "D-30" if days <= 30 else None
exp = {e["cn"]: level(e["not_after"]) for e in expected}
s = agents.Store(agents.resolve_db(str(W / "kpi0.db")))
certs = {c["id"]: c["subject_cn"] for c in s.list("certificates")}
got = {}
for nn in s.list("notifications"):
    got[certs[nn["cert_id"]]] = nn["level"]
need = {cn: lv for cn, lv in exp.items() if lv}
hit = sum(1 for cn, lv in need.items() if got.get(cn) == lv)
false_alerts = [cn for cn in got if not exp.get(cn)]
by_level = {lv: sum(1 for v in need.values() if v == lv) for lv in ("D-30", "D-7", "만료")}
# 재실행 시 중복 알림 없음
dup = agents.notify(s)["created"]

# KPI 3: 업무시간 (기획서 기준값 사용)
baseline_min = 60.0                     # 기획서: 회당 약 1시간 (연 130건 × 1h = 130시간)
auto_s = med / 100                      # 인증서 1건당 Agent 자동 처리 시간(실측)
human_steps = ["대시보드 알림 확인", "수집 결과(대장 대조 포함) 검토", "승인 클릭"]
human_min = 5.0                         # 화면 확인·승인 시간 가정(1건당 5분, 사용자 실측 전 추정)
after_min = human_min + auto_s / 60
reduction = (baseline_min - after_min) / baseline_min * 100

res = {
    "measured_at": now.isoformat(timespec="seconds"),
    "kpi1": {"target": "인증서 100개 60초 이내", "runs": runs, "median_total_s": med, "pass": med < 60 and runs[0]["pending"] == 100},
    "kpi2": {"target": "D-30·D-7 알림 커버리지 100%", "expected": len(need), "hit": hit, "coverage_pct": round(hit / len(need) * 100, 1),
             "by_level": by_level, "false_alerts": len(false_alerts), "duplicate_on_rerun": dup, "pass": hit == len(need) and not false_alerts and dup == 0},
    "kpi3": {"target": "업무시간 50% 이상 단축", "baseline_min_per_case": baseline_min, "after_min_per_case": round(after_min, 2),
             "auto_s_per_case": round(auto_s, 4), "human_steps": human_steps, "human_min_assumed": human_min,
             "reduction_pct": round(reduction, 1), "annual_hours_before": 130, "annual_hours_after": round(130 * after_min / 60, 1),
             "note": "자동 처리 시간은 실측, 화면 확인 5분은 추정(실사용자 측정은 E2E 서비스 운영 단계에서 수행)", "pass": reduction >= 50},
}
(ROOT / "results" / "kpi.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: (v if k == "measured_at" else {kk: vv for kk, vv in v.items() if kk != "runs"}) for k, v in res.items()}, ensure_ascii=False, indent=1))
print("runs:", runs)

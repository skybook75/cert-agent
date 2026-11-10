"""AI 운영: 오케스트레이터(Supervisor) + 유지보수·개선 Agent + 자가 진단

  오케스트레이터  목표(자연어) → 계획(LLM 또는 규칙 템플릿) → 각 Agent 도구 실행 → 결과 검증 → 보고.
                  배포처럼 되돌리기 어려운 단계는 실행하지 않고 '사람 승인 대기'로 남긴다(Human-in-the-loop).
  유지보수 Agent  ① 자가 진단: 서버·화면 오류, 수집·배포 실패, 느린 API, 데이터 품질, Agent 미접속을 찾아 버그 리포트·개선 제안
                  ② 대화형 개선: 요청 → 원인 분석 → 수정안(파일 교체 블록) → 임시 사본에서 컴파일·문법·기동 테스트 → 통과 시에만 반영
                     → 결과 리포트(무엇을·왜·테스트 결과) · 백업으로 되돌리기. 실패하면 반영하지 않음(Self-Correction 최대 2회 재시도)
  CLI             python ops_agents.py orchestrate "일일 운영 점검" --db platform.db   (작업 스케줄러·systemd timer 용)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import agents  # noqa: E402
import certlib  # noqa: E402

now_iso = agents.now_iso
EDITABLE = ["web/index.html", "web/core.js", "agents.py", "platform_server.py", "ops_agents.py", "certlib.py"]
BACKUPS = BASE / "maint_backups"

# ───────────────────────────── 관측: 요청 시간·오류 (메모리 + DB)
PERF = defaultdict(lambda: deque(maxlen=200))     # path → 최근 처리 시간(ms)
_PERF_LOCK = threading.Lock()


def record_perf(path: str, ms: float):
    key = re.sub(r"/[A-Za-z0-9_\-.:@+~]{12,}", "/:id", path)
    with _PERF_LOCK:
        PERF[key].append(ms)


def perf_summary():
    out = []
    with _PERF_LOCK:
        for k, v in PERF.items():
            s = sorted(v)
            if s:
                out.append({"path": k, "n": len(s), "p50": round(s[len(s) // 2], 1), "p95": round(s[min(len(s) - 1, int(len(s) * .95))], 1)})
    return sorted(out, key=lambda x: -x["p95"])


def record_error(store, source: str, where: str, message: str, trace: str = ""):
    """source: server | client. 같은 오류는 한 문서로 묶어 횟수만 올린다."""
    sig = re.sub(r"\d+", "N", f"{source}|{where}|{message}")[:300]
    eid = "e_" + format(abs(hash(sig)) % (10 ** 12), "012d")
    cur = store.get("errors", eid)
    if cur:
        store.update("errors", eid, {"count": int(cur.get("count", 1)) + 1, "last_at": now_iso(), "trace": trace[-4000:] or cur.get("trace", "")})
    else:
        store.set("errors", eid, {"source": source, "where": where[:200], "message": message[:500], "trace": trace[-4000:],
                                  "count": 1, "first_at": now_iso(), "last_at": now_iso()})
    return eid


# ───────────────────────────── LLM (Claude) — 키 없으면 None
def llm(prompt: str, max_tokens=4000) -> str | None:
    mock = os.environ.get("CERT_LLM_MOCK")            # 테스트용: 프롬프트 키워드 → 응답 파일
    if mock:
        table = json.loads(Path(mock).read_text(encoding="utf-8"))
        for k, v in table.items():
            if k in prompt:
                return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return agents.ask_claude(prompt, timeout=180, max_tokens=max_tokens)
    except RuntimeError as e:
        record_error_soft("llm", str(e))
        return None


_SOFT = {}


def record_error_soft(kind, msg):
    _SOFT[kind] = {"message": msg[:400], "at": now_iso()}


def _json_block(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S) or re.search(r"(\{.*\}|\[.*\])", text, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


# ───────────────────────────── 자가 진단 (유지보수 Agent)
SEV = {"높음": 3, "중간": 2, "낮음": 1}


def _status(c, now):
    try:
        return certlib.status_of(c.get("not_before") or "1970-01-01T00:00:00+00:00", c["not_after"], now)
    except Exception:  # noqa: BLE001
        return ("?", 0)


def diagnose(store, now=None) -> list[dict]:
    """규칙 기반 탐지 → 찾은 것마다 {kind, severity, title, evidence, proposal, fix?}. 같은 문제는 같은 ID 로 갱신."""
    now = now or datetime.now(timezone.utc)
    F = []

    def add(fid, kind, sev, title, evidence, proposal, fix=None, link=None):
        F.append({"id": "f_" + re.sub(r"[^A-Za-z0-9_\-]", "_", fid)[:100], "kind": kind, "severity": sev, "title": title,
                  "evidence": evidence, "proposal": proposal, "fix": fix, "link": link})

    # 1) 서버·화면 오류 → 버그 리포트
    for e in sorted(store.list("errors"), key=lambda x: x.get("last_at", ""), reverse=True)[:20]:
        if e.get("resolved"):
            continue
        add("err_" + e["id"], "버그", "높음" if e["source"] == "server" else "중간",
            f"{'서버' if e['source'] == 'server' else '화면'} 오류: {e['message'][:80]}",
            f"{e['where']} · {e.get('count', 1)}회 · 최근 {e.get('last_at', '')[:16]}\n{(e.get('trace') or '')[-600:]}",
            "오류 위치의 코드를 분석해 수정안을 만들고 자동 테스트 후 반영합니다(유지보수 Agent에 '이 오류 고쳐줘').",
            {"type": "llm_patch", "request": f"다음 오류를 고쳐줘: {e['message']} ({e['where']})\n{(e.get('trace') or '')[-1500:]}", "error_id": e["id"]})
    # 2) 수집 실패 반복 (비밀번호·형식)
    runs = sorted(store.list("scan_runs"), key=lambda r: r.get("started_at", ""), reverse=True)[:10]
    fails = defaultdict(list)
    for r in runs:
        for f in r.get("failures", []) or []:
            fails[str(f.get("error", ""))[:60]].append(f.get("path") or f.get("location") or "")
    for err, paths in fails.items():
        if len(paths) >= 1 and ("비밀번호" in err or "password" in err.lower()):
            folder = os.path.dirname(paths[0]) if paths[0] else ""
            add("pfx_" + folder[-60:], "개선", "중간", f"PFX 비밀번호가 없어 수집 실패 {len(paths)}건",
                "\n".join(sorted(set(paths))[:5]), f"PFX 비밀번호 보관함에 '{folder}' 폴더 범위로 비밀번호를 등록하면 다음 수집부터 자동으로 열립니다.",
                None, "vault")
        elif len(paths) >= 2:
            add("cf_" + err, "운영", "낮음", f"수집 실패 반복: {err}", "\n".join(sorted(set(paths))[:5]), "실패 파일 형식·권한을 확인하세요(손상 파일은 제외 목록으로).")
    # 3) 서버 배포 실패·자동 원복
    for env in store.list("environments"):
        for host, a in (env.get("applied") or {}).items():
            if a.get("ok") is False:
                add(f"dep_{env['id']}_{host}", "운영", "높음", f"서버 배포 실패·자동 원복: {host} {env.get('stage_name')} {env.get('subject_cn')}",
                    a.get("detail", ""), "인증서와 서버 개인키가 짝이 맞는지 확인하세요. 키를 새로 만든 갱신이면 담당자가 키와 함께 설치해야 합니다.", None, "deploy")
    # 4) 서버 Agent 미접속
    for ag in store.list("agents"):
        seen = ag.get("last_seen")
        if not seen or datetime.fromisoformat(seen) < now - timedelta(hours=2):
            add("ag_" + ag["id"], "운영", "중간", f"서버 Agent 미접속: {ag.get('host')}", f"마지막 접속 {seen or '없음'}",
                "서버의 cert-agent 타이머(작업 스케줄러)와 플랫폼 접속(보안그룹·토큰)을 확인하세요.", None, "admin")
    # 5) 알림 발송 누락·담당자 이메일 누락
    certs = {c["id"]: c for c in store.list("certificates")}
    unsent = [n for n in store.list("notifications") if not n.get("closed") and n.get("mail_status") in (None, "실패")]
    smtp = agents.smtp_settings(store)
    if unsent and smtp.get("enabled"):
        add("mail_unsent", "운영", "중간", f"알림 메일 미발송·실패 {len(unsent)}건", "\n".join(n.get("title", "") for n in unsent[:5]),
            "미발송 알림을 지금 발송합니다(이메일·문자·푸시 채널 설정 기준).", {"type": "mail_resend", "ids": [n["id"] for n in unsent][:200]})
    noemail = [c for c in certs.values() if not c.get("replaced_by") and not c.get("owner_email")]
    if noemail:
        add("noemail", "데이터", "낮음", f"담당자 이메일 없는 인증서 {len(noemail)}건", ", ".join(c.get("subject_cn", "") for c in noemail[:8]),
            "인증서 상세에서 담당자 이메일을 채우면 만료 알림이 실제로 전달됩니다.", None, "certs")
    # 6) 알림 Agent 미실행 (D-30 이내인데 알림 없음)
    ids = {n.get("cert_id") for n in store.list("notifications")}
    miss = [c for c in certs.values() if not c.get("replaced_by") and c.get("not_after") and c.get("renewal_status") != "완료"
            and _status(c, now)[0] in ("D-30", "D-7", "만료") and c["id"] not in ids]
    if miss:
        add("notify_missing", "운영", "높음", f"만료 임박인데 알림이 없는 인증서 {len(miss)}건", ", ".join(c.get("subject_cn", "") for c in miss[:8]),
            "알림 Agent를 지금 실행해 알림을 만듭니다.", {"type": "run_notify"})
    # 7) 느린 API
    for p in perf_summary()[:5]:
        if p["n"] >= 5 and p["p95"] > 1500:
            add("slow_" + p["path"], "개선", "낮음", f"느린 API: {p['path']} p95 {p['p95']}ms", f"표본 {p['n']}건, p50 {p['p50']}ms",
                "조회 범위를 줄이거나 결과를 캐시하는 개선을 제안합니다.", {"type": "llm_patch", "request": f"{p['path']} 응답이 느리다(p95 {p['p95']}ms). 원인을 찾아 개선해줘"})
    # 8) 승인 대기 오래됨
    old = [d for d in store.list("discoveries") if d.get("status") == "대기" and d.get("found_at", now_iso()) < (now - timedelta(days=3)).isoformat()]
    if old:
        add("disc_old", "운영", "낮음", f"3일 넘게 승인 대기 중인 수집 {len(old)}건", ", ".join(d.get("subject_cn", "") for d in old[:6]),
            "수집 승인 탭에서 처리하세요(갱신본이면 승인 즉시 기존 인증서가 갱신 완료로 바뀝니다).", None, "disc")
    # 기존 상태(무시·해결) 유지하며 저장
    prev = {d["id"]: d for d in store.list("maint") if d.get("type") == "finding"}
    seen_ids = set()
    for f in F:
        seen_ids.add(f["id"])
        p = prev.get(f["id"])
        status = p.get("status") if p and p.get("status") in ("무시", "반영 완료") else "열림"
        store.set("maint", f["id"], {**f, "type": "finding", "status": status, "first_at": (p or {}).get("first_at", now_iso()), "last_at": now_iso()})
    for fid, p in prev.items():                       # 더 이상 탐지되지 않으면 해결 처리
        if fid not in seen_ids and p.get("status") == "열림":
            store.update("maint", fid, {"status": "해결됨", "resolved_at": now_iso()})
    return sorted([f for f in store.list("maint") if f.get("type") == "finding"], key=lambda f: (f.get("status") != "열림", -SEV.get(f.get("severity"), 0)))


def apply_fix(store, fid: str, actor: str) -> dict:
    f = store.get("maint", fid)
    if not f or f.get("type") != "finding":
        raise ValueError("없는 항목")
    fix = f.get("fix") or {}
    t = fix.get("type")
    if t == "run_notify":
        r = agents.notify(store)
        res = f"알림 Agent 실행: 새 알림 {r['created']}건"
    elif t == "mail_resend":
        r = agents.mail_notifications(store, fix.get("ids"), actor)
        res = f"발송 {r['sent']}건, 실패 {len(r['failed'])}건" + (f" ({r['failed'][0]['error']})" if r["failed"] else "")
    elif t == "llm_patch":
        rep = improve(store, fix["request"], actor)
        res = rep.get("summary", "")
        if rep.get("status") == "반영" and fix.get("error_id"):
            store.update("errors", fix["error_id"], {"resolved": True, "resolved_at": now_iso(), "report": rep["id"]})
    else:
        raise ValueError("자동 반영할 수 없는 항목입니다(제안 내용을 참고해 직접 처리)")
    store.update("maint", fid, {"status": "반영 완료", "fixed_at": now_iso(), "fixed_by": actor, "fix_result": res})
    return {"ok": True, "result": res}


# ───────────────────────────── 대화형 개선 (유지보수 Agent)
def _file_map():
    out = []
    for rel in EDITABLE:
        p = BASE / rel
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            heads = [ln.strip()[:90] for ln in txt.splitlines() if re.match(r"\s*(def |class |function |async function |// ─|# ─)", ln)][:80]
            out.append({"file": rel, "lines": txt.count("\n") + 1, "outline": heads})
    return out


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def gate_tests(root: Path) -> list[dict]:
    """반영 전 자동 테스트: 파이썬 컴파일 · JS 문법 · 임시 서버 기동 후 주요 화면/API 응답."""
    res = []
    for rel in ("agents.py", "platform_server.py", "ops_agents.py", "certlib.py", "vault.py"):
        p = root / rel
        if p.exists():
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)], capture_output=True, text=True)
            res.append({"test": f"파이썬 컴파일 {rel}", "ok": r.returncode == 0, "detail": r.stderr[-300:]})
    node = shutil.which("node")
    if node:
        for rel in ("web/core.js",):
            r = subprocess.run([node, "--check", str(root / rel)], capture_output=True, text=True)
            res.append({"test": f"JS 문법 {rel}", "ok": r.returncode == 0, "detail": r.stderr[-300:]})
        html = (root / "web/index.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S)
        tmp = root / "_inline_check.mjs"
        tmp.write_text("\n;\n".join(scripts), encoding="utf-8")
        r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
        tmp.unlink()
        res.append({"test": "JS 문법 web/index.html(내장 스크립트)", "ok": r.returncode == 0, "detail": r.stderr[-300:]})
    if all(x["ok"] for x in res):
        port = _free_port()
        db = root / "_gate.db"
        p = subprocess.Popen([sys.executable, str(root / "platform_server.py"), "--db", str(db), "--port", str(port), "--no-browser",
                              "--vault-key", str(root / "_gate.key"), "--deploy", str(root / "_gate_deploy")], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env={k: v for k, v in os.environ.items() if k not in ("CERT_TEST_DB", "CERT_DB")})   # 임시 사본은 운영 DB와 분리된 파일 DB
        try:
            ok_all = True
            for path, want in (("/", b"tabs"), ("/core.js", b"function"), ("/api/health", b"setup_needed")):
                body, err = b"", ""
                for _ in range(30):
                    try:
                        body = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).read()
                        break
                    except Exception as e:  # noqa: BLE001
                        err = str(e)
                        time.sleep(0.3)
                ok = want in body
                ok_all &= ok
                res.append({"test": f"임시 서버 기동 후 GET {path}", "ok": ok, "detail": "" if ok else err[:200]})
        finally:
            p.kill()
            stderr = p.communicate()[1].decode(errors="ignore")[-400:] if p.stderr else ""
        if not ok_all and stderr:
            res.append({"test": "서버 오류 출력", "ok": False, "detail": stderr})
    return res


def _apply_edits(root: Path, edits: list[dict]) -> list[str]:
    errs = []
    for e in edits:
        rel = str(e.get("file", ""))
        if rel not in EDITABLE:
            errs.append(f"수정 허용 목록 밖 파일: {rel}")
            continue
        p = root / rel
        txt = p.read_text(encoding="utf-8")
        old, new = e.get("old", ""), e.get("new", "")
        if not old or txt.count(old) != 1:
            errs.append(f"{rel}: 바꿀 원문이 {'없음' if not old or txt.count(old) == 0 else '여러 곳'} — {old[:60]!r}")
            continue
        p.write_text(txt.replace(old, new, 1), encoding="utf-8")
    return errs


BEFORE_RESTART = []      # 플랫폼 서버가 등록: 재시작 전 로그인 세션 보존 등


def _restart_soon():
    """반영 후 프로세스 재시작(같은 인자로 재실행). 로그인 세션은 보존해 사용자가 다시 로그인하지 않게 한다."""
    def go():
        time.sleep(1.0)
        for f in BEFORE_RESTART:
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    if os.environ.get("CERT_NO_RESTART") != "1":
        threading.Thread(target=go, daemon=True).start()


def _edit_brief(edits):
    """수정안의 실제 변경 내용(바뀐 부분만)을 기계적으로 요약 — LLM 요약과 실제 수정이 어긋나지 않게 화면·로그에 함께 표시."""
    B = " <>\"'`(){}[],;:"
    out = []
    for e in edits:
        a, b = e.get("old", ""), e.get("new", "")
        i = 0
        while i < min(len(a), len(b)) and a[i] == b[i]:
            i += 1
        j = 0
        while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
            j += 1
        while i > 0 and a[i - 1] not in B:                       # 단어 경계까지 넓혀 읽기 쉽게
            i -= 1
        while j > 0 and a[len(a) - j] not in B:
            j -= 1
        out.append(f"'{a[i:len(a) - j][:40]}' → '{b[i:len(b) - j][:40]}'")
    return f"{len(edits)}곳: " + ", ".join(out)


def _mlog(msg):                                    # 유지보수 AI 실행 로그(journald, 영문)
    print(f"maint: {msg}", flush=True)


def _en_test(name):
    return (name.replace("파이썬 컴파일", "py_compile").replace("JS 문법", "js_syntax").replace("(내장 스크립트)", " (inline script)")
            .replace("임시 서버 기동 후", "smoke: temp server").encode("ascii", "replace").decode())


def improve(store, request: str, actor: str) -> dict:
    """요청 → (LLM) 분석·수정안 → 사본에서 테스트 → 통과 시 반영. 결과 리포트 저장."""
    rid = "rp_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    _mlog(f"request id={rid} chars={len(request)}")
    rep = {"type": "report", "request": request[:2000], "actor": actor, "at": now_iso(), "steps": [], "status": "진행"}
    step = lambda name, detail="", ok=True: rep["steps"].append({"step": name, "detail": str(detail)[:3000], "ok": ok, "at": now_iso()})

    fm = _file_map()
    _mlog(f"plan: analyze -> select files -> patch -> test on temp copy -> apply if green (editable={len(fm)})")
    step("Plan", f"요청 분석 → 관련 파일 선택 → 수정안 작성 → 임시 사본 테스트 → 통과 시 반영. 수정 가능 파일 {len(fm)}개")
    pick = _json_block(llm("너는 인증서 관리 플랫폼의 유지보수 Agent다. 사용자 요청을 해결하려면 어떤 파일을 봐야 하는지 고른다.\n"
                           f"요청: {request}\n파일 목록(개요): {json.dumps(fm, ensure_ascii=False)[:12000]}\n"
                           '응답은 JSON만: {"analysis": "원인/의도 분석 2~3문장", "files": ["web/index.html"]}', 800))
    if not pick:
        rep.update(status="보류", summary="생성형 AI API 키가 없어 코드 수정은 할 수 없습니다. 자가 진단의 규칙형 조치(알림 재실행·재발송 등)는 동작합니다.")
        step("LLM 확인", "ANTHROPIC_API_KEY 미설정 → 코드 수정 보류", False)
        _mlog("llm: no API key -> code change skipped")
        store.set("maint", rid, rep)
        return dict(rep, id=rid)
    files = [f for f in pick.get("files", []) if f in EDITABLE][:3] or ["web/index.html"]
    step("분석", pick.get("analysis", ""))
    _mlog(f"llm: analysis done -> files={','.join(files)}")
    feedback = ""
    for attempt in range(1, 4):                        # 최초 + Self-Correction 2회
        src = {f: (BASE / f).read_text(encoding="utf-8") for f in files}
        prompt = ("너는 인증서 관리 플랫폼(파이썬 표준 http.server + SQLite, 바닐라 JS 단일 페이지)의 유지보수 Agent다.\n"
                  f"요청: {request}\n분석: {pick.get('analysis', '')}\n"
                  + (f"이전 수정안이 테스트에서 실패했다. 실패 내용: {feedback}\n반드시 고쳐서 다시 제시하라.\n" if feedback else "")
                  + "규칙: 요청한 것만 최소한으로 수정(요청에 없는 다른 문구·기능은 바꾸지 말 것). 보안 검사(권한·CSRF·감사 로그)를 약하게 만들지 말 것. 비밀번호·키를 코드에 넣지 말 것.\n"
                  "summary 에는 edits 를 하나도 빠짐없이, 그 개수와 같게 설명할 것.\n"
                  "각 수정은 원문에서 정확히 한 번 나오는 old 문자열을 new 로 바꾸는 방식. old 는 공백까지 원문 그대로 복사.\n"
                  '응답은 JSON만: {"summary": "무엇을 왜 바꿨는지 2~3문장", "edits": [{"file": "...", "old": "...", "new": "..."}]}\n\n'
                  + "\n\n".join(f"=== {f} ===\n{t}" for f, t in src.items()))
        plan = _json_block(llm(prompt, 8000))
        if not plan or not plan.get("edits"):
            step(f"수정안 {attempt}차", "수정안을 만들지 못함", False)
            feedback = "JSON 형식의 edits 가 없었다"
            continue
        _mlog(f"patch: attempt={attempt} " + " ".join(f"{e.get('file')}(-{len(e.get('old', ''))}c/+{len(e.get('new', ''))}c)" for e in plan["edits"]))
        step(f"수정안 {attempt}차", plan.get("summary", "") + "\n" + "\n".join(f"- {e.get('file')}: {len(e.get('old', ''))}자 → {len(e.get('new', ''))}자" for e in plan["edits"]))
        work = Path(tempfile.mkdtemp(prefix="maint_"))
        try:
            for item in BASE.iterdir():
                if item.name in ("maint_backups", "__pycache__") or item.suffix == ".db":
                    continue
                if item.is_dir():
                    shutil.copytree(item, work / item.name, symlinks=True, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.db"))
                else:
                    shutil.copy2(item, work / item.name)
            errs = _apply_edits(work, plan["edits"])
            if errs:
                feedback = "; ".join(errs)
                step(f"적용 검사 {attempt}차", feedback, False)
                continue
            tests = gate_tests(work)
            rep["tests"] = tests
            passed = all(t["ok"] for t in tests)
            for t in tests:
                _mlog(f"gate: {'PASS' if t['ok'] else 'FAIL'} {_en_test(t['test'])}")
            _mlog(f"gate: {sum(t['ok'] for t in tests)}/{len(tests)} passed")
            step(f"자동 테스트 {attempt}차", "\n".join(f"{'✓' if t['ok'] else '✗'} {t['test']} {t['detail'][:120]}" for t in tests), passed)
            if not passed:
                feedback = "; ".join(f"{t['test']}: {t['detail'][:200]}" for t in tests if not t["ok"])
                continue
            # 반영: 원본 백업 → 교체
            bdir = BACKUPS / rid
            bdir.mkdir(parents=True, exist_ok=True)
            changed = sorted({e["file"] for e in plan["edits"]})
            diffs = []
            for rel in changed:
                (bdir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BASE / rel, bdir / rel)
                import difflib
                diffs.append("".join(difflib.unified_diff((BASE / rel).read_text(encoding="utf-8").splitlines(True),
                                                          (work / rel).read_text(encoding="utf-8").splitlines(True), f"a/{rel}", f"b/{rel}", n=2))[:6000])
                shutil.copy2(work / rel, BASE / rel)
            rep.update(status="반영", summary=plan.get("summary", "") + "\n실제 변경 " + _edit_brief(plan["edits"]), files=changed, diff="\n".join(diffs), backup=str(bdir), attempts=attempt)
            step("반영", f"{', '.join(changed)} 교체 · 백업 {bdir.name} · 서버 재시작")
            _mlog(f"apply: {','.join(changed)} backup={bdir.name} -> restarting service")
            store.set("maint", rid, rep)
            store.set("audit", "a_maint_" + rid, {"at": now_iso(), "actor": f"유지보수 Agent(요청 {actor})", "action": "코드 개선 반영",
                                                   "detail": f"{request[:120]} → {', '.join(changed)} (테스트 {len(tests)}개 통과)"})
            _restart_soon()
            return dict(rep, id=rid)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    rep.update(status="미반영", summary="자동 테스트를 통과하는 수정안을 만들지 못해 반영하지 않았습니다(서비스 영향 없음).")
    store.set("maint", rid, rep)
    return dict(rep, id=rid)


def rollback(store, rid: str, actor: str) -> dict:
    rep = store.get("maint", rid)
    if not rep or rep.get("status") != "반영":
        raise ValueError("되돌릴 수 있는 반영 리포트가 아닙니다")
    bdir = Path(rep["backup"])
    for rel in rep.get("files", []):
        shutil.copy2(bdir / rel, BASE / rel)
    store.update("maint", rid, {"status": "되돌림", "rolled_back_at": now_iso(), "rolled_back_by": actor})
    store.set("audit", "a_maint_rb_" + rid, {"at": now_iso(), "actor": actor, "action": "코드 개선 되돌림", "detail": rid})
    _restart_soon()
    return {"ok": True}


def chat(store, message: str, actor: str) -> dict:
    """유지보수 Agent 대화: 의도 → (진단 | 조치 | 코드 개선 | 질의응답)."""
    m = message.strip()
    hist = store.get("maint", "chat_log") or {"type": "chat", "messages": []}
    hist["messages"] = (hist.get("messages") or [])[-40:] + [{"role": "user", "text": m[:2000], "at": now_iso(), "by": actor}]
    reply, extra = "", {}
    if re.search(r"진단|점검|문제|버그 찾|찾아|살펴", m) and not re.search(r"고쳐|수정|바꿔|추가|개선해", m):
        fs = [f for f in diagnose(store) if f.get("status") == "열림"]
        reply = (f"자가 진단 결과 열린 항목 {len(fs)}건입니다.\n" + "\n".join(f"· [{f['severity']}·{f['kind']}] {f['title']}" for f in fs[:8])
                 + ("\n자동 반영 가능한 항목은 오른쪽 목록의 '반영' 버튼으로 처리할 수 있습니다." if fs else "\n특별한 문제가 없습니다."))
        extra = {"findings": len(fs)}
    elif re.search(r"되돌려|원복|롤백", m):
        reps = [r for r in store.list("maint") if r.get("type") == "report" and r.get("status") == "반영"]
        if reps:
            last = sorted(reps, key=lambda r: r["at"])[-1]
            rollback(store, last["id"], actor)
            reply = f"가장 최근 반영({last['request'][:60]})을 백업으로 되돌렸습니다. 서버를 다시 시작합니다."
        else:
            reply = "되돌릴 반영 이력이 없습니다."
    elif re.search(r"알림.*(다시|재)?\s*(보내|발송)|재발송", m):
        r = agents.mail_notifications(store, None, actor)
        reply = f"미발송 알림을 발송했습니다: {r['sent']}건 성공, {len(r['failed'])}건 실패."
    elif re.search(r"고쳐|수정|바꿔|추가|개선|만들어|넣어|버그", m):
        rep = improve(store, m, actor)
        reply = {"반영": f"반영했습니다. {rep.get('summary', '')}\n테스트 {sum(t['ok'] for t in rep.get('tests', []))}/{len(rep.get('tests', []))} 통과 · 변경 파일 {', '.join(rep.get('files', []))} · 서버 재시작 후 적용됩니다. 문제가 있으면 '되돌려줘'라고 하세요.",
                 "미반영": rep.get("summary", ""), "보류": rep.get("summary", "")}.get(rep["status"], rep.get("summary", ""))
        extra = {"report": rep["id"], "status": rep["status"]}
    else:
        ans = llm("너는 인증서 관리 플랫폼의 유지보수 Agent다. 운영자의 질문에 한국어로 짧고 정확하게 답한다. 모르면 모른다고 한다.\n"
                  f"최근 자가 진단: {json.dumps([{k: f[k] for k in ('severity', 'title')} for f in diagnose(store)[:6]], ensure_ascii=False)}\n"
                  f"성능: {json.dumps(perf_summary()[:5], ensure_ascii=False)}\n질문: {m}", 900)
        reply = ans or ("저는 '진단해줘', '알림 다시 보내줘', '○○ 기능을 바꿔줘/버그 고쳐줘', '되돌려줘' 같은 요청을 처리합니다. "
                        "자유 질의응답과 코드 수정은 생성형 AI API 키가 설정되면 동작합니다.")
    hist["messages"].append({"role": "agent", "text": reply, "at": now_iso(), **extra})
    store.set("maint", "chat_log", hist)
    return {"reply": reply, **extra}


# ───────────────────────────── 오케스트레이터 (Supervisor)
AGENT_DEFS = [
    {"id": "collector", "name": "수집 Agent", "desc": "서버 Agent·폴더 스캔으로 인증서 발견·파싱·업로드"},
    {"id": "verifier", "name": "대조·검증 Agent", "desc": "관리대장과 수집 결과 대조, 갱신본·이전 버전 판별"},
    {"id": "notifier", "name": "알림 Agent", "desc": "D-30·D-7·만료 알림 생성, 이메일·문자·푸시 발송"},
    {"id": "deployer", "name": "배포 Agent", "desc": "4단계 승인 배포, 서버 적용·검사·자동 원복"},
    {"id": "qa", "name": "QA Agent", "desc": "가이드 RAG 기반 질의응답·답변 검증"},
    {"id": "maint", "name": "유지보수 AI", "desc": "자가 진단·버그 리포트·개선 반영"},
]
TOOLS = {
    "collector.status": ("collector", "서버 Agent 접속·최근 수집 현황 확인"),
    "verifier.pending": ("verifier", "승인 대기 수집 결과 분류 요약"),
    "notifier.run": ("notifier", "만료 알림 생성"),
    "notifier.send": ("notifier", "미발송 알림 발송"),
    "deployer.plan": ("deployer", "갱신됐지만 운영 미배포인 인증서 찾기 → 배포 요청 제안(사람 승인 필요)"),
    "deployer.status": ("deployer", "서버 적용 결과·실패 확인"),
    "qa.brief": ("qa", "만료 임박 인증서별 갱신 가이드 요약"),
    "maint.diagnose": ("maint", "유지보수 AI 자가 진단(버그·운영·데이터 품질)"),
    "report.summary": ("supervisor", "전체 상태 집계·보고"),
}
TEMPLATES = [
    (r"일일|매일|아침|전체 점검|운영 점검|daily", ["collector.status", "verifier.pending", "notifier.run", "notifier.send", "deployer.status", "maint.diagnose", "report.summary"]),
    (r"만료|갱신|임박|이번 ?주", ["notifier.run", "qa.brief", "deployer.plan", "notifier.send", "maint.diagnose", "report.summary"]),
    (r"배포", ["deployer.status", "deployer.plan", "maint.diagnose", "report.summary"]),
    (r"수집|서버", ["collector.status", "verifier.pending", "maint.diagnose", "report.summary"]),
    (r"진단|버그|오류|장애", ["maint.diagnose", "deployer.status", "report.summary"]),
    (r"알림|메일", ["notifier.run", "notifier.send", "maint.diagnose", "report.summary"]),
]


def _plan(goal: str) -> tuple[list[str], str]:
    """정해진 운영 점검 절차(규칙)로 계획한다. 같은 목표면 항상 같은 순서 — 감사·재현 가능, 지연·비용 없음.
    AI(LLM)는 계획에 쓰지 않고, 마지막 단계의 유지보수 AI(자가 진단)와 개별 Agent 안에서만 쓴다."""
    for pat, steps in TEMPLATES:
        if re.search(pat, goal, re.I):
            return steps, "정해진 점검 절차"
    return TEMPLATES[0][1], "정해진 점검 절차(일일 운영 점검)"


def _run_tool(store, tool: str, ctx: dict, actor: str) -> dict:
    now = datetime.now(timezone.utc)
    certs = [c for c in store.list("certificates") if not c.get("replaced_by")]
    if tool == "collector.status":
        ags = store.list("agents")
        stale = [a["host"] for a in ags if not a.get("last_seen") or datetime.fromisoformat(a["last_seen"]) < now - timedelta(hours=2)]
        runs = sorted(store.list("scan_runs"), key=lambda r: r.get("started_at", ""), reverse=True)
        last = runs[0] if runs else {}
        return {"ok": not stale, "summary": f"서버 Agent {len(ags)}대(미접속 {len(stale)}{': ' + ', '.join(stale) if stale else ''}) · 최근 수집 {last.get('started_at', '-')[:16]} 파일 {last.get('files', 0)}개 실패 {last.get('failed', 0)}"}
    if tool == "verifier.pending":
        pend = [d for d in store.list("discoveries") if d.get("status") == "대기"]
        kinds = defaultdict(int)
        for d in pend:
            kinds[d.get("kind") or "미분류"] += 1
        return {"ok": True, "summary": f"승인 대기 {len(pend)}건" + (f" ({', '.join(f'{k} {v}' for k, v in kinds.items())})" if pend else "") + (" → 수집 승인 탭에서 처리 필요" if pend else ""), "needs_human": bool(pend)}
    if tool == "notifier.run":
        r = agents.notify(store)
        return {"ok": True, "summary": f"새 알림 {r['created']}건 생성"}
    if tool == "notifier.send":
        cfg = agents.smtp_settings(store)
        if not cfg.get("enabled"):
            return {"ok": True, "summary": "이메일 채널 미설정 → 발송 생략(관리 탭에서 설정)", "skipped": True}
        r = agents.mail_notifications(store, None, actor)
        return {"ok": not r["failed"], "summary": f"발송 {r['sent']}건 · 실패 {len(r['failed'])}건" + (f" ({r['failed'][0]['error'][:80]})" if r["failed"] else "")}
    if tool == "deployer.plan":
        envs = store.list("environments")
        prod = {e.get("subject_cn"): e for e in envs if e.get("stage") == "prod"}
        todo = [c for c in certs if c.get("renewed_at") or c.get("source") == "갱신본"]
        todo = [c for c in todo if prod.get(c.get("subject_cn"), {}).get("cert_id") != c["id"]]
        ctx["deploy_candidates"] = [c["subject_cn"] for c in todo]
        return {"ok": True, "summary": (f"운영 미배포 갱신 인증서 {len(todo)}건: {', '.join(c['subject_cn'] for c in todo[:6])} → 담당자 배포 요청·검토·승인 필요(자동 실행 안 함)"
                                        if todo else "운영 미배포 갱신 인증서 없음"), "needs_human": bool(todo)}
    if tool == "deployer.status":
        bad = [(e.get("subject_cn"), h, a.get("detail", "")[:80]) for e in store.list("environments") for h, a in (e.get("applied") or {}).items() if a.get("ok") is False]
        okn = sum(1 for e in store.list("environments") for a in (e.get("applied") or {}).values() if a.get("ok"))
        return {"ok": not bad, "summary": f"서버 적용 성공 {okn}건 · 실패·원복 {len(bad)}건" + (f": {bad[0][0]}@{bad[0][1]} {bad[0][2]}" if bad else "")}
    if tool == "qa.brief":
        urgent = sorted([(c, _status(c, now)) for c in certs if c.get("not_after")], key=lambda x: x[1][1])
        urgent = [(c, s) for c, s in urgent if s[0] in ("D-7", "D-30", "만료") and c.get("renewal_status") != "완료"][:5]
        lines = []
        for c, s in urgent:
            brand = c.get("issuer_cn", "")
            lines.append(f"{c['subject_cn']}({s[0]}, {s[1]}일, {brand[:30]})")
        ans = llm("인증서 관리자에게 아래 만료 임박 인증서들의 갱신 우선순위와 발급기관별 핵심 절차를 5줄 이내로 요약하라.\n" + "\n".join(lines), 600) if lines else None
        return {"ok": True, "summary": (ans.strip()[:600] if ans else ("만료 임박: " + "; ".join(lines) + " → QA 챗봇에서 발급기관별 갱신 절차 확인" if lines else "만료 임박 인증서 없음"))}
    if tool == "maint.diagnose":
        fs = [f for f in diagnose(store) if f.get("status") == "열림"]
        hi = [f for f in fs if f.get("severity") == "높음"]
        return {"ok": not hi, "summary": f"열린 항목 {len(fs)}건(높음 {len(hi)})" + (": " + "; ".join(f["title"][:50] for f in hi[:3]) if hi else "")}
    if tool == "report.summary":
        cnt = defaultdict(int)
        for c in certs:
            if c.get("not_after"):
                cnt[_status(c, now)[0]] += 1
        return {"ok": True, "summary": "인증서 " + ", ".join(f"{k} {cnt[k]}" for k in ("만료", "D-7", "D-30", "정상") if cnt[k] or k == "정상")}
    return {"ok": False, "summary": "알 수 없는 도구"}


def orchestrate(store, goal: str, actor: str = "오케스트레이터") -> dict:
    t0 = time.time()
    steps, why = _plan(goal)
    run = {"goal": goal[:300], "actor": actor, "started_at": now_iso(), "plan": steps, "plan_note": why, "steps": []}
    ctx = {}
    for tool in steps:
        st = time.time()
        try:
            r = _run_tool(store, tool, ctx, actor)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "summary": f"실행 오류: {e}"[:300]}
            record_error(store, "server", f"orchestrate:{tool}", str(e), traceback.format_exc())
        run["steps"].append({"tool": tool, "agent": TOOLS[tool][0], "label": TOOLS[tool][1], "ok": r.get("ok", False),
                             "needs_human": r.get("needs_human", False), "skipped": r.get("skipped", False),
                             "summary": r.get("summary", ""), "ms": round((time.time() - st) * 1000)})
    bad = [s for s in run["steps"] if not s["ok"]]
    human = [s for s in run["steps"] if s["needs_human"]]
    run.update(finished_at=now_iso(), ms=round((time.time() - t0) * 1000), status="주의" if bad else "정상",
               result=("확인 필요: " + "; ".join(f"{s['label']} — {s['summary'][:60]}" for s in bad[:3]) if bad else "모든 단계 정상")
               + (f" · 사람 승인 필요 {len(human)}건" if human else ""))
    rid = "or_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    store.set("orch_runs", rid, run)
    store.set("audit", "a_orch_" + rid, {"at": now_iso(), "actor": actor, "action": "오케스트레이션 실행", "detail": f"{goal[:80]} → {run['status']} ({len(steps)}단계)"})
    return dict(run, id=rid)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["orchestrate", "diagnose"])
    ap.add_argument("goal", nargs="?", default="일일 운영 점검")
    ap.add_argument("--db", default=os.environ.get("CERT_DB") or str(BASE / "platform.db"))
    a = ap.parse_args()
    s = agents.Store(agents.resolve_db(a.db))
    out = orchestrate(s, a.goal, "오케스트레이터(정기 실행)") if a.cmd == "orchestrate" else diagnose(s)
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])

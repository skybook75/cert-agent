"""인증서 관리 플랫폼 — PC 실행용 저장소와 Agent.

  Store          SQLite 문서 저장소 (collection/id → JSON). 웹 화면과 Agent가 같은 데이터를 쓴다.
  collect()      수집 Agent: 승인된 폴더 스캔 → 새 인증서를 '승인 대기'로 등록 (자동 반영 안 함)
  notify()       알림 Agent: D-30·D-7·만료 알림 + 이메일 초안 (단계별 1회)
  ask_claude()   QA Agent의 LLM 호출 (Anthropic Messages API, 키는 환경변수 ANTHROPIC_API_KEY)

CLI (작업 스케줄러 등록용):
  python agents.py collect "C:\\...\\pem"      (PFX 비밀번호는 플랫폼 보관함에 등록된 것을 복호화해 사용)
  python agents.py notify
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import certlib  # noqa: E402  (v2 파싱·상태 판정 재사용)

COLLECTIONS = {"certificates", "discoveries", "renewals", "notifications", "scan_runs", "audit", "settings",
               "users", "deployments", "environments", "import_runs", "guides", "secrets", "agents",
               "errors", "maint", "orch_runs", "targets"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-.:@+~]{1,120}$")
KST = certlib.KST


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """스레드 안전: 웹 서버가 요청을 동시에 처리하므로 연결 하나를 잠금으로 보호한다."""
    def __init__(self, path):
        import threading
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT, id TEXT, data TEXT, updated_at TEXT, "
                          "PRIMARY KEY (coll, id))")
        self.conn.commit()

    @staticmethod
    def _check(coll, id_=None):
        if coll not in COLLECTIONS:
            raise ValueError(f"알 수 없는 컬렉션: {coll}")
        if id_ is not None and not ID_RE.match(id_):
            raise ValueError("문서 ID 형식 오류")

    def list(self, coll):
        self._check(coll)
        with self.lock:
            rows = self.conn.execute("SELECT id, data FROM docs WHERE coll=? ORDER BY id", (coll,)).fetchall()
        return [dict(json.loads(d), id=i) for i, d in rows]

    def get(self, coll, id_):
        self._check(coll, id_)
        with self.lock:
            r = self.conn.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, id_)).fetchone()
        return dict(json.loads(r[0]), id=id_) if r else None

    def set(self, coll, id_, data):
        self._check(coll, id_)
        body = {k: v for k, v in data.items() if k != "id"}
        raw = json.dumps(body, ensure_ascii=False)
        if len(raw.encode("utf-8")) > (1024 if coll == "guides" else 256) * 1024:
            raise ValueError("문서가 너무 큽니다")
        with self.lock:
            self.conn.execute("INSERT INTO docs VALUES (?,?,?,?) ON CONFLICT(coll,id) DO UPDATE SET data=excluded.data, "
                              "updated_at=excluded.updated_at", (coll, id_, raw, now_iso()))
            self.conn.commit()

    def update(self, coll, id_, data):
        with self.lock:
            cur = self.get(coll, id_) or {}
            cur.update(data)
            self.set(coll, id_, cur)

    def delete(self, coll, id_):
        self._check(coll, id_)
        with self.lock:
            self.conn.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, id_))
            self.conn.commit()


# ───────────────────────────── 수집 Agent
def _fp_id(fp):
    return fp.replace(":", "")[:16].lower()


def _open(store: Store, vault, f: Path, rel: str, extra_pw, secret_docs, res):
    """파일을 연다. PFX 는 보관함에서 적용 범위가 맞는 비밀번호를 메모리에서만 복호화해 순서대로 시도한다."""
    if f.suffix.lower() not in (".pfx", ".p12"):
        return certlib.load_certs(f, [])
    import vault as vault_mod
    cands = vault_mod.candidates(secret_docs, rel) if vault else []
    pws, owners = [], []
    for sec in cands:
        try:
            pws.append(vault.decrypt(sec["id"], sec["enc"]))
            owners.append(sec)
        except Exception:  # noqa: BLE001  (키 불일치·변조)
            res["vault_errors"] += 1
    pws += list(extra_pw)
    try:
        certs, used = certlib.load_certs_ex(f, pws)
    except certlib.CertError as e:
        if str(e) == "비밀번호 필요" and owners:
            raise certlib.CertError(f"비밀번호 불일치(보관함 {len(owners)}개 시도)") from None
        raise
    finally:
        pws.clear()                                          # 복호화한 비밀번호는 이 함수 밖으로 내보내지 않음
    if used is not None and used < len(owners):
        sec = owners[used]
        store.update("secrets", sec["id"], {"last_used_at": now_iso(), "last_used_file": rel, "use_count": int(sec.get("use_count") or 0) + 1,
                                            "last_result": "성공"})
        sec["last_used_at"] = now_iso()
        res["vault_used"] += 1
    return certs


class _Registrar:
    """수집 결과(인증서 객체 + 위치)를 승인 대기로 올리는 공통 규칙 — 로컬 폴더 수집과 서버 Agent 업로드가 함께 쓴다."""
    def __init__(self, store: Store, folder_label: str):
        self.store, self.folder = store, folder_label
        self.certs = store.list("certificates")
        self.by_fp = {c["fingerprint"]: c for c in self.certs}
        all_discs = store.list("discoveries")
        self.discs = {d["fingerprint"]: d for d in all_discs if d.get("status") == "대기"}
        self.rejected = {d["fingerprint"] for d in all_discs if d.get("status") == "반려"}   # 반려한 파일은 다시 올리지 않음
        self.started = now_iso()
        self.res = {"files": 0, "new_pending": 0, "known": 0, "failed": 0, "errors": [], "renewal_candidates": 0, "vault_used": 0,
                    "vault_errors": 0, "skipped_rejected": 0}

    def fail(self, loc, reason):
        self.res["failed"] += 1
        self.res["errors"].append({"file": loc, "reason": reason})

    def add(self, cert, loc, pem=None):
        store, res = self.store, self.res
        r = certlib.to_record(cert)
        fp = r["sha256_fingerprint"]
        if fp in self.by_fp:                                  # 이미 관리 중 → 위치만 추가
            c = self.by_fp[fp]
            if loc not in c.get("locations", []):
                store.update("certificates", c["id"], {"locations": c.get("locations", []) + [loc]})
                c.setdefault("locations", []).append(loc)
            res["known"] += 1
            return
        if fp in self.discs:                                  # 승인 대기 중 → 위치만 추가
            d = self.discs[fp]
            if loc not in d["locations"]:
                d["locations"].append(loc)
                store.update("discoveries", d["id"], {"locations": d["locations"]})
            res["known"] += 1
            return
        if fp in self.rejected:                               # 이전에 반려한 인증서 → 대기로 되살리지 않음
            res["skipped_rejected"] += 1
            return
        prev = sorted([x for x in self.certs if not x.get("replaced_by") and x["subject_cn"] == r["subject_cn"]
                       and x["not_after"] < r["not_after"]], key=lambda x: x["not_after"], reverse=True)
        newer = [x for x in self.certs if not x.get("replaced_by") and x.get("fingerprint") and x["subject_cn"].lower() == r["subject_cn"].lower()
                 and x["not_after"] >= r["not_after"]]
        kind = "갱신본" if prev else "이전 버전" if newer else "신규"
        res["renewal_candidates"] += kind == "갱신본"
        d = {"fingerprint": fp, **{k: v for k, v in r.items() if k != "sha256_fingerprint"},
             "locations": [loc], "kind": kind, "match_cert_id": prev[0]["id"] if prev else None,
             "status": "대기", "scanned_at": self.started, "folder": self.folder}
        if pem:
            d["pem"] = pem                                    # 공개 인증서(배포 Agent 가 서버에 설치할 때 사용). 개인키는 받지 않음
        d_id = "d_" + _fp_id(fp)
        store.set("discoveries", d_id, d)
        self.discs[fp] = dict(d, id=d_id)
        res["new_pending"] += 1

    def finish(self, actor, extra=None):
        res = self.res
        run_id = "run_" + self.started.replace(":", "").replace("-", "").replace("+", "")[:15] + (("_" + extra["host"]) if extra and extra.get("host") else "")
        self.store.set("scan_runs", re.sub(r"[^A-Za-z0-9_\-.]", "_", run_id)[:100], {"started_at": self.started, "folder": self.folder, **res, **(extra or {})})
        self.store.set("audit", re.sub(r"[^A-Za-z0-9_\-.]", "_", "a_" + run_id)[:100], {"at": self.started, "actor": actor, "action": "수집 실행",
                                                 "detail": f"{self.folder}: 파일 {res['files']}개, 승인 대기 {res['new_pending']}건, 실패 {res['failed']}건"})
        return res


def collect(store: Store, folder, passwords=(), allowed_roots=None, actor="수집 Agent", vault=None) -> dict:
    """스캔 결과를 승인 대기(discoveries)로 올린다. 이미 등록·대기 중인 인증서는 위치만 갱신.
    PFX 비밀번호는 보관함(vault)에 담당자가 등록한 것을 쓴다. passwords 는 CLI 일회성 입력용(저장 안 함)."""
    from cryptography.hazmat.primitives import serialization
    folder = Path(folder)
    certlib.check_root(folder, allowed_roots)
    reg = _Registrar(store, str(folder))
    secret_docs = store.list("secrets") if vault else []
    for f in certlib.iter_files(folder):
        reg.res["files"] += 1
        rel = f.relative_to(folder).as_posix()
        try:
            found = _open(store, vault, f, rel, passwords, secret_docs, reg.res)
        except certlib.CertError as e:
            reg.fail(rel, str(e))
            continue
        for c in found:
            reg.add(c, rel, c.public_bytes(serialization.Encoding.PEM).decode())
    return reg.finish(actor)


def ingest(store: Store, host: str, files: list, actor: str) -> dict:
    """서버 Agent 업로드: files = [{path, ders:[base64 DER], error?}] — 위치는 '호스트:경로'로 기록.
    인증서는 서버에서 다시 파싱해 Agent 가 보낸 메타정보를 그대로 믿지 않는다."""
    import base64
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    reg = _Registrar(store, f"{host} (서버 Agent)")
    for f in files[:5000]:
        loc = f"{host}:{str(f.get('path', ''))[:400]}"
        reg.res["files"] += 1
        if f.get("error"):
            reg.fail(loc, str(f["error"])[:200])
            continue
        ok = 0
        for b in (f.get("ders") or [])[:20]:
            try:
                c = x509.load_der_x509_certificate(base64.b64decode(b))
            except Exception:  # noqa: BLE001
                continue
            reg.add(c, loc, c.public_bytes(serialization.Encoding.PEM).decode())
            ok += 1
        if not ok:
            reg.fail(loc, "인증서를 읽지 못함")
        if f.get("secret_id"):
            sec = store.get("secrets", f["secret_id"]) if ID_RE.match(str(f["secret_id"])) else None
            if sec:
                store.update("secrets", sec["id"], {"last_used_at": now_iso(), "last_used_file": loc, "use_count": int(sec.get("use_count") or 0) + 1, "last_result": "성공"})
                reg.res["vault_used"] += 1
    return reg.finish(actor, {"host": host, "engine": "서버 Agent"})


# ───────────────────────────── 알림 Agent (core.js notifyOps 와 같은 규칙)
LEVEL_RANK = {"D-30": 1, "D-7": 2, "만료": 3}
LEVEL_CODE = {"D-30": "d30", "D-7": "d7", "만료": "exp"}   # 문서 ID 는 ASCII 만 허용


def notify(store: Store, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    existing = {n["id"] for n in store.list("notifications")}
    made = []
    for c in store.list("certificates"):
        if c.get("replaced_by") or c.get("renewal_status") == "완료" or not c.get("not_after"):
            continue
        # 관리대장(엑셀)으로만 등록된 건은 발급일이 없음 → 만료일 기준으로만 판정
        status, days = certlib.status_of(c.get("not_before") or "1970-01-01T00:00:00+00:00", c["not_after"], now)
        if status not in LEVEL_RANK:
            continue
        nid = f"n_{c['id']}_{LEVEL_CODE[status]}"
        if nid in existing:
            continue
        due = datetime.fromisoformat(c["not_after"]).astimezone(KST).strftime("%Y-%m-%d")
        title = (f"[만료] {c['subject_cn']} 인증서가 만료되었습니다" if status == "만료"
                 else f"[{status}] {c['subject_cn']} 인증서 만료 {days}일 전")
        body = (f"{c['subject_cn']} (발급: {c['issuer_cn']}) 인증서의 만료일은 {due} 입니다"
                + (". 이미 만료되어 서비스 장애 위험이 있습니다." if status == "만료" else f". 남은 기간은 {days}일입니다.")
                + f" 갱신 상태: {c.get('renewal_status', '미조치')}. 저장 위치: {', '.join(c.get('locations', [])) or '-'}")
        email = "\n".join([f"받는 사람: {c.get('owner_email') or c.get('owner') or '(담당자 미지정)'}", f"제목: {title}", "",
                           f"{c.get('owner') or '담당자'}님, 안녕하세요.", "", body,
                           "발급기관별 갱신 방법은 인증서 관리 플랫폼의 QA 챗봇이나 가이드를 참고해 주세요.",
                           "갱신을 마치면 새 인증서 파일을 지정 폴더에 두거나 플랫폼에 갱신 결과를 등록해 주세요."])
        store.set("notifications", nid, {"cert_id": c["id"], "level": status, "rank": LEVEL_RANK[status], "title": title,
                                         "body": body, "email_draft": email, "recipient": c.get("owner", ""),
                                         "created_at": now_iso(), "read": False, "closed": False})
        made.append(nid)
    if made:
        store.set("audit", "a_notify_" + now.strftime("%Y%m%d%H%M%S"),
                  {"at": now_iso(), "actor": "알림 Agent", "action": "알림 생성", "detail": f"{len(made)}건"})
    return {"created": len(made), "ids": made}


# ───────────────────────────── 이메일 발송 (SMTP) — 비밀번호는 환경변수 SMTP_PASSWORD 에만
def smtp_settings(store: Store) -> dict:
    return store.get("settings", "smtp") or {}


def send_mail(cfg: dict, to: str, subject: str, body: str, timeout=10):
    import smtplib
    import ssl
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid
    if not re.match(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$", to or ""):
        raise RuntimeError("받는 사람 이메일이 없거나 형식이 맞지 않습니다")
    if cfg.get("enabled") and cfg.get("provider") == "ses":      # AWS SES API (EC2 역할 권한, 키 저장 없음)
        import boto3
        boto3.client("ses", region_name=cfg.get("aws_region") or os.environ.get("AWS_REGION") or "ap-southeast-2").send_email(
            Source=cfg.get("from_addr"), Destination={"ToAddresses": [to]},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}})
        return
    if not cfg.get("enabled") or not cfg.get("host"):
        raise RuntimeError("SMTP 설정이 없습니다")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg.get("from_addr") or "cert-platform@localhost", to, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="cert-platform")
    msg.set_content(body)
    port, sec = int(cfg.get("port") or 25), cfg.get("security", "none")
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(cfg["host"], port, timeout=timeout, context=ctx) if sec == "ssl" else smtplib.SMTP(cfg["host"], port, timeout=timeout)
    try:
        if sec == "starttls":
            smtp.starttls(context=ctx)
        if cfg.get("username"):
            smtp.login(cfg["username"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send_sms(cfg: dict, phone: str, text: str):
    """AWS SNS 문자 발송 (EC2 역할 권한). 번호는 +82 형식으로 맞춘다."""
    import boto3
    num = re.sub(r"[^0-9+]", "", phone or "")
    if num.startswith("0"):
        num = "+82" + num[1:]
    if not re.match(r"^\+\d{9,15}$", num):
        raise RuntimeError("휴대폰 번호 형식 오류")
    boto3.client("sns", region_name=cfg.get("aws_region") or os.environ.get("AWS_REGION") or "ap-southeast-2").publish(
        PhoneNumber=num, Message=text[:300], MessageAttributes={"AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}})


def send_push(cfg: dict, title: str, text: str, priority="high"):
    """휴대폰 푸시 — ntfy(앱에서 구독한 주제로 전달). 주제 이름은 추측하기 어려운 값으로 설정."""
    topic = cfg.get("push_topic")
    if not topic:
        raise RuntimeError("푸시 주제 미설정")
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text[:1000].encode("utf-8"), method="POST",
                                 headers={"Title": "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?=", "Priority": priority, "Tags": "warning"})
    urllib.request.urlopen(req, timeout=10).read()


def channel_notify(store: Store, n: dict, cert: dict) -> dict:
    """문자·푸시 채널: 설정의 수신 번호(당직)·담당자 번호로 짧게 알린다."""
    cfg = smtp_settings(store)
    out = {}
    short = f"[인증서] {n.get('title', '')}"[:120]
    if cfg.get("sms_enabled"):
        nums = [x for x in [cert.get("owner_phone"), *(cfg.get("sms_to") or [])] if x]
        ok, err = 0, ""
        for num in dict.fromkeys(nums):
            try:
                send_sms(cfg, num, short)
                ok += 1
            except Exception as e:  # noqa: BLE001
                err = str(e)[:150]
        out["sms_status"], out["sms_error"] = ("발송" if ok else "실패"), (err or None)
    if cfg.get("push_topic"):
        try:
            send_push(cfg, "인증서 만료 알림", f"{n.get('title', '')}\n{n.get('body', '')[:300]}")
            out["push_status"], out["push_error"] = "발송", None
        except Exception as e:  # noqa: BLE001
            out["push_status"], out["push_error"] = "실패", str(e)[:150]
    return out


def mail_notifications(store: Store, ids=None, actor="알림 Agent") -> dict:
    """알림의 이메일 초안을 담당자 이메일로 발송. ids 가 없으면 아직 발송하지 않은 열린 알림 전체."""
    cfg = smtp_settings(store)
    certs = {c["id"]: c for c in store.list("certificates")}
    sent, failed = [], []
    for n in store.list("notifications"):
        if ids is not None and n["id"] not in ids:
            continue
        if ids is None and (n.get("closed") or n.get("mail_status") == "발송"):
            continue
        c = certs.get(n.get("cert_id"), {})
        to = c.get("owner_email", "")
        body = n.get("email_draft", "").split("\n", 3)[-1] if n.get("email_draft", "").startswith("받는 사람") else n.get("body", "")
        try:
            send_mail(cfg, to, n.get("title", "인증서 만료 알림"), body.lstrip("\n"))
            store.update("notifications", n["id"], {"mail_status": "발송", "mail_to": to, "mailed_at": now_iso(), "mail_error": None})
            sent.append(n["id"])
            ch = channel_notify(store, n, c)
            if ch:
                store.update("notifications", n["id"], ch)
        except Exception as e:   # noqa: BLE001  (발송 실패는 알림별로 기록하고 계속)
            store.update("notifications", n["id"], {"mail_status": "실패", "mail_to": to, "mail_error": str(e)[:200]})
            failed.append({"id": n["id"], "to": to, "error": str(e)[:200]})
    if sent or failed:
        store.set("audit", "a_mail_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
                  {"at": now_iso(), "actor": actor, "action": "이메일 발송", "detail": f"발송 {len(sent)}건, 실패 {len(failed)}건"})
    return {"sent": len(sent), "failed": failed}


# ───────────────────────────── QA Agent LLM 호출 (Claude)
LLM_STATE = {"model": None, "last_error": None}
MODEL_CANDIDATES = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-4-5", "claude-3-5-sonnet-latest"]


def ask_claude(prompt: str, model: str | None = None, timeout=120, max_tokens=900) -> str:
    """Claude Messages API. 모델이 계정에서 안 되면(400/404) 다음 후보로 자동 전환하고 성공한 모델을 기억한다."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no_key")
    cands = [m for m in [model, os.environ.get("CLAUDE_MODEL"), LLM_STATE["model"], *MODEL_CANDIDATES] if m]
    last = None
    for m in dict.fromkeys(cands):
        body = json.dumps({"model": m, "max_tokens": max_tokens, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            LLM_STATE.update(model=m, last_error=None)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "ignore")
            last = f"Claude API 오류 {e.code} ({m}): {detail}"
            LLM_STATE["last_error"] = last
            if e.code in (400, 404) and ("model" in detail.lower() or "not_found" in detail.lower()):
                continue                                   # 모델 미지원 → 다음 후보
            raise RuntimeError(last) from None
    raise RuntimeError(last or "Claude API 호출 실패")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "notify"])
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--db", default=str(BASE / "platform.db"))
    ap.add_argument("--password", action="append", default=[], help="일회성 PFX 비밀번호(저장 안 함). 평소에는 플랫폼 보관함에 등록한 비밀번호를 씀")
    ap.add_argument("--vault-key", help="보관함 키 파일 경로 (기본: %%APPDATA%%\\cert_platform\\vault.key)")
    ap.add_argument("--send", action="store_true", help="notify 후 발송하지 않은 알림을 SMTP 로 발송")
    a = ap.parse_args()
    s = Store(a.db)
    if a.cmd == "collect":
        import vault as vault_mod
        print(json.dumps(collect(s, a.folder, a.password, [a.folder], vault=vault_mod.Vault(a.vault_key)), ensure_ascii=False, indent=1))
    else:
        out = notify(s)
        if a.send:
            out["mail"] = mail_notifications(s)
        print(json.dumps(out, ensure_ascii=False))

"""배포 게이트 E2E (서버 API 직접 호출 — 화면을 거치지 않는 우회 시도까지 서버가 막는지 확인)

  python3 tests/e2e_deploy_gates.py      → results/e2e_deploy_gates.json

검증 항목
  1. 자동 점검: 남은 기간 30일 이하 · SHA-1 서명 · RSA 1024비트 · 만료일이 현재 배포본보다 짧은 갱신본 → 배포 요청 거부
  2. 단계 순서: 개발 완료 전 테스트·운영 요청 거부, 순서대로는 통과
  3. 검토 시 재점검: 요청 뒤 인증서가 점검 기준을 벗어나면 '검토 통과' 거부
  4. 승인 시 재확인: 앞 단계가 원복되면 다음 단계 승인 거부
  5. 직무 분리: 요청자 검토·검토자 승인 거부
  6. 서버 적용 실패 보고 → 담당자 알림 즉시 생성 + 메일 발송
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import agents  # noqa: E402
import certlib  # noqa: E402

PORT, SMTP = 8791, 2531
W = Path(tempfile.mkdtemp(prefix="gates_"))
R = []


def ok(name, cond, detail=""):
    R.append({"name": name, "pass": bool(cond), "detail": str(detail)[:300]})
    print(("PASS " if cond else "FAIL ") + name + " — " + str(detail)[:200])


# ── 시험용 인증서 (사설 CA 서명)
now = datetime.now(timezone.utc)
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gate Test CA")])


def make(cn, days, bits=2048, algo=hashes.SHA256(), start=-1):
    k = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])).issuer_name(ca_name)
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now + timedelta(days=start)).not_valid_after(now + timedelta(days=days))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False).sign(ca_key, algo))
    return c


store = agents.Store(W / "p.db")


def put(cert, loc):
    rec = certlib.to_record(cert)
    rec["fingerprint"] = rec.pop("sha256_fingerprint")
    cid = "x_" + rec["fingerprint"].replace(":", "").lower()[:16]
    from cryptography.hazmat.primitives.serialization import Encoding
    rec.update({"locations": [loc], "owner": "김담당", "owner_email": "owner@demo.test", "renewal_status": "미조치",
                "pem": cert.public_bytes(Encoding.PEM).decode()})
    store.set("certificates", cid, rec)
    return cid, rec


good, good_rec = put(make("shop.gate.test", 365), "/etc/nginx/ssl/shop/fullchain.pem")
short, _ = put(make("short.gate.test", 10), "/etc/nginx/ssl/short.pem")
def make_sha1(cn):
    """cryptography 는 SHA-1 서명을 만들지 않으므로 openssl 로 만든다(실제로 남아 있는 구형 인증서 재현)."""
    pem = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha1", "-nodes", "-days", "365", "-subj", f"/CN={cn}",
                          "-keyout", os.devnull], capture_output=True, check=True).stdout
    return x509.load_pem_x509_certificate(pem)


sha1, sha1_rec = put(make_sha1("sha1.gate.test"), "/etc/nginx/ssl/sha1.pem")
weak, weak_rec = put(make("weak.gate.test", 365, bits=1024), "/etc/nginx/ssl/weak.pem")
shorter, _ = put(make("shop.gate.test", 200), "/etc/nginx/ssl/shop/fullchain.pem")      # 같은 도메인, 만료일이 더 이른 '갱신본'
later, _ = put(make("later.gate.test", 365), "/etc/nginx/ssl/later.pem")
store.conn.commit() if hasattr(store, "conn") and hasattr(store.conn, "commit") else None

sink = subprocess.Popen([sys.executable, "tests/smtp_sink.py", str(SMTP), str(W / "mail.jsonl")], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
srv = subprocess.Popen([sys.executable, "platform_server.py", "--db", str(W / "p.db"), "--port", str(PORT), "--no-browser",
                        "--vault-key", str(W / "vault.key"), "--deploy", str(W / "deploy")], cwd=ROOT,
                       stdout=open(W / "srv.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}/"


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf, **(headers or {})}
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with self.op.open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:  # noqa: BLE001
                return e.code, {}

    def login(self, u, pw, new=None):
        s, _ = self.call("POST", "api/login", {"username": u, "password": pw})
        s2, me = self.call("GET", "api/me")
        self.csrf = me.get("csrf", "")
        if new:
            self.call("POST", "api/password", {"old": pw, "new": new})
        return s

    def act(self, action, ops):
        return self.call("POST", "api/action", {"action": action, "ops": ops, "summary": "e2e"})


def req_ops(cid, rec, stage):
    name = {"dev": "개발", "test": "테스트", "prod": "운영"}[stage]
    return [{"op": "set", "coll": "deployments", "id": f"dp_{cid}_{stage}", "data": {
        "cert_id": cid, "subject_cn": rec["subject_cn"], "stage": stage, "stage_name": name, "status": "요청", "requested_by": "김관리(관리자)",
        "requested_at": agents.now_iso(), "note": "", "review": None, "approved_by": None, "approved_at": None}}]


def review_ops(cid, stage, who, passed=True):
    return [{"op": "update", "coll": "deployments", "id": f"dp_{cid}_{stage}", "data": {
        "status": "검토 통과" if passed else "검토 반려",
        "review": {"checks": [], "pass": passed, "comment": "", "reviewed_by": who, "reviewed_at": agents.now_iso()}}}]


def env_id(stage, cn):
    out = subprocess.run(["node", "-e", f"const C=require('./web/core.js');console.log(C.envId({json.dumps(stage)},{json.dumps(cn)}))"],
                         cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def approve_ops(cid, rec, stage, who, prev=None):
    eid = env_id(stage, rec["subject_cn"])
    name = {"dev": "개발", "test": "테스트", "prod": "운영"}[stage]
    return [{"op": "set", "coll": "environments", "id": eid, "data": {"stage": stage, "stage_name": name, "subject_cn": rec["subject_cn"],
             "cert_id": cid, "prev_cert_id": prev, "history": [], "updated_at": agents.now_iso(), "apply_after": None}},
            {"op": "update", "coll": "deployments", "id": f"dp_{cid}_{stage}", "data": {"status": "배포 완료", "approved_by": who,
             "approved_at": agents.now_iso(), "prev_cert_id": prev, "apply_after": None}}]


try:
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE, timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    A = Client()
    A.call("POST", "api/setup", {"username": "admin", "name": "김관리", "email": "admin@demo.test", "password": "Admin2026a"})
    A.login("admin", "Admin2026a")
    for u, n, r in (("rev", "이검토", "검토자"), ("apr", "최승인", "승인자")):
        A.call("POST", "api/users", {"username": u, "name": n, "email": f"{u}@demo.test", "role": r, "password": "Temp2026a"})
    RV, AP = Client(), Client()
    RV.login("rev", "Temp2026a", "Review2026a")
    AP.login("apr", "Temp2026a", "Approve2026a")
    RV.login("rev", "Review2026a")
    AP.login("apr", "Approve2026a")
    REV, APR = "이검토(검토자)", "최승인(승인자)"

    # 1. 자동 점검으로 요청 거부
    s, b = A.act("deploy.request", req_ops(short, store.get("certificates", short), "dev"))
    ok("자동 점검: 남은 10일 인증서 → 배포 요청 거부", s == 409 and "남은 기간 30일 초과" in b.get("error", ""), f"{s} {b.get('error')}")
    s, b = A.act("deploy.request", req_ops(sha1, sha1_rec, "dev"))
    ok("자동 점검: SHA-1 서명 인증서 → 배포 요청 거부", s == 409 and "서명 알고리즘" in b.get("error", ""), f"{s} {b.get('error')}")
    s, b = A.act("deploy.request", req_ops(weak, weak_rec, "dev"))
    ok("자동 점검: RSA 1024비트 키 → 배포 요청 거부", s == 409 and "공개키 강도" in b.get("error", ""), f"{s} {b.get('error')}")

    # 2. 단계 순서
    s, b = A.act("deploy.request", req_ops(good, good_rec, "prod"))
    ok("단계 순서: 개발·테스트 없이 운영 요청 → 거부(API 직접 호출)", s == 409 and "단계 순서 위반" in b.get("error", ""), f"{s} {b.get('error')}")
    s, b = A.act("deploy.request", req_ops(good, good_rec, "test"))
    ok("단계 순서: 개발 완료 전 테스트 요청 → 거부", s == 409 and "개발 단계 배포 완료 후" in b.get("error", ""), f"{s} {b.get('error')}")

    # 5. 직무 분리 + 정상 흐름(개발)
    s, b = A.act("deploy.request", req_ops(good, good_rec, "dev"))
    ok("정상 인증서 개발 단계 요청 → 통과", s == 200, f"{s} {b}")
    s, b = A.act("deploy.review", review_ops(good, "dev", "김관리(관리자)"))
    ok("직무 분리: 요청자가 검토 → 거부", s == 403, f"{s} {b.get('error')}")
    s, b = RV.act("deploy.review", review_ops(good, "dev", REV))
    ok("검토자 검토 통과(서버 재점검 통과)", s == 200, f"{s} {b}")
    s, b = RV.act("deploy.approve", approve_ops(good, good_rec, "dev", REV))
    ok("권한: 검토자 계정은 승인 불가", s == 403, f"{s} {b.get('error')}")
    s, b = AP.act("deploy.approve", approve_ops(good, good_rec, "dev", APR))
    ok("승인자 승인 → 개발 배포 완료", s == 200, f"{s} {b}")

    # 1-b. 만료일이 짧아지는 갱신본 → 거부 (개발에 현재 배포본이 있으므로 비교)
    s, b = A.act("deploy.request", req_ops(shorter, store.get("certificates", shorter), "dev"))
    ok("자동 점검: 현재 배포본보다 만료일이 이른 갱신본 → 거부", s == 409 and "만료일 연장" in b.get("error", ""), f"{s} {b.get('error')}")

    # 2-b. 순서대로면 통과: 테스트 요청·검토·승인 → 운영 요청 가능
    s1, _ = A.act("deploy.request", req_ops(good, good_rec, "test"))
    s2, _ = RV.act("deploy.review", review_ops(good, "test", REV))
    s3, _ = AP.act("deploy.approve", approve_ops(good, good_rec, "test", APR))
    s4, b4 = A.act("deploy.request", req_ops(good, good_rec, "prod"))
    ok("단계 순서: 개발 → 테스트 완료 후 운영 요청 → 통과", (s1, s2, s3, s4) == (200, 200, 200, 200), f"{s1} {s2} {s3} {s4} {b4}")

    # 4. 승인 시 재확인: 운영 검토 통과 뒤 테스트 단계가 원복되면 운영 승인 거부
    s, _ = RV.act("deploy.review", review_ops(good, "prod", REV))
    store.update("deployments", f"dp_{good}_test", {"status": "원복"})
    s, b = AP.act("deploy.approve", approve_ops(good, good_rec, "prod", APR))
    ok("승인 시 재확인: 테스트 단계가 원복되면 운영 승인 거부", s == 409 and "단계 순서 위반" in b.get("error", ""), f"{s} {b.get('error')}")

    # 3. 검토 시 재점검: 요청 뒤 남은 기간이 30일 이하가 되면 검토 통과 거부
    later_rec = store.get("certificates", later)
    s, _ = A.act("deploy.request", req_ops(later, later_rec, "dev"))
    store.update("certificates", later, {"not_after": (now + timedelta(days=5)).isoformat()})
    s, b = RV.act("deploy.review", review_ops(later, "dev", REV))
    ok("검토 시 재점검: 요청 뒤 남은 5일이 되면 '검토 통과' 거부(화면 우회 포함)", s == 409 and "남은 기간 30일 초과" in b.get("error", ""), f"{s} {b.get('error')}")
    s, b = RV.act("deploy.review", review_ops(later, "dev", REV, passed=False))
    ok("검토 반려는 그대로 가능", s == 200, f"{s} {b}")

    # 6. 서버 적용 실패 → 즉시 알림 + 메일
    A.call("PUT", "api/settings/smtp", {"enabled": True, "host": "127.0.0.1", "port": SMTP, "security": "none", "from_addr": "cert@demo.test"})
    s, reg = A.call("POST", "api/agents", {"host": "web01", "name": "web01", "os": "Linux", "deploy_stage": "dev"})
    eid = env_id("dev", "shop.gate.test")
    rq = urllib.request.Request(BASE + "api/agent-api/deploy-result", method="POST", headers={"Authorization": "Agent " + reg["token"], "Content-Type": "application/json"},
                                data=json.dumps({"env_id": eid, "cert_id": good, "ok": False, "rolled_back": True,
                                                 "detail": "configtest: nginx -t rc=1 key values mismatch -> rollback"}).encode())
    with urllib.request.urlopen(rq, timeout=10) as r:
        r.read()
    t0 = time.time()
    noti, mail = None, []
    while time.time() - t0 < 10:
        _, data = A.call("GET", "api/db/notifications")
        rows = data if isinstance(data, list) else data.get("items", data.get("rows", []))
        noti = next((n for n in rows if n.get("level") == "배포 실패"), None)
        mail = [json.loads(x) for x in open(W / "mail.jsonl")] if (W / "mail.jsonl").exists() else []
        if noti and noti.get("mail_status") and mail:
            break
        time.sleep(0.3)
    ok("서버 적용 실패 보고 → 담당자 알림 즉시 생성", noti and "배포 실패" in noti.get("title", "") and "자동 원복" in noti.get("title", ""), (noti or {}).get("title"))
    ok("서버 적용 실패 → 담당자 메일 발송", noti and noti.get("mail_status") == "발송" and any("배포 실패" in json.dumps(m, ensure_ascii=False) for m in mail),
       f"{(noti or {}).get('mail_status')} · 수신 {len(mail)}건 → {[m.get('to') or m.get('rcpts') for m in mail][:2]}")
finally:
    srv.terminate()
    sink.terminate()
    out = ROOT / "results" / "e2e_deploy_gates.json"
    out.write_text(json.dumps({"generated_at": agents.now_iso(), "passed": sum(r["pass"] for r in R), "total": len(R), "results": R}, ensure_ascii=False, indent=1))
    print(f"\n{sum(r['pass'] for r in R)}/{len(R)} 통과 → {out}")
    shutil.rmtree(W, ignore_errors=True)
    sys.exit(0 if R and all(r["pass"] for r in R) else 1)

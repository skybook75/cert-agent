"""인증서 관리 플랫폼 — PC 실행 서버 (파이썬 표준 라이브러리 + cryptography).

  python platform_server.py --scan "C:\\...\\pem" [--deploy "C:\\...\\deploy"] [--port 8000]
  → http://localhost:8000  (처음 실행하면 관리자 계정 생성 화면)

보안 구조
  - 로그인: 플랫폼 자체 계정(PBKDF2-SHA256 해시), 세션 쿠키(HttpOnly·SameSite=Strict), 5회 실패 시 10분 잠금
  - 권한: 역할 5종(관리자·담당자·검토자·승인자·조회자) × 작업별 허용표(PERMS) 를 서버에서 검사
          쓰기는 /api/action 한 곳으로만: 작업별 허용 컬렉션·필드·상태 전이·담당 범위를 검사하고, 처리자는 세션 기준으로 기록
  - 조회 범위: 담당자는 본인 담당 인증서와 그 알림·배포·갱신 이력만
  - 직무 분리: 배포 요청자 ≠ 검토자 ≠ 승인자
  - 키·비밀번호: Claude 키(ANTHROPIC_API_KEY)·SMTP 비밀번호(SMTP_PASSWORD)는 환경변수에만
  - PFX 비밀번호: 담당자가 플랫폼에서 입력 → AES-256-GCM 암호화 후 보관(vault.py, 키는 DB 와 분리된 키 파일).
                  화면·API 로 평문을 다시 내보내지 않고, 수집 Agent 가 PFX 를 열 때만 메모리에서 복호화
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import agents  # noqa: E402
import vault as vault_mod  # noqa: E402
import ops_agents  # noqa: E402
import targets as targets_mod  # noqa: E402

STORE: agents.Store = None
VAULT: vault_mod.Vault = None
SCAN_ROOT: str | None = None
DEPLOY_ROOT: Path = BASE / "deploy"
STATIC = {"/": "web/index.html", "/index.html": "web/index.html", "/core.js": "web/core.js",
          "/xlsx-lite.js": "web/xlsx-lite.js", "/scanner.js": "web/scanner.js", "/guides.json": "guides/guides.json"}
MAX_BODY = 2 * 1024 * 1024
LOCK = threading.RLock()

# ───────────────────────────── 역할·권한 (web/core.js 의 PERMS 와 같은 표)
ROLES = ["관리자", "담당자", "검토자", "승인자", "조회자"]
PERMS = {
    "collect.run": ["관리자"], "notify.run": ["관리자"], "import.excel": ["관리자"], "cert.manage": ["관리자"],
    "discovery.review": ["관리자", "검토자"], "discovery.decide": ["관리자", "승인자"],
    "cert.renewal": ["관리자", "담당자"], "deploy.request": ["관리자", "담당자"],
    "deploy.review": ["관리자", "검토자"], "deploy.approve": ["관리자", "승인자"], "deploy.rollback": ["관리자", "승인자"],
    "noti.read": ["관리자", "담당자"], "mail.send": ["관리자"], "users.manage": ["관리자"], "settings.manage": ["관리자"],
    "audit.view": ["관리자", "검토자", "승인자", "조회자"], "guide.manage": ["관리자"],
    "secret.manage": ["관리자", "담당자"],
    "ops.manage": ["관리자"],
    "target.manage": ["관리자", "담당자"],
}
OWNER_SCOPED = {"cert.renewal", "deploy.request", "noti.read"}
CERT_META = {"fingerprint", "subject_cn", "subject_dn", "issuer_cn", "issuer_dn", "not_before", "not_after", "serial_number",
             "public_key", "signature_algorithm", "cert_type", "san", "locations", "pem"}
# 작업별로 쓸 수 있는 컬렉션·필드 ("*" = 문서 전체, set 은 새 문서 ID 접두어 제한)
RULES = {
    "discovery.decide": {"discoveries.update": {"status", "kind", "cert_id", "diffs", "decided_by", "decided_at"},
                         "certificates.update": CERT_META | {"renewal_status", "replaced_by", "renewed_at", "verified_at", "verified_by",
                                                             "approved_by", "approved_at", "source", "registry_diffs", "owner", "system"},
                         "certificates.set": ("c_", "*"), "renewals.set": ("r_", "*"), "closeAlerts": True},
    "discovery.review": {"discoveries.update": {"review_status", "review_note", "reviewed_by", "reviewed_at"}},
    "cert.manage": {"certificates.update": {"owner", "owner_email", "system"}},
    "cert.renewal": {"certificates.update": {"renewal_status", "renewed_at"}, "renewals.set": ("r_", "*"), "closeAlerts": True},
    "import.excel": {"certificates.set": ("x_", "*"),
                     "certificates.update": {"owner", "owner_email", "system", "registered_from", "registry_row", "issuer_cn", "not_after", "serial_number"},
                     "import_runs.set": ("imp_", "*")},
    "deploy.request": {"deployments.set": ("dp_", "*")},
    "deploy.review": {"deployments.update": {"status", "review"}},
    "deploy.approve": {"deployments.update": {"status", "approved_by", "approved_at", "prev_cert_id"}, "environments.set": ("env_", "*")},
    "deploy.rollback": {"deployments.update": {"status", "rolled_back_by", "rolled_back_at", "rollback_reason", "restored_cert_id"},
                        "environments.set": ("env_", "*")},
    "noti.read": {"notifications.update": {"read", "read_by", "read_at"}},
    "guide.manage": {"guides.set": ("g_", "*"), "guides.update": {"deleted", "deleted_by", "deleted_at"}},
}
ACTION_LABEL = {"discovery.decide": "수집 승인·반려", "discovery.review": "수집 검토", "cert.manage": "관리정보 변경", "cert.renewal": "갱신 처리",
                "import.excel": "관리대장 등록", "deploy.request": "배포 요청", "deploy.review": "배포 검토", "deploy.approve": "배포 승인",
                "deploy.rollback": "배포 원복", "noti.read": "알림 확인", "guide.manage": "가이드 관리",
                "secret.manage": "PFX 비밀번호 관리"}
ACTOR_FIELDS = {"decided_by", "reviewed_by", "approved_by", "handled_by", "requested_by", "read_by", "rolled_back_by", "verified_by", "by", "uploaded_by", "deleted_by"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ───────────────────────────── 계정·세션
PBKDF2_ITER = 200_000
USER_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, list] = {}
SESSION_TTL, IDLE_TTL = 8 * 3600, 60 * 60


def hash_pw(pw: str, salt: bytes | None = None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex(), hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER).hex()


def check_pw_policy(pw: str):
    if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise ValueError("비밀번호는 8자 이상, 영문과 숫자를 함께 사용하세요")


def public_user(u):
    return {k: u.get(k) for k in ("id", "username", "name", "email", "role", "active", "must_change", "created_at", "last_login")}


def label(u):
    return f"{u['name']}({u['role']})"


def owns(user, cert):
    if not cert:
        return False
    return (bool(cert.get("owner")) and cert["owner"] == user["name"]) or \
        (bool(cert.get("owner_email")) and bool(user.get("email")) and cert["owner_email"].lower() == user["email"].lower())


class Http(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    # ───────── 응답
    def _send(self, code, body: bytes, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8", headers)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("요청이 너무 큽니다")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("JSON 형식이 아닙니다") from None

    # ───────── 세션
    def _session(self):
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)sid=([A-Za-z0-9_\-]{20,})", cookie)
        if not m:
            return None
        with LOCK:
            s = SESSIONS.get(m.group(1))
            if not s:
                return None
            t = time.time()
            if t - s["created"] > SESSION_TTL or t - s["seen"] > IDLE_TTL:
                SESSIONS.pop(m.group(1), None)
                return None
            u = STORE.get("users", s["uid"])
            if not u or not u.get("active", True):
                SESSIONS.pop(m.group(1), None)
                return None
            s["seen"] = t
            return {"sid": m.group(1), "csrf": s["csrf"], "user": u}

    def _require(self, method, action=None):
        s = self._session()
        if not s:
            raise Http(401, "로그인이 필요합니다")
        if method != "GET":
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token") or "", s["csrf"]):
                raise Http(403, "요청 검증(CSRF)에 실패했습니다. 새로고침 후 다시 시도하세요")
            origin = self.headers.get("Origin")
            same_host = origin and origin.split("://", 1)[-1] == (self.headers.get("Host") or "")
            if origin and not same_host and not re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin):
                raise Http(403, "허용되지 않은 출처의 요청입니다")
        if s["user"].get("must_change") and action not in (None, "password"):
            raise Http(403, "처음 로그인했습니다. 비밀번호를 먼저 변경하세요")
        if action and action != "password" and s["user"]["role"] not in PERMS.get(action, []):
            raise Http(403, f"권한이 없습니다 ({s['user']['role']}: {ACTION_LABEL.get(action, action)})")
        return s

    def _audit(self, user, action, detail):
        STORE.set("audit", "a_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f") + secrets.token_hex(2),
                  {"at": now_iso(), "actor": label(user) if isinstance(user, dict) else user, "action": action, "detail": str(detail)[:300]})

    # ───────── 라우팅
    def _route(self, method):
        path = urlparse(self.path).path
        t0 = time.time()
        try:
            return self._route_inner(method, path)
        finally:
            if path.startswith("/api/"):
                ops_agents.record_perf(f"{method} {path}", (time.time() - t0) * 1000)

    def _route_inner(self, method, path):
        try:
            if method == "GET" and path in STATIC:
                f = BASE / STATIC[path]
                ctype = (mimetypes.guess_type(f.name)[0] or "text/plain") + "; charset=utf-8"
                body = f.read_bytes()
                if f.name == "index.html":
                    body = (b'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" '
                            b'content="width=device-width, initial-scale=1"></head><body>' + body + b"</body></html>")
                return self._send(200, body, ctype, {"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                     "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                                     "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
            if method == "GET" and path.startswith("/vendor/pdfjs/"):     # PDF 가이드 읽기용 pdf.js (로컬 번들)
                root = (BASE / "web" / "vendor" / "pdfjs").resolve()
                f = (root / path[len("/vendor/pdfjs/"):]).resolve()
                if root not in f.parents or not f.is_file():
                    return self._json({"error": "not found"}, 404)
                ctype = {".mjs": "text/javascript; charset=utf-8", ".bcmap": "application/octet-stream", ".txt": "text/plain; charset=utf-8"}.get(f.suffix, "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)
            if path == "/api/health":
                return self._json({"ok": True, "mode": "server", "scan_root": SCAN_ROOT, "deploy_root": str(DEPLOY_ROOT),
                                   "setup_needed": not STORE.list("users"), "llm": bool(os.environ.get("ANTHROPIC_API_KEY")),
                                   "smtp": bool(agents.smtp_settings(STORE).get("enabled"))})
            if method == "POST" and path == "/api/setup":
                return self._setup()
            if method == "POST" and path == "/api/login":
                return self._login()
            if method == "POST" and path == "/api/logout":
                s = self._session()
                if s:
                    with LOCK:
                        SESSIONS.pop(s["sid"], None)
                return self._json({"ok": True}, headers={"Set-Cookie": "sid=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
            if method == "GET" and path == "/api/me":
                s = self._session()
                if not s:
                    raise Http(401, "로그인이 필요합니다")
                return self._json({"user": public_user(s["user"]), "csrf": s["csrf"], "perms": PERMS})
            if method == "POST" and path == "/api/password":
                return self._password()
            parts = path.strip("/").split("/")
            if parts[:2] == ["api", "db"] and len(parts) == 3 and method == "GET":
                s = self._require("GET")
                return self._json(self._read(s["user"], parts[2]))
            if method == "POST" and path == "/api/action":
                return self._action()
            if method == "POST" and path == "/api/agent/collect":
                s = self._require(method, "collect.run")
                if not SCAN_ROOT:
                    raise Http(400, "서버 실행 시 --scan 폴더를 지정하세요")
                with LOCK:   # PFX 비밀번호는 요청으로 받지 않고 보관함에서 복호화해 사용
                    return self._json(agents.collect(STORE, SCAN_ROOT, (), [SCAN_ROOT], label(s["user"]), vault=VAULT))
            if path == "/api/targets" or path.startswith("/api/targets/"):
                return self._targets(method, path)
            if path.startswith("/api/ops/"):
                return self._ops(method, path)
            if method == "POST" and path == "/api/maint/client-error":
                s = self._require(method)
                b = self._body()
                now = time.time()
                if now - getattr(self.server, "_last_cerr", 0) > 0.5:       # 과도한 보고 제한
                    self.server._last_cerr = now
                    ops_agents.record_error(STORE, "client", str(b.get("where", ""))[:200], str(b.get("message", ""))[:500], str(b.get("stack", ""))[:3000])
                return self._json({"ok": True})
            if path == "/api/secrets" or path.startswith("/api/secrets/"):
                return self._secrets(method, path)
            if path == "/api/agents" or path.startswith("/api/agents/"):
                return self._agents_admin(method, path)
            if path.startswith("/api/agent-api/"):
                return self._agent_api(method, path)
            if method == "POST" and path == "/api/agent/notify":
                self._require(method, "notify.run")
                with LOCK:
                    return self._json(agents.notify(STORE))
            if method == "POST" and path == "/api/mail/send":
                s = self._require(method, "mail.send")
                ids = [i for i in self._body().get("ids", []) if isinstance(i, str)][:200]
                with LOCK:
                    return self._json(agents.mail_notifications(STORE, ids, label(s["user"])))
            if method == "POST" and path == "/api/mail/test":
                s = self._require(method, "settings.manage")
                to = str(self._body().get("to", ""))[:200]
                try:
                    cfg = agents.smtp_settings(STORE)
                    agents.send_mail(cfg, to, "[인증서 관리 플랫폼] 알림 채널 테스트", "인증서 관리 플랫폼의 이메일 발송 설정이 정상입니다.")
                except Exception as e:  # noqa: BLE001
                    raise Http(400, f"발송 실패: {str(e)[:200]}") from None
                ch = agents.channel_notify(STORE, {"title": "알림 채널 테스트", "body": "인증서 관리 플랫폼 알림 채널(문자·푸시) 테스트입니다."}, {})
                self._audit(s["user"], "알림 채널 테스트", f"메일 {to} · 문자 {ch.get('sms_status', '미사용')} · 푸시 {ch.get('push_status', '미사용')}")
                return self._json({"ok": True, **ch})
            if path == "/api/settings/smtp":
                s = self._require(method, "settings.manage")
                if method == "GET":
                    return self._json(agents.smtp_settings(STORE))
                if method == "PUT":
                    b = self._body()
                    cfg = {"enabled": bool(b.get("enabled")), "host": str(b.get("host", ""))[:200], "port": int(b.get("port") or 25),
                           "security": b.get("security") if b.get("security") in ("none", "starttls", "ssl") else "none",
                           "username": str(b.get("username", ""))[:200], "from_addr": str(b.get("from_addr", ""))[:200],
                           "provider": "ses" if b.get("provider") == "ses" else "smtp", "aws_region": re.sub(r"[^a-z0-9-]", "", str(b.get("aws_region", "")))[:20],
                           "sms_enabled": bool(b.get("sms_enabled")), "sms_to": [re.sub(r"[^0-9+]", "", str(x))[:16] for x in (b.get("sms_to") or [])][:5],
                           "push_topic": re.sub(r"[^A-Za-z0-9_-]", "", str(b.get("push_topic", "")))[:64]}
                    if cfg["provider"] == "ses":
                        cfg["enabled"] = True
                    STORE.set("settings", "smtp", cfg)
                    self._audit(s["user"], "SMTP 설정 변경", f"{cfg['host']}:{cfg['port']} {cfg['security']}")
                    return self._json(cfg)
            if path == "/api/users" and method == "POST":
                return self._create_user()
            if parts[:2] == ["api", "users"] and len(parts) == 3 and method == "PATCH":
                return self._update_user(parts[2])
            if method == "POST" and path == "/api/llm":
                self._require(method)
                prompt = str(self._body().get("prompt", ""))[:20000]
                try:
                    return self._json({"text": agents.ask_claude(prompt)})
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 503)
            return self._json({"error": "not found"}, 404)
        except Http as e:
            return self._json({"error": str(e)}, e.code)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                ops_agents.record_error(STORE, "server", f"{method} {path}", f"{type(e).__name__}: {e}", traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
            return self._json({"error": "서버 처리 중 오류가 발생했습니다"}, 500)

    # ───────── 계정
    def _setup(self):
        with LOCK:
            if STORE.list("users"):
                raise Http(403, "이미 관리자 계정이 있습니다")
            b = self._body()
            u = self._new_user(b, role="관리자", must_change=False)
            self._audit(u, "최초 관리자 생성", u["username"])
        return self._json({"ok": True})

    def _new_user(self, b, role, must_change):
        username = str(b.get("username", "")).strip().lower()
        if not USER_RE.match(username):
            raise ValueError("아이디는 영문 소문자·숫자·._- 3~32자")
        if STORE.get("users", "u_" + username):
            raise ValueError("이미 있는 아이디입니다")
        name = str(b.get("name", "")).strip()[:30]
        if not name:
            raise ValueError("이름을 입력하세요")
        email = str(b.get("email", "")).strip()[:200]
        if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            raise ValueError("이메일 형식이 아닙니다")
        if role not in ROLES:
            raise ValueError("알 수 없는 역할")
        pw = str(b.get("password", ""))
        check_pw_policy(pw)
        salt, h = hash_pw(pw)
        u = {"username": username, "name": name, "email": email, "role": role, "active": True, "must_change": must_change,
             "salt": salt, "pw_hash": h, "created_at": now_iso(), "last_login": None}
        STORE.set("users", "u_" + username, u)
        return dict(u, id="u_" + username)

    def _login(self):
        b = self._body()
        username = str(b.get("username", "")).strip().lower()
        pw = str(b.get("password", ""))
        t = time.time()
        with LOCK:
            fails = [x for x in FAILS.get(username, []) if t - x < 600]
            FAILS[username] = fails
            if len(fails) >= 5:
                raise Http(429, "로그인 5회 실패로 10분간 잠겼습니다")
            u = STORE.get("users", "u_" + username) if USER_RE.match(username) else None
            ok = False
            if u and u.get("active", True):
                _, h = hash_pw(pw, bytes.fromhex(u["salt"]))
                ok = hmac.compare_digest(h, u["pw_hash"])
            else:
                hash_pw(pw)   # 계정 유무에 따른 응답 시간 차이 줄이기
            if not ok:
                fails.append(t)
                self._audit(f"{username[:32] or '(빈 아이디)'}(로그인 시도)", "로그인 실패",
                            f"{len(fails)}회째 실패" + (" · 없는 계정" if not u else " · 비활성 계정" if not u.get("active", True) else " · 비밀번호 불일치"))
                raise Http(401, "아이디 또는 비밀번호가 맞지 않습니다")
            FAILS.pop(username, None)
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            SESSIONS[sid] = {"uid": u["id"], "csrf": csrf, "created": t, "seen": t}
            STORE.update("users", u["id"], {"last_login": now_iso()})
            self._audit(u, "로그인", username)
        return self._json({"user": public_user(u), "csrf": csrf, "perms": PERMS},
                          headers={"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Strict"})

    def _password(self):
        s = self._require("POST", "password")
        b = self._body()
        u = s["user"]
        _, h = hash_pw(str(b.get("old", "")), bytes.fromhex(u["salt"]))
        if not hmac.compare_digest(h, u["pw_hash"]):
            raise Http(400, "현재 비밀번호가 맞지 않습니다")
        new = str(b.get("new", ""))
        check_pw_policy(new)
        if new == b.get("old"):
            raise ValueError("새 비밀번호는 현재 비밀번호와 달라야 합니다")
        salt, h2 = hash_pw(new)
        STORE.update("users", u["id"], {"salt": salt, "pw_hash": h2, "must_change": False})
        self._audit(u, "비밀번호 변경", u["username"])
        return self._json({"ok": True})

    def _create_user(self):
        s = self._require("POST", "users.manage")
        b = self._body()
        with LOCK:
            u = self._new_user(b, role=b.get("role"), must_change=True)
        self._audit(s["user"], "사용자 생성", f"{u['username']} ({u['role']})")
        return self._json(public_user(u))

    def _update_user(self, uid):
        s = self._require("PATCH", "users.manage")
        b = self._body()
        with LOCK:
            u = STORE.get("users", uid)
            if not u:
                raise Http(404, "사용자가 없습니다")
            patch = {}
            if "role" in b:
                if b["role"] not in ROLES:
                    raise ValueError("알 수 없는 역할")
                patch["role"] = b["role"]
            if "active" in b:
                patch["active"] = bool(b["active"])
            if "email" in b:
                patch["email"] = str(b["email"])[:200]
            if b.get("reset_password"):
                check_pw_policy(str(b["reset_password"]))
                salt, h = hash_pw(str(b["reset_password"]))
                patch.update({"salt": salt, "pw_hash": h, "must_change": True})
            if uid == s["user"]["id"] and (patch.get("role", "관리자") != "관리자" or patch.get("active") is False):
                raise ValueError("본인의 관리자 권한은 해제할 수 없습니다")
            STORE.update("users", uid, patch)
            if patch.get("active") is False or "reset_password" in b or "role" in patch:
                for k in [k for k, v in SESSIONS.items() if v["uid"] == uid]:
                    SESSIONS.pop(k, None)
        self._audit(s["user"], "사용자 변경", f"{u['username']}: {', '.join(k for k in patch if k not in ('salt', 'pw_hash'))}")
        return self._json(public_user(STORE.get("users", uid)))

    # ───────── 조회 (역할별 범위)
    # ───────── PFX 비밀번호 보관함 (평문은 받기만 하고 내보내지 않음)
    def _secrets(self, method, path):
        s = self._require(method, "secret.manage")
        user, admin = s["user"], s["user"]["role"] == "관리자"
        mine = lambda sec: admin or sec.get("owner_id") == user["id"]   # noqa: E731
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 2:
            items = [vault_mod.public(x) for x in STORE.list("secrets") if mine(x)]
            runs = sorted(STORE.list("scan_runs"), key=lambda r: r.get("started_at", ""), reverse=True)
            needs = [e for e in (runs[0].get("errors", []) if runs else []) if str(e.get("reason", "")).startswith("비밀번호")]
            return self._json({"items": items, "needs": needs, "alg": vault_mod.ALG, "kid": VAULT.kid, "last_run_at": runs[0].get("started_at") if runs else None,
                               "key_source": VAULT.source if admin else None})
        if method != "POST":
            raise Http(405, "지원하지 않는 요청")
        b = self._body()
        pw = b.pop("password", None)
        if len(parts) == 2:                                   # 등록
            if not isinstance(pw, str) or not (1 <= len(pw) <= 256):
                raise ValueError("비밀번호를 1~256자로 입력하세요")
            scope = b.get("scope")
            if scope not in vault_mod.SCOPES:
                raise ValueError("적용 범위를 고르세요")
            if scope == "all" and not admin:
                raise Http(403, "'전체 파일' 범위는 관리자만 등록할 수 있습니다")
            value = "" if scope == "all" else vault_mod.norm_rel(b.get("scope_value", ""))[:300]
            if scope != "all" and not value:
                raise ValueError("적용할 폴더 또는 파일 경로를 입력하세요")
            if scope == "file" and not value.lower().endswith((".pfx", ".p12")):
                raise ValueError("파일 범위는 .pfx 또는 .p12 파일이어야 합니다")
            lab = str(b.get("label", "")).strip()[:60] or (value or "전체 PFX")
            sid = "s_" + secrets.token_hex(8)
            doc = {"label": lab, "scope": scope, "scope_value": value, "owner": user["name"], "owner_id": user["id"],
                   "created_by": label(user), "created_at": now_iso(), "use_count": 0, "active": True, "enc": VAULT.encrypt(sid, pw)}
            pw = None
            with LOCK:
                STORE.set("secrets", sid, doc)
                self._audit(user, "PFX 비밀번호 등록", f"{lab} · {self._scope_text(doc)} (암호화 보관)")
            return self._json(vault_mod.public(dict(doc, id=sid)))
        if len(parts) == 4 and parts[3] in ("replace", "delete"):
            sid = parts[2]
            with LOCK:
                sec = STORE.get("secrets", sid) if agents.ID_RE.match(sid) else None
                if not sec or not mine(sec):
                    raise Http(404, "비밀번호 항목을 찾을 수 없습니다")
                if parts[3] == "delete":
                    STORE.delete("secrets", sid)
                    self._audit(user, "PFX 비밀번호 삭제", f"{sec['label']} · {self._scope_text(sec)}")
                    return self._json({"ok": True})
                if not isinstance(pw, str) or not (1 <= len(pw) <= 256):
                    raise ValueError("새 비밀번호를 1~256자로 입력하세요")
                STORE.update("secrets", sid, {"enc": VAULT.encrypt(sid, pw), "updated_at": now_iso(), "last_result": None})
                pw = None
                self._audit(user, "PFX 비밀번호 교체", f"{sec['label']} · {self._scope_text(sec)}")
                return self._json(vault_mod.public(STORE.get("secrets", sid)))
        raise Http(404, "not found")

    # ───────── 서버 Agent (원격 서버의 수집·배포 Agent) — 관리자 등록, 토큰은 한 번만 보여 주고 해시만 저장
    def _agents_admin(self, method, path):
        s = self._require(method, "settings.manage")
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 2:
            return self._json([{k: a.get(k) for k in ("id", "name", "host", "os", "deploy_stage", "created_by", "created_at", "last_seen", "last_result", "version")}
                               for a in STORE.list("agents")])
        if method == "POST" and len(parts) == 2:
            b = self._body()
            host = str(b.get("host", "")).strip().lower()
            if not re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", host):
                raise ValueError("호스트 이름은 영문 소문자·숫자·- 로 입력하세요 (예: web01)")
            stage = b.get("deploy_stage") or ""
            if stage not in ("", "dev", "test", "edu", "prod"):
                raise ValueError("배포 단계 값 오류")
            token = secrets.token_urlsafe(32)
            aid = "ag_" + host
            if STORE.get("agents", aid):
                raise ValueError("이미 등록된 호스트입니다(삭제 후 다시 등록)")
            STORE.set("agents", aid, {"name": str(b.get("name", host))[:40], "host": host, "os": str(b.get("os", ""))[:20], "deploy_stage": stage,
                                      "token_hash": hashlib.sha256(token.encode()).hexdigest(), "created_by": label(s["user"]), "created_at": now_iso()})
            self._audit(s["user"], "서버 Agent 등록", f"{host} · 배포 단계 {stage or '없음'}")
            return self._json({"id": aid, "token": token})
        if method == "POST" and len(parts) == 4 and parts[3] == "delete":
            if not agents.ID_RE.match(parts[2]) or not STORE.get("agents", parts[2]):
                raise Http(404, "없는 Agent")
            STORE.delete("agents", parts[2])
            self._audit(s["user"], "서버 Agent 삭제", parts[2])
            return self._json({"ok": True})
        raise Http(404, "not found")

    def _agent_auth(self):
        m = re.match(r"^Agent ([A-Za-z0-9_\-]{20,})$", self.headers.get("Authorization") or "")
        if not m:
            raise Http(401, "Agent 토큰이 필요합니다")
        h = hashlib.sha256(m.group(1).encode()).hexdigest()
        for a in STORE.list("agents"):
            if hmac.compare_digest(a.get("token_hash", ""), h):
                return a
        raise Http(401, "등록되지 않은 Agent 토큰")

    def _agent_api(self, method, path):
        ag = self._agent_auth()
        host, op = ag["host"], path.rsplit("/", 1)[-1]
        actor = f"{ag['name']}(서버 Agent)"
        if method == "POST" and op == "hello":
            b = self._body()
            pub = str(b.get("pubkey", ""))
            if pub and "BEGIN PUBLIC KEY" not in pub:
                raise ValueError("공개키 형식 오류")
            STORE.update("agents", ag["id"], {"last_seen": now_iso(), "os": str(b.get("os", ag.get("os", "")))[:40], "version": str(b.get("version", ""))[:20],
                                              **({"pubkey": pub[:2000]} if pub else {})})
            return self._json({"id": ag["id"], "host": host, "deploy_stage": ag.get("deploy_stage", "")})
        if method == "POST" and op == "secrets":
            # PFX 비밀번호: 이 호스트 범위에 맞는 것만, Agent 공개키(RSA-OAEP)로 감싸서 전달 — 평문은 네트워크·로그에 남지 않음
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            import base64
            if not ag.get("pubkey"):
                raise ValueError("hello 로 공개키를 먼저 등록하세요")
            pub = serialization.load_pem_public_key(STORE.get("agents", ag["id"])["pubkey"].encode())
            docs = STORE.list("secrets")
            out = []
            for pth in [str(x)[:400] for x in (self._body().get("files") or [])][:200]:
                rel = f"{host}/{pth.replace(':', '').replace(chr(92), '/').lstrip('/')}"
                cands = []
                for sec in vault_mod.candidates(docs, rel)[:5]:
                    try:
                        pw = VAULT.decrypt(sec["id"], sec["enc"])
                    except Exception:  # noqa: BLE001
                        continue
                    ct = pub.encrypt(pw.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=sec["id"].encode()))
                    pw = None
                    cands.append({"secret_id": sec["id"], "wrapped": base64.b64encode(ct).decode()})
                out.append({"path": pth, "candidates": cands})
            self._audit(actor, "PFX 비밀번호 전달(암호화)", f"{host}: 파일 {len(out)}개, 비밀번호 {sum(len(x['candidates']) for x in out)}건")
            return self._json({"files": out})
        if method == "POST" and op == "ingest":
            b = self._body()
            with LOCK:
                res = agents.ingest(STORE, host, b.get("files") or [], actor)
                summary = f"수집 {res['files']}개 · 승인 대기 {res['new_pending']} · 실패 {res['failed']}"
                for tid in [str(x) for x in (b.get("target_ids") or [])][:20]:          # 플랫폼에 등록한 수집 대상 실행 결과
                    t = STORE.get("targets", tid) if agents.ID_RE.match(tid) else None
                    if t and t.get("host") == host:
                        STORE.update("targets", tid, {"last_run_at": now_iso(), "last_result": summary, "last_ok": res["failed"] == 0, "done_seq": int(t.get("run_seq") or 0)})
            STORE.update("agents", ag["id"], {"last_seen": now_iso(), "last_result": summary})
            return self._json(res)
        if method == "GET" and op == "config":            # 플랫폼에 등록된 이 서버의 수집 경로
            return self._json({"scan": targets_mod.agent_paths(STORE, host),
                               "targets": [t["id"] for t in STORE.list("targets") if t.get("method") == "agent" and t.get("host") == host]})
        if method == "GET" and op == "collect-tasks":     # '수집 실행' 요청
            STORE.update("agents", ag["id"], {"last_seen": now_iso()})
            return self._json({"tasks": targets_mod.agent_tasks(STORE, host)})
        if method == "GET" and op == "deploy-tasks":
            stage = ag.get("deploy_stage")
            tasks = []
            if stage:
                certs = {c["id"]: c for c in STORE.list("certificates")}
                for env in STORE.list("environments"):
                    if env.get("stage") != stage:
                        continue
                    want = env.get("cert_id")
                    mine = (env.get("applied") or {}).get(host, {})
                    if not want or mine.get("cert_id") == want or mine.get("failed_cert_id") == want:
                        continue
                    c = certs.get(want)
                    if not c or not c.get("pem"):
                        continue
                    same = [x for x in certs.values() if str(x.get("subject_cn", "")).lower() == str(env.get("subject_cn", "")).lower()]
                    if mine.get("cert_id") in certs:          # 이 서버에 지금 적용돼 있는 인증서의 파일이 교체 대상
                        same = [certs[mine["cert_id"]]]
                    targets = sorted({loc.split(":", 1)[1] for x in same if x["id"] != want for loc in x.get("locations", []) if loc.startswith(host + ":")})
                    targets = [t for t in targets if t not in {l.split(":", 1)[1] for l in c.get("locations", []) if l.startswith(host + ":")} or t.endswith(("fullchain.pem", "cert.pem"))]  # 새 인증서 자신의 파일(incoming 등)은 제외
                    if targets:
                        tasks.append({"env_id": env["id"], "stage": stage, "subject_cn": env["subject_cn"], "cert_id": want, "pem": c["pem"],
                                      "fingerprint": c.get("fingerprint"), "targets": targets})
            return self._json({"tasks": tasks})
        if method == "POST" and op == "deploy-result":
            b = self._body()
            env_id = str(b.get("env_id", ""))
            with LOCK:
                env = STORE.get("environments", env_id) if agents.ID_RE.match(env_id) else None
                if not env or env.get("stage") != ag.get("deploy_stage"):
                    raise Http(404, "배포 대상 아님")
                rec = {"cert_id": str(b.get("cert_id", ""))[:60] if b.get("ok") else env.get("applied", {}).get(host, {}).get("cert_id"),
                       "ok": bool(b.get("ok")), "at": now_iso(), "detail": str(b.get("detail", ""))[:400],
                       "served_fingerprint": str(b.get("served_fingerprint", ""))[:120], "rolled_back": bool(b.get("rolled_back"))}
                if not b.get("ok"):
                    rec["failed_cert_id"] = str(b.get("cert_id", ""))[:60]
                applied = dict(env.get("applied") or {})
                applied[host] = rec
                STORE.update("environments", env_id, {"applied": applied})
                STORE.update("agents", ag["id"], {"last_seen": now_iso(), "last_result": f"배포 {env.get('stage_name')} {env.get('subject_cn')} {'성공' if b.get('ok') else '실패·자동 원복'}"})
                self._audit(actor, "서버 적용" if b.get("ok") else "서버 적용 실패", f"{host} {env.get('stage_name')} {env.get('subject_cn')}: {rec['detail'][:200]}")
            return self._json({"ok": True})
        raise Http(404, "not found")

    def _targets(self, method, path):
        """수집 대상 서버 목록: 관리자 전체, 담당자는 자기가 등록한 것만."""
        s = self._require(method, "target.manage")
        user, admin = s["user"], s["user"]["role"] == "관리자"
        mine = lambda t: admin or t.get("owner_id") == user["id"]   # noqa: E731
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 2:
            agents_by_host = {a["host"]: a for a in STORE.list("agents")}
            out = []
            for t in STORE.list("targets"):
                if mine(t):
                    a = agents_by_host.get(t.get("host"))
                    out.append(dict(targets_mod.public(t), agent_registered=bool(a), agent_last_seen=(a or {}).get("last_seen")))
            return self._json(sorted(out, key=lambda t: (t.get("host", ""), t.get("name", ""))))
        if method != "POST":
            raise Http(405, "지원하지 않는 요청")
        b = self._body()
        pw = b.pop("password", None)
        if pw is not None and (not isinstance(pw, str) or len(pw) > 256):
            raise ValueError("비밀번호는 256자 이하")
        if len(parts) == 2:                                    # 등록
            doc = targets_mod.clean(b)
            tid = "tg_" + secrets.token_hex(6)
            doc.update(owner=user["name"], owner_id=user["id"], created_by=label(user), created_at=now_iso())
            if pw:
                doc["enc"] = VAULT.encrypt(tid, pw)
            pw = None
            if doc["method"] == "ssh" and not doc.get("enc"):
                raise ValueError("SSH 방식은 접속 비밀번호가 필요합니다(암호화 저장)")
            with LOCK:
                STORE.set("targets", tid, doc)
                self._audit(user, "수집 대상 등록", f"{doc['name']} · {doc['host']} · {doc['method']} · 경로 {len(doc['paths'])}개{' · 접속 비밀번호(암호화)' if doc.get('enc') else ''}")
            return self._json(targets_mod.public(dict(doc, id=tid)))
        if len(parts) == 4 and parts[3] in ("update", "delete", "run"):
            tid = parts[2]
            t = STORE.get("targets", tid) if agents.ID_RE.match(tid) else None
            if not t or not mine(t):
                raise Http(404, "수집 대상을 찾을 수 없습니다")
            if parts[3] == "delete":
                with LOCK:
                    STORE.delete("targets", tid)
                    self._audit(user, "수집 대상 삭제", f"{t['name']} · {t['host']}")
                return self._json({"ok": True})
            if parts[3] == "update":
                doc = targets_mod.clean(b, t)
                if pw:
                    doc["enc"] = VAULT.encrypt(tid, pw)
                pw = None
                doc.update(updated_at=now_iso(), updated_by=label(user))
                with LOCK:
                    STORE.update("targets", tid, doc)
                    self._audit(user, "수집 대상 수정", f"{doc['name']} · {doc['host']}{' · 접속 비밀번호 교체' if 'enc' in doc else ''}")
                return self._json(targets_mod.public(STORE.get("targets", tid)))
            # run
            if t["method"] == "agent":
                if not any(a.get("host") == t["host"] for a in STORE.list("agents")):
                    raise ValueError(f"{t['host']} 서버 Agent 가 등록되어 있지 않습니다(관리 탭에서 서버 등록)")
                STORE.update("targets", tid, {"run_requested_at": now_iso(), "run_requested_by": label(user), "run_seq": int(t.get("run_seq") or 0) + 1,
                                              "last_result": "실행 요청됨 — 서버 Agent 가 1분 안에 수집"})
                self._audit(user, "수집 실행 요청", f"{t['name']} · {t['host']} (서버 Agent)")
                return self._json({"ok": True, "queued": True})
            try:
                res = targets_mod.ssh_collect(STORE, VAULT, t, label(user))
                summary, ok = f"수집 {res['files']}개 · 승인 대기 {res['new_pending']} · 실패 {res['failed']}", res["failed"] == 0
            except Exception as e:  # noqa: BLE001
                res, summary, ok = None, f"접속·수집 실패: {str(e)[:150]}", False
            STORE.update("targets", tid, {"last_run_at": now_iso(), "last_result": summary, "last_ok": ok})
            self._audit(user, "수집 실행(SSH)", f"{t['name']} · {t['host']} → {summary}")
            return self._json({"ok": ok, "result": summary, "detail": res})
        raise Http(404, "not found")

    def _ops(self, method, path):
        """AI 운영: 오케스트레이터 · 유지보수 Agent (관리자)."""
        s = self._require(method, "ops.manage")
        who = label(s["user"])
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/api/ops/state":
            maint = STORE.list("maint")
            return self._json({
                "agents": ops_agents.AGENT_DEFS, "tools": {k: {"agent": v[0], "label": v[1]} for k, v in ops_agents.TOOLS.items()},
                "runs": sorted(STORE.list("orch_runs"), key=lambda r: r.get("started_at", ""), reverse=True)[:15],
                "findings": sorted([f for f in maint if f.get("type") == "finding"], key=lambda f: (f.get("status") != "열림", -ops_agents.SEV.get(f.get("severity"), 0), f.get("title", ""))),
                "reports": sorted([r for r in maint if r.get("type") == "report"], key=lambda r: r.get("at", ""), reverse=True)[:20],
                "chat": (STORE.get("maint", "chat_log") or {}).get("messages", [])[-30:],
                "perf": ops_agents.perf_summary()[:8], "errors": len([e for e in STORE.list("errors") if not e.get("resolved")]),
                "llm": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CERT_LLM_MOCK")),
                "llm_model": agents.LLM_STATE.get("model"), "llm_error": agents.LLM_STATE.get("last_error")})
        b = self._body() if method == "POST" else {}
        if method == "POST" and path == "/api/ops/orchestrate":
            goal = str(b.get("goal", "")).strip()[:300] or "일일 운영 점검"
            with LOCK:
                return self._json(ops_agents.orchestrate(STORE, goal, who))
        if method == "POST" and path == "/api/ops/diagnose":
            with LOCK:
                fs = ops_agents.diagnose(STORE)
            self._audit(s["user"], "자가 진단", f"열린 항목 {sum(1 for f in fs if f.get('status') == '열림')}건")
            return self._json({"findings": fs})
        if method == "POST" and path == "/api/ops/chat":
            msg = str(b.get("message", "")).strip()[:2000]
            if not msg:
                raise ValueError("메시지를 입력하세요")
            return self._json(ops_agents.chat(STORE, msg, who))     # LLM 호출이 길 수 있어 전역 잠금 없이(문서 저장은 Store 잠금)
        if method == "POST" and len(parts) == 4 and parts[2] in ("fix", "dismiss", "rollback"):
            if not agents.ID_RE.match(parts[3]):
                raise Http(400, "ID 오류")
            if True:
                if parts[2] == "fix":
                    r = ops_agents.apply_fix(STORE, parts[3], who)
                    self._audit(s["user"], "진단 항목 반영", f"{parts[3]}: {r['result'][:200]}")
                    return self._json(r)
                if parts[2] == "dismiss":
                    STORE.update("maint", parts[3], {"status": "무시", "dismissed_by": who, "dismissed_at": agents.now_iso()})
                    return self._json({"ok": True})
                return self._json(ops_agents.rollback(STORE, parts[3], who))
        raise Http(404, "not found")

    @staticmethod
    def _scope_text(sec):
        return {"all": "전체 PFX", "folder": f"폴더 {sec.get('scope_value')}/", "file": f"파일 {sec.get('scope_value')}"}.get(sec.get("scope"), "-")

    def _read(self, user, coll):
        role = user["role"]
        if coll == "users":
            if role != "관리자":
                raise Http(403, "권한이 없습니다")
            return [public_user(u) for u in STORE.list("users")]
        if coll == "settings":
            raise Http(403, "설정은 전용 API 로 조회합니다")
        if coll in ("secrets", "agents", "errors", "maint", "orch_runs", "targets"):
            raise Http(403, "전용 API 로 조회합니다")
        data = STORE.list(coll)
        if role != "담당자":
            if coll == "audit" and role not in PERMS["audit.view"]:
                return []
            return data
        if coll == "guides":
            return data
        mine = {c["id"] for c in STORE.list("certificates") if owns(user, c)}
        if coll == "certificates":
            return [c for c in data if c["id"] in mine]
        if coll in ("notifications", "deployments"):
            return [x for x in data if x.get("cert_id") in mine]
        if coll == "renewals":
            return [x for x in data if x.get("old_cert_id") in mine or x.get("new_cert_id") in mine]
        if coll == "environments":
            return [x for x in data if x.get("cert_id") in mine or x.get("prev_cert_id") in mine]
        if coll == "audit":
            return [x for x in data if str(x.get("actor", "")).startswith(user["name"] + "(")]
        return []   # discoveries, scan_runs, import_runs: 담당자 조회 범위 아님

    # ───────── 쓰기: 작업 단위 검사 후 적용
    def _action(self):
        b = self._body()
        action = b.get("action")
        if action not in RULES:
            raise Http(400, "알 수 없는 작업")
        s = self._require("POST", action)
        user, actor = s["user"], label(s["user"])
        ops = b.get("ops")
        if not isinstance(ops, list) or not ops or len(ops) > 500:
            raise ValueError("작업 내용이 없습니다")
        rules = RULES[action]
        with LOCK:
            certs = {c["id"]: c for c in STORE.list("certificates")}
            touched = set()
            for o in ops:
                kind = o.get("op")
                if kind == "closeAlerts":
                    if not rules.get("closeAlerts"):
                        raise Http(403, "허용되지 않은 작업입니다 (알림 종료)")
                    touched.add(o.get("cert_id"))
                    continue
                coll, oid, data = o.get("coll"), o.get("id"), o.get("data")
                if kind not in ("set", "update") or not isinstance(data, dict) or not isinstance(oid, str):
                    raise ValueError("작업 형식 오류")
                rule = rules.get(f"{coll}.{kind}")
                if rule is None:
                    raise Http(403, f"허용되지 않은 작업입니다 ({coll} {kind})")
                if kind == "set":
                    prefix, _ = rule
                    if not oid.startswith(prefix):
                        raise Http(403, f"허용되지 않은 문서 ID 입니다 ({oid})")
                else:
                    extra = set(data) - rule
                    if extra:
                        raise Http(403, f"수정할 수 없는 항목입니다: {', '.join(sorted(extra))}")
                    if STORE.get(coll, oid) is None:
                        raise Http(404, f"문서가 없습니다 ({coll}/{oid})")
                for f in ACTOR_FIELDS & set(data):           # 처리자는 세션 기준으로 기록
                    if data[f]:
                        data[f] = actor
                if isinstance(data.get("review"), dict):
                    data["review"]["reviewed_by"] = actor
                if isinstance(data.get("history"), list) and data["history"] and isinstance(data["history"][-1], dict):
                    data["history"][-1]["by"] = actor
                if coll == "certificates":
                    touched.add(oid)
                elif coll == "renewals":
                    touched.add(data.get("old_cert_id"))
                elif coll == "deployments":
                    touched.add(data.get("cert_id") if kind == "set" else STORE.get(coll, oid).get("cert_id"))
                elif coll == "notifications":
                    touched.add(STORE.get(coll, oid).get("cert_id"))
            if user["role"] == "담당자" and action in OWNER_SCOPED:
                bad = [t for t in touched if not owns(user, certs.get(t))]
                if bad:
                    raise Http(403, "본인 담당 인증서만 처리할 수 있습니다")
            result = self._check_workflow(action, ops, user)
            for o in ops:
                if o["op"] == "closeAlerts":
                    for n in STORE.list("notifications"):
                        if n.get("cert_id") == o.get("cert_id") and not n.get("closed"):
                            STORE.update("notifications", n["id"], {"closed": True, "closed_at": now_iso()})
                elif o["op"] == "set":
                    STORE.set(o["coll"], o["id"], o["data"])
                else:
                    STORE.update(o["coll"], o["id"], o["data"])
            summary = str(b.get("summary", ""))[:300]
            lab = ACTION_LABEL[action]
            if action == "discovery.decide":   # 승인과 반려를 이력에서 구분 (반려는 discoveries 상태만 바꿈)
                lab = "수집 반려" if all(o.get("coll") == "discoveries" and (o.get("data") or {}).get("status") == "반려" for o in ops) else "수집 승인"
            self._audit(user, lab, summary or f"{len(ops)}건")
        return self._json({"ok": True, **result})

    # 상태 전이·직무 분리·배포 파일 반영
    def _check_workflow(self, action, ops, user):
        if action == "discovery.decide":
            for o in ops:
                if o.get("coll") == "discoveries":
                    cur = STORE.get("discoveries", o["id"])
                    if cur.get("status") != "대기":
                        raise Http(409, f"이미 처리된 수집 결과입니다 ({cur.get('status')})")
            return {}
        if not action.startswith("deploy.") or action == "deploy.request":
            if action == "deploy.request":
                for o in ops:
                    cur = STORE.get("deployments", o["id"])
                    if cur and cur.get("status") in ("요청", "검토 통과", "배포 완료"):
                        raise Http(409, "이미 진행 중이거나 완료된 배포 단계입니다")
                    o["data"]["status"] = "요청"
            return {}
        dep_op = next((o for o in ops if o["coll"] == "deployments"), None)
        if not dep_op:
            raise ValueError("배포 문서가 없습니다")
        dep = STORE.get("deployments", dep_op["id"])
        req_name = str(dep.get("requested_by", "")).split("(")[0]
        new_status = dep_op["data"].get("status")
        if action == "deploy.review":
            if dep.get("status") != "요청" or new_status not in ("검토 통과", "검토 반려"):
                raise Http(409, f"검토할 수 없는 상태입니다 ({dep.get('status')})")
            if req_name == user["name"]:
                raise Http(403, "직무 분리: 배포 요청자는 검토할 수 없습니다")
            return {}
        if action == "deploy.approve":
            if dep.get("status") != "검토 통과" or new_status != "배포 완료":
                raise Http(409, f"승인할 수 없는 상태입니다 ({dep.get('status')})")
            rev_name = str((dep.get("review") or {}).get("reviewed_by", "")).split("(")[0]
            if user["name"] in (req_name, rev_name):
                raise Http(403, "직무 분리: 요청자·검토자는 승인할 수 없습니다")
            env_op = next((o for o in ops if o["coll"] == "environments"), None)
            ed = (env_op or {}).get("data", {})
            if not env_op or ed.get("cert_id") != dep["cert_id"] or ed.get("stage") != dep["stage"] or ed.get("subject_cn") != dep["subject_cn"]:
                raise Http(400, "배포 환경 정보가 배포 요청과 다릅니다")
            return {"deployed": self._deploy_file(dep["stage"], dep["subject_cn"], env_op["data"].get("cert_id"))}
        if action == "deploy.rollback":
            if dep.get("status") != "배포 완료" or new_status != "원복":
                raise Http(409, f"원복할 수 없는 상태입니다 ({dep.get('status')})")
            env_op = next((o for o in ops if o["coll"] == "environments"), None)
            cur_env = STORE.get("environments", env_op["id"]) if env_op else None
            if not cur_env or cur_env.get("cert_id") != dep["cert_id"]:
                raise Http(409, "현재 배포본이 아니어서 원복할 수 없습니다")
            if env_op["data"].get("cert_id") != cur_env.get("prev_cert_id"):
                raise Http(400, "원복 대상이 직전 배포본과 다릅니다")
            return {"deployed": self._deploy_file(dep["stage"], dep["subject_cn"], env_op["data"].get("cert_id"))}
        return {}

    def _deploy_file(self, stage, cn, cert_id):
        """배포 폴더 deploy/<단계>/<인증서명>/ 에 인증서 파일 반영. 기존 파일은 history/<시각>/ 로 보관."""
        if stage not in ("dev", "test", "edu", "prod"):
            raise Http(400, "알 수 없는 배포 단계")
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(cn))[:80] or "cert"
        target = DEPLOY_ROOT / stage / safe
        cert, src = None, None
        if cert_id:                                       # 원본 파일을 먼저 확인한 뒤 교체
            cert = STORE.get("certificates", cert_id)
            if SCAN_ROOT:
                root = Path(SCAN_ROOT).resolve()
                for rel in (cert or {}).get("locations") or []:
                    cand = (root / rel).resolve()
                    if root in cand.parents and cand.is_file():
                        src = cand
                        break
            if not src and not (cert or {}).get("pem"):
                raise Http(400, "배포할 인증서 파일을 찾을 수 없습니다 (스캔 폴더의 원본 파일 확인)")
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        olds = [p for p in target.iterdir() if p.is_file()]
        if olds:
            hist = target / "history" / stamp
            hist.mkdir(parents=True, exist_ok=True)
            for p in olds:
                shutil.move(str(p), str(hist / p.name))
        if not cert_id:
            return {"path": str(target), "file": None}
        if src:
            shutil.copy2(src, target / src.name)
            fname = src.name
        else:                                             # 원격 서버(Agent) 수집분: 보관된 공개 인증서 PEM 으로 기록, 실제 설치는 서버 Agent 가 수행
            fname = f"{safe}.crt"
            (target / fname).write_text(cert["pem"].strip() + "\n", encoding="utf-8")
        src = Path(fname)
        (target / "DEPLOYED.json").write_text(json.dumps({"cert_id": cert_id, "subject_cn": cn, "file": src.name, "not_after": cert.get("not_after"),
                                                          "fingerprint": cert.get("fingerprint"), "deployed_at": now_iso()}, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"path": str(target), "file": src.name}

    def do_GET(self):
        self._route("GET")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def do_POST(self):
        self._route("POST")

    def log_message(self, *a):
        pass


def main(argv=None):
    global STORE, SCAN_ROOT, DEPLOY_ROOT, VAULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE / "platform.db"))
    ap.add_argument("--scan", help="수집 Agent가 스캔할 승인 폴더")
    ap.add_argument("--deploy", default=str(BASE / "deploy"), help="단계별 배포 폴더 (deploy/<dev|test|edu|prod>/<인증서명>)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1", help="접속 허용 주소 (서버 설치 시 0.0.0.0, 방화벽·보안그룹으로 접근 제한)")
    ap.add_argument("--vault-key", help="PFX 비밀번호 보관함 키 파일 (기본: %%APPDATA%%\\cert_platform\\vault.key, DB 와 다른 곳)")
    a = ap.parse_args(argv)
    STORE = agents.Store(a.db)
    VAULT = vault_mod.Vault(a.vault_key)
    SCAN_ROOT = str(Path(a.scan).resolve()) if a.scan else None
    DEPLOY_ROOT = Path(a.deploy).resolve()
    sess_file = Path(a.db).with_suffix(".sessions")       # 유지보수 Agent 반영 후 재시작 시 로그인 세션 유지(600 권한, 즉시 삭제)
    if sess_file.exists():
        try:
            SESSIONS.update(json.loads(sess_file.read_text(encoding="utf-8")))
        finally:
            sess_file.unlink(missing_ok=True)

    def _save_sessions():
        fd = os.open(sess_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(SESSIONS))
    ops_agents.BEFORE_RESTART.append(_save_sessions)
    url = f"http://localhost:{a.port}"
    print(f"인증서 관리 플랫폼: {url}\n  DB: {a.db}\n  스캔 폴더: {SCAN_ROOT or '(미지정)'}\n  배포 폴더: {DEPLOY_ROOT}\n"
          f"  PFX 비밀번호 보관함: {vault_mod.ALG}, 키 {VAULT.source}\n"
          f"  Claude 챗봇: {'사용' if os.environ.get('ANTHROPIC_API_KEY') else '키 없음 → 검색형 답변'}\n"
          f"  계정: {'없음 → 브라우저에서 최초 관리자 생성' if not STORE.list('users') else '로그인 필요'}")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

"""PostgreSQL 접속 모듈 — 표준 라이브러리만 사용(사내망·폐쇄망 PC에도 별도 설치 없이 동작).

  conn = pgwire.connect("postgresql://user:pw@127.0.0.1:5432/certplatform")
  rows = conn.query("SELECT id, data FROM docs WHERE coll=$1", ["users"])   # 값은 항상 매개변수로 분리 전송
  conn.execute("BEGIN"); ...; conn.execute("COMMIT")

  - 프로토콜: PostgreSQL v3 확장 질의(Parse/Bind/Execute) — SQL 문과 값이 따로 전달되어 SQL 인젝션이 구조적으로 불가
  - 인증: SCRAM-SHA-256(PostgreSQL 14+ 기본), MD5, 평문, trust
  - 결과는 텍스트 형식(문자열 또는 None). 형 변환은 호출하는 쪽에서 한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import struct
from urllib.parse import unquote, urlparse


class PgError(Exception):
    def __init__(self, fields: dict):
        self.fields = fields
        self.code = fields.get("C", "")
        super().__init__(f"{fields.get('S', 'ERROR')} {self.code}: {fields.get('M', '')}")


def _cstr(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


class Connection:
    def __init__(self, url: str, timeout: float = 15.0):
        u = urlparse(url)
        if u.scheme not in ("postgresql", "postgres"):
            raise ValueError("postgresql:// 주소가 아닙니다")
        self.user = unquote(u.username or "postgres")
        self.password = unquote(u.password or "")
        self.database = (u.path or "/").lstrip("/") or self.user
        host, port = u.hostname or "127.0.0.1", u.port or 5432
        if host.startswith("/") or (u.hostname is None and "host=" in (u.query or "")):
            path = host if host.startswith("/") else dict(x.split("=", 1) for x in u.query.split("&"))["host"]
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(os.path.join(path, f".s.PGSQL.{port}"))
        else:
            self.sock = socket.create_connection((host, port), timeout=timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)    # 작은 메시지 지연(Nagle) 방지
        self.sock.settimeout(None)
        self._buf = b""
        self.params: dict = {}
        self._startup()

    # ── 저수준 송수신
    @staticmethod
    def _msg(typ: bytes, payload: bytes = b"") -> bytes:
        return typ + struct.pack("!I", len(payload) + 4) + payload

    def _send(self, typ: bytes, payload: bytes = b""):
        self.sock.sendall(self._msg(typ, payload))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("PostgreSQL 연결이 끊어졌습니다")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv(self):
        typ = self._recv_exact(1)
        (ln,) = struct.unpack("!I", self._recv_exact(4))
        return typ, self._recv_exact(ln - 4)

    @staticmethod
    def _fields(body: bytes) -> dict:
        out = {}
        for part in body.split(b"\x00"):
            if part:
                out[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return out

    # ── 접속·인증
    def _startup(self):
        body = struct.pack("!I", 196608) + _cstr("user") + _cstr(self.user) + _cstr("database") + _cstr(self.database) \
            + _cstr("client_encoding") + _cstr("UTF8") + _cstr("application_name") + _cstr("cert-platform") + b"\x00"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        scram = None
        while True:
            typ, data = self._recv()
            if typ == b"R":
                (code,) = struct.unpack("!I", data[:4])
                if code == 0:
                    continue
                if code == 3:                                            # 평문
                    self._send(b"p", _cstr(self.password))
                elif code == 5:                                          # MD5
                    inner = hashlib.md5((self.password + self.user).encode()).hexdigest()
                    self._send(b"p", _cstr("md5" + hashlib.md5(inner.encode() + data[4:8]).hexdigest()))
                elif code == 10:                                         # SASL 시작
                    mechs = [m.decode() for m in data[4:].split(b"\x00") if m]
                    if "SCRAM-SHA-256" not in mechs:
                        raise PgError({"M": f"지원하지 않는 인증 방식: {mechs}"})
                    scram = _Scram(self.password)
                    first = scram.client_first().encode()
                    self._send(b"p", _cstr("SCRAM-SHA-256") + struct.pack("!I", len(first)) + first)
                elif code == 11:                                         # SASL 계속
                    self._send(b"p", scram.client_final(data[4:].decode()).encode())
                elif code == 12:                                         # SASL 완료 — 서버 서명 확인
                    scram.verify(data[4:].decode())
                else:
                    raise PgError({"M": f"지원하지 않는 인증 코드 {code}"})
            elif typ == b"S":
                k, v, _ = data.split(b"\x00", 2)
                self.params[k.decode()] = v.decode()
            elif typ == b"E":
                raise PgError(self._fields(data))
            elif typ == b"Z":
                return

    # ── 질의
    def query(self, sql: str, params=()) -> list:
        """확장 질의 1회. 매개변수는 $1, $2 … 로 참조. 결과 행(문자열 튜플) 목록을 돌려준다."""
        enc = []
        for p in params:
            if p is None:
                enc.append(struct.pack("!i", -1))
            else:
                b = (p if isinstance(p, str) else str(p)).encode("utf-8")
                enc.append(struct.pack("!i", len(b)) + b)
        self.sock.sendall(self._msg(b"P", b"\x00" + _cstr(sql) + struct.pack("!H", 0))                       # 한 번에 전송(왕복 1회)
                          + self._msg(b"B", b"\x00\x00" + struct.pack("!H", 0) + struct.pack("!H", len(enc)) + b"".join(enc) + struct.pack("!H", 0))
                          + self._msg(b"D", b"P\x00") + self._msg(b"E", b"\x00" + struct.pack("!I", 0)) + self._msg(b"S"))
        rows, err = [], None
        while True:
            typ, data = self._recv()
            if typ == b"D":
                (n,) = struct.unpack("!H", data[:2]); pos, row = 2, []
                for _ in range(n):
                    (ln,) = struct.unpack("!i", data[pos:pos + 4]); pos += 4
                    if ln < 0:
                        row.append(None)
                    else:
                        row.append(data[pos:pos + ln].decode("utf-8")); pos += ln
                rows.append(tuple(row))
            elif typ == b"E":
                err = PgError(self._fields(data))
            elif typ == b"Z":
                if err:
                    raise err
                return rows
            # 1 ParseComplete, 2 BindComplete, T RowDescription, n NoData, C CommandComplete, N Notice, S ParameterStatus: 무시

    execute = query

    def close(self):
        try:
            self._send(b"X")
        except OSError:
            pass
        self.sock.close()


class _Scram:
    """RFC 5802 / 7677 SCRAM-SHA-256 클라이언트 (채널 바인딩 없음)"""
    def __init__(self, password: str):
        self.password = password.encode("utf-8")
        self.nonce = base64.b64encode(os.urandom(18)).decode()
        self.first_bare = f"n=,r={self.nonce}"

    def client_first(self) -> str:
        return "n,," + self.first_bare

    def client_final(self, server_first: str) -> str:
        a = dict(x.split("=", 1) for x in server_first.split(","))
        if not a["r"].startswith(self.nonce):
            raise PgError({"M": "SCRAM nonce 불일치"})
        salted = hashlib.pbkdf2_hmac("sha256", self.password, base64.b64decode(a["s"]), int(a["i"]))
        ckey = hmac.new(salted, b"Client Key", "sha256").digest()
        without_proof = f"c=biws,r={a['r']}"
        self.auth_msg = f"{self.first_bare},{server_first},{without_proof}".encode()
        sig = hmac.new(hashlib.sha256(ckey).digest(), self.auth_msg, "sha256").digest()
        proof = bytes(x ^ y for x, y in zip(ckey, sig))
        self.server_key = hmac.new(salted, b"Server Key", "sha256").digest()
        return f"{without_proof},p={base64.b64encode(proof).decode()}"

    def verify(self, server_final: str):
        v = dict(x.split("=", 1) for x in server_final.split(","))
        expect = base64.b64encode(hmac.new(self.server_key, self.auth_msg, "sha256").digest()).decode()
        if not hmac.compare_digest(v.get("v", ""), expect):
            raise PgError({"M": "SCRAM 서버 서명 불일치"})


def connect(url: str, timeout: float = 15.0) -> Connection:
    return Connection(url, timeout)

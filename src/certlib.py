"""인증서 수집 Agent v2 (테스트 및 고도화) — 공용 라이브러리.

v1(PoC) 대비 변경점
  1. cert_location 테이블: 같은 인증서의 저장 위치를 모두 기록 (PoC 개선점)
  2. scan_run / scan_error 테이블: 스캔 실행 이력·실패 사유 보존 (처리 이력 관리)
  3. 증분 스캔: 파일 크기·수정시각이 그대로면 파싱 생략
  4. 상태 판정을 KST 기준 + 만료 '시각' 기준으로 수정 (자정 경계 오판 해결)
  5. 승인 경로(allowed roots) 밖 스캔 차단, 심볼릭 링크 미추적, 파일 크기 상한
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

KST = timezone(timedelta(hours=9))
EXTENSIONS = {".pem", ".crt", ".cer", ".der", ".pfx", ".p12"}
MAX_FILE_BYTES = 1 * 1024 * 1024        # 인증서 파일 상한 1MB (대용량 위장 파일 차단)

SCHEMA = """
CREATE TABLE IF NOT EXISTS certificate (
    cert_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_cn TEXT, subject_dn TEXT, issuer_cn TEXT, issuer_dn TEXT,
    not_before          TEXT NOT NULL,   -- UTC ISO8601
    not_after           TEXT NOT NULL,   -- UTC ISO8601
    serial_number TEXT, version TEXT, public_key TEXT, signature_algorithm TEXT,
    cert_type TEXT, san TEXT,
    sha256_fingerprint  TEXT NOT NULL UNIQUE,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    search_text         TEXT             -- 검색용: CN·SAN·일련번호·저장 경로 (스캔 시 갱신)
);
CREATE TABLE IF NOT EXISTS cert_location (
    loc_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cert_id     INTEGER NOT NULL REFERENCES certificate(cert_id),
    file_path   TEXT NOT NULL,
    file_size   INTEGER, file_mtime REAL,
    last_seen   TEXT NOT NULL,
    UNIQUE(cert_id, file_path)
);
CREATE TABLE IF NOT EXISTS scan_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, folder TEXT, started_at TEXT, elapsed_sec REAL,
    files INTEGER, parsed INTEGER, skipped INTEGER, new_certs INTEGER, updated INTEGER, failed INTEGER
);
CREATE TABLE IF NOT EXISTS scan_error (
    run_id INTEGER, file_path TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_cert_not_after ON certificate(not_after);
CREATE INDEX IF NOT EXISTS ix_cert_issuer    ON certificate(issuer_cn);
CREATE INDEX IF NOT EXISTS ix_loc_path       ON cert_location(file_path);
CREATE INDEX IF NOT EXISTS ix_loc_cert       ON cert_location(cert_id);
"""


# ─────────────────────────────── 파싱
class CertError(Exception):
    pass


def load_certs(path: Path, passwords: list[str]) -> list[x509.Certificate]:
    return load_certs_ex(path, passwords)[0]


def load_certs_ex(path: Path, passwords: list[str]):
    """(인증서 목록, 사용한 비밀번호 순번) — 비밀번호 없이 열리거나 PFX 가 아니면 순번은 None."""
    size = path.stat().st_size
    if size == 0:
        raise CertError("빈 파일")
    if size > MAX_FILE_BYTES:
        raise CertError(f"파일 크기 초과({size // 1024}KB > 1MB)")
    data = path.read_bytes()
    if path.suffix.lower() in (".pfx", ".p12"):
        for i, pw in enumerate([None] + list(passwords)):
            try:
                _, cert, extra = pkcs12.load_key_and_certificates(data, pw.encode() if pw else None)
                certs = ([cert] if cert else []) + list(extra)
                if not certs:
                    raise CertError("PKCS#12 안에 인증서 없음")
                return certs, (i - 1 if i else None)
            except ValueError:
                continue
        raise CertError("비밀번호 필요")
    try:
        if b"-----BEGIN CERTIFICATE" in data:
            return x509.load_pem_x509_certificates(data), None
        if b"-----BEGIN" in data:
            raise CertError("인증서가 아닌 PEM(개인키 등)")
        return [x509.load_der_x509_certificate(data)], None
    except CertError:
        raise
    except Exception as e:
        raise CertError(f"형식 오류: {type(e).__name__}")


def _cn(name):
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATIONAL_UNIT_NAME, NameOID.ORGANIZATION_NAME):
        a = name.get_attributes_for_oid(oid)
        if a:
            return str(a[0].value)
    return name.rfc4514_string()


def _ext(cert, oid):
    try:
        return cert.extensions.get_extension_for_oid(oid).value
    except Exception:
        return None


def _key(cert):
    try:
        k = cert.public_key()
    except Exception:
        return "지원하지 않는 알고리즘"
    if isinstance(k, rsa.RSAPublicKey):
        return f"RSA {k.key_size}bit"
    if isinstance(k, ec.EllipticCurvePublicKey):
        return f"EC {k.curve.name}"
    if isinstance(k, dsa.DSAPublicKey):
        return f"DSA {k.key_size}bit"
    return type(k).__name__


def to_record(cert) -> dict:
    san = _ext(cert, ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    bc = _ext(cert, ExtensionOID.BASIC_CONSTRAINTS)
    return {
        "subject_cn": _cn(cert.subject), "subject_dn": cert.subject.rfc4514_string(),
        "issuer_cn": _cn(cert.issuer), "issuer_dn": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.isoformat(), "not_after": cert.not_valid_after_utc.isoformat(),
        "serial_number": format(cert.serial_number, "X"), "version": cert.version.name,
        "public_key": _key(cert), "signature_algorithm": getattr(cert.signature_algorithm_oid, "_name", ""),
        "cert_type": "CA 인증서" if bc and bc.ca else "서버 인증서",
        "san": ", ".join(str(v.value) for v in san if hasattr(v, "value")) if san else "",
        "sha256_fingerprint": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
    }


# ─────────────────────────────── 상태 판정 (KST, 만료 시각 기준)
def status_of(not_before: str, not_after: str, now: datetime | None = None) -> tuple[str, int]:
    now = now or datetime.now(timezone.utc)
    nb, na = datetime.fromisoformat(not_before), datetime.fromisoformat(not_after)
    days = (na.astimezone(KST).date() - now.astimezone(KST).date()).days
    if na <= now:
        return "만료", days
    if nb > now:
        return "사용 전", days
    if days <= 7:
        return "D-7", days
    if days <= 30:
        return "D-30", days
    return "정상", days


# ─────────────────────────────── 스캔 → DB
@dataclass
class ScanResult:
    files: int = 0
    parsed: int = 0
    skipped: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    elapsed: float = 0.0
    lines: list = field(default_factory=list)


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def check_root(folder: Path, allowed_roots: list[str] | None):
    real = folder.resolve()
    if allowed_roots and not any(real == Path(r).resolve() or Path(r).resolve() in real.parents for r in allowed_roots):
        raise PermissionError(f"승인되지 않은 스캔 경로: {real}")


def iter_files(folder: Path):
    for dirpath, dirnames, files in os.walk(folder, followlinks=False):
        for name in sorted(files):
            p = Path(dirpath, name)
            if p.is_symlink():
                continue
            if p.suffix.lower() in EXTENSIONS or name.lower() == "signcert.der":
                yield p


def scan(folder, db_path, passwords=(), allowed_roots=None, incremental=True) -> ScanResult:
    folder = Path(folder)
    check_root(folder, allowed_roots)
    conn = connect(db_path)
    t0 = time.perf_counter()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = ScanResult()
    known = {}
    if incremental:
        known = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT file_path, file_size, file_mtime FROM cert_location")}
    run_id = conn.execute("INSERT INTO scan_run(folder, started_at) VALUES (?, ?)", (str(folder), now)).lastrowid
    for f in iter_files(folder):
        res.files += 1
        rel = str(f.relative_to(folder))
        st = f.stat()
        if incremental and known.get(rel) == (st.st_size, st.st_mtime):
            res.skipped += 1
            conn.execute("UPDATE cert_location SET last_seen=? WHERE file_path=?", (now, rel))
            continue
        try:
            certs = load_certs(f, list(passwords))
        except CertError as e:
            res.failed += 1
            conn.execute("INSERT INTO scan_error VALUES (?, ?, ?)", (run_id, rel, str(e)))
            res.lines.append(f"[실패] {rel}: {e}")
            continue
        for c in certs:
            rec = to_record(c)
            row = conn.execute("SELECT cert_id FROM certificate WHERE sha256_fingerprint=?",
                               (rec["sha256_fingerprint"],)).fetchone()
            if row:
                cert_id = row[0]
                conn.execute("UPDATE certificate SET last_seen=? WHERE cert_id=?", (now, cert_id))
                res.updated += 1
                kind = "갱신"
            else:
                cols = list(rec) + ["first_seen", "last_seen"]
                cert_id = conn.execute(
                    f"INSERT INTO certificate ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [*rec.values(), now, now]).lastrowid
                res.new += 1
                kind = "신규"
            conn.execute(
                "INSERT INTO cert_location(cert_id, file_path, file_size, file_mtime, last_seen) VALUES (?,?,?,?,?) "
                "ON CONFLICT(cert_id, file_path) DO UPDATE SET file_size=excluded.file_size, "
                "file_mtime=excluded.file_mtime, last_seen=excluded.last_seen",
                (cert_id, rel, st.st_size, st.st_mtime, now))
            res.parsed += 1
            res.lines.append(f"[{kind}] {rel}: {rec['subject_cn']} (만료 {rec['not_after'][:10]})")
    if res.parsed:  # 바뀐 인증서가 있을 때만 검색용 컬럼 재구성
        conn.execute("UPDATE certificate SET search_text = subject_cn || ' ' || san || ' ' || serial_number || ' ' || "
                     "COALESCE((SELECT group_concat(file_path, ' ') FROM cert_location l "
                     "WHERE l.cert_id = certificate.cert_id), '')")
    res.elapsed = time.perf_counter() - t0
    conn.execute("UPDATE scan_run SET elapsed_sec=?, files=?, parsed=?, skipped=?, new_certs=?, updated=?, failed=? "
                 "WHERE run_id=?", (res.elapsed, res.files, res.parsed, res.skipped, res.new, res.updated,
                                    res.failed, run_id))
    conn.commit()
    conn.close()
    return res


# ─────────────────────────────── 조회 (API·자연어 조회 공용)
STATUSES = {"정상", "D-30", "D-7", "만료", "사용 전"}
CERT_TYPES = {"서버 인증서", "CA 인증서"}


class QueryError(ValueError):
    pass


def _date(v, name):
    try:
        return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=KST)
    except (TypeError, ValueError):
        raise QueryError(f"{name} 날짜 형식은 YYYY-MM-DD 입니다")


def validate(params: dict) -> dict:
    """조회조건 가드레일: 허용 키만, 길이·형식·값 목록 검사."""
    allowed = {"q", "issuer", "cert_type", "status", "expiry_from", "expiry_to", "within_days"}
    p = {k: str(v).strip() for k, v in params.items() if k in allowed and v not in (None, "")}
    if len(p.get("q", "")) > 100:
        raise QueryError("검색어는 100자 이하로 입력하세요")
    if len(p.get("issuer", "")) > 200:
        raise QueryError("발급자 조건이 너무 깁니다")
    if p.get("status") and p["status"] not in STATUSES:
        raise QueryError(f"상태는 {', '.join(sorted(STATUSES))} 중 하나입니다")
    if p.get("cert_type") and p["cert_type"] not in CERT_TYPES:
        raise QueryError("인증서 종류는 '서버 인증서' 또는 'CA 인증서'입니다")
    for k in ("expiry_from", "expiry_to"):
        if k in p:
            _date(p[k], k)
    if "within_days" in p:
        if not p["within_days"].isdigit() or not 0 <= int(p["within_days"]) <= 3650:
            raise QueryError("within_days 는 0~3650 정수입니다")
    if "expiry_from" in p and "expiry_to" in p and p["expiry_from"] > p["expiry_to"]:
        raise QueryError("만료일 시작이 끝보다 늦습니다")
    return p


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _status_bounds(status: str, now: datetime):
    """상태 → not_after/not_before 범위 조건 (Python status_of 와 같은 규칙을 SQL로)."""
    day0 = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    d8 = (day0 + timedelta(days=8)).astimezone(timezone.utc).isoformat()
    d31 = (day0 + timedelta(days=31)).astimezone(timezone.utc).isoformat()
    n = now.isoformat()
    return {
        "만료": ("c.not_after <= ?", [n]),
        "사용 전": ("c.not_after > ? AND c.not_before > ?", [n, n]),
        "D-7": ("c.not_after > ? AND c.not_after < ? AND c.not_before <= ?", [n, d8, n]),
        "D-30": ("c.not_after >= ? AND c.not_after < ? AND c.not_before <= ?", [d8, d31, n]),
        "정상": ("c.not_after >= ? AND c.not_before <= ?", [d31, n]),
    }[status]


def search(conn, params: dict, now: datetime | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
    p = validate(params)
    now = now or datetime.now(timezone.utc)
    where, args = [], []
    if "q" in p:
        where.append("c.search_text LIKE ? ESCAPE '\\'")
        args.append(_like(p["q"]))
    if "issuer" in p:
        where.append("c.issuer_cn = ?")
        args.append(p["issuer"])
    if "cert_type" in p:
        where.append("c.cert_type = ?")
        args.append(p["cert_type"])
    if "expiry_from" in p:   # KST 날짜 → UTC 시각 경계
        where.append("c.not_after >= ?")
        args.append(_date(p["expiry_from"], "").astimezone(timezone.utc).isoformat())
    if "expiry_to" in p:
        where.append("c.not_after < ?")
        args.append((_date(p["expiry_to"], "") + timedelta(days=1)).astimezone(timezone.utc).isoformat())
    if "within_days" in p:   # 오늘(KST)부터 N일 이내 만료 (이미 만료 제외)
        end = (now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
               + timedelta(days=int(p["within_days"]) + 1))
        where.append("c.not_after > ? AND c.not_after < ?")
        args += [now.isoformat(), end.astimezone(timezone.utc).isoformat()]
    if "status" in p:        # 상태도 SQL 범위 조건으로 처리 (인덱스 사용)
        cond, a = _status_bounds(p["status"], now)
        where.append(cond)
        args += a
    # 1) 조건으로 인증서만 먼저 걸러 페이지를 자르고  2) 그 페이지에만 저장 위치를 붙인다
    inner = "SELECT c.* FROM certificate c"
    if where:
        inner += " WHERE " + " AND ".join(where)
    # 검색어 조건은 전체 스캔 후 정렬이 빠르고(인덱스 순서로 읽으면 행마다 임의 접근), 범위 조건은 인덱스 순서가 빠르다
    inner += (" ORDER BY +c.not_after" if "q" in p else " ORDER BY c.not_after") + " LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 5000)), max(0, int(offset))]
    sql = (f"WITH page AS ({inner}) SELECT page.*, "
           "(SELECT group_concat(file_path, ' | ') FROM cert_location l WHERE l.cert_id = page.cert_id) AS locations, "
           "(SELECT count(*) FROM cert_location l WHERE l.cert_id = page.cert_id) AS location_count "
           "FROM page ORDER BY page.not_after")
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["status"], d["days_left"] = status_of(d["not_before"], d["not_after"], now)
        d["issued_date"] = datetime.fromisoformat(d["not_before"]).astimezone(KST).strftime("%Y-%m-%d")
        d["expiry_date"] = datetime.fromisoformat(d["not_after"]).astimezone(KST).strftime("%Y-%m-%d")
        rows.append(d)
    return rows


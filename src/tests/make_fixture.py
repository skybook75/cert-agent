"""E2E·KPI 테스트용 데이터 생성.

  python tests/make_fixture.py <dest>            → <dest>/pem (스캔 폴더), <dest>/관리대장.xlsx
  python tests/make_fixture.py <dest> --renew pay.example.co.kr 400  → 갱신본 1개 추가
  python tests/make_fixture.py <dest> --bulk 100 → 인증서 100개(PEM·DER·PFX 혼합, 만료일 분포) + 기대 판정 JSON

운영 세트(tests/fixtures/운영)는 공개 인증서 5개, 검증 세트는 오늘 기준 만료일을 갖는 테스트 인증서다.
"""
import json
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

def _na(c):  # cryptography 42+ / 이전 버전 모두 지원
    from datetime import timezone as _tz
    return c.not_valid_after_utc if hasattr(c, "not_valid_after_utc") else c.not_valid_after.replace(tzinfo=_tz.utc)



HERE = Path(__file__).resolve().parent
PFX_PASSWORD = b"Test#1234"      # 테스트 전용
CASES = [  # 파일명, CN, 잔여일, 형식, 키
    ("web-normal.crt", "portal.example.co.kr", 200, "pem", "rsa"),
    ("api-d30.pem", "api.example.co.kr", 25, "pem", "ec"),
    ("pay-d7.pfx", "pay.example.co.kr", 5, "pfx", "rsa"),
    ("legacy-expired.cer", "legacy.example.co.kr", -12, "der", "rsa"),
    ("signCert.der", "홍길동()0012345678", 60, "der", "rsa"),
]
_KEYS = {}


def key_for(kt):
    if kt not in _KEYS:
        _KEYS[kt] = rsa.generate_private_key(65537, 2048) if kt == "rsa" else ec.generate_private_key(ec.SECP256R1())
    return _KEYS[kt]


def make(cn, days_left, kt="rsa", issuer_cn="PoC Test CA"):
    key = key_for(kt)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SK AX PoC"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"), x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=365))
         .not_valid_after(now + timedelta(days=days_left, hours=12)))
    b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]) if "." in cn and cn.isascii()
                        else x509.BasicConstraints(ca=False, path_length=None), critical=False)
    return key, b.sign(key, hashes.SHA256())


def write(path: Path, key, cert, fmt):
    if fmt == "pem":
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    elif fmt == "der":
        path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    else:
        path.write_bytes(pkcs12.serialize_key_and_certificates(b"cert", key, cert, None, serialization.BestAvailableEncryption(PFX_PASSWORD)))


def build(dest: Path):
    pem = dest / "pem"
    if pem.exists():
        shutil.rmtree(pem)
    (pem / "운영").mkdir(parents=True)
    for f in (HERE / "fixtures" / "운영").iterdir():
        shutil.copy2(f, pem / "운영" / f.name)
    v = pem / "검증"
    v.mkdir()
    made = {}
    for fname, cn, days, fmt, kt in CASES:
        key, cert = make(cn, days, kt)
        write(v / fname, key, cert, fmt)
        made[cn] = cert
    (v / "broken.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\nTk9UIEEgQ0VSVA==\n-----END CERTIFICATE-----\n")
    (v / "only-key.pem").write_text("-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIB...\n-----END ENCRYPTED PRIVATE KEY-----\n")
    (v / "backup").mkdir()
    shutil.copyfile(v / "api-d30.pem", v / "backup" / "api-d30_old.pem")
    # 기존 관리대장: 일치 2(portal·legacy) / 불일치 1(api 만료일 오기재) / 미수집 1(old-erp) / 오류 2(날짜·담당자 누락)
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "관리대장"
    ws.append(["인증서명(CN)", "발급기관", "만료일", "관리담당자", "담당자 이메일", "업무시스템", "일련번호", "갱신상태"])
    exp = lambda cn, plus=0: (_na(made[cn]) + timedelta(hours=9) + timedelta(days=plus)).date()   # noqa: E731
    ws.append(["portal.example.co.kr", "PoC Test CA", exp("portal.example.co.kr"), "김담당", "kim@example.co.kr", "포털",
               format(made["portal.example.co.kr"].serial_number, "X"), "미조치"])
    ws.append(["api.example.co.kr", "PoC Test CA", exp("api.example.co.kr", 365), "이연동", "lee@example.co.kr", "대외연동", None, "예정"])
    ws.append(["legacy.example.co.kr", "PoC Test CA", exp("legacy.example.co.kr").strftime("%Y.%m.%d"), "김담당", "kim@example.co.kr", "레거시", None, None])
    ws.append(["old-erp.example.co.kr", "Sectigo RSA Domain Validation Secure Server CA", "2026-12-31", "박담당", "park.d@example.co.kr", "ERP", "0A1B2C3D4E5F", None])
    ws.append(["pay.example.co.kr", "PoC Test CA", "2026-13-01", "박결제", "pay@example.co.kr", "결제", None, None])
    ws.append(["cdn.example.co.kr", "DigiCert", "2027-03-01", None, "cdn@example.co.kr", "CDN", None, None])
    wb.save(dest / "관리대장.xlsx")
    print(json.dumps({"pem": str(pem), "ledger": str(dest / "관리대장.xlsx"), "files": sum(1 for p in pem.rglob("*") if p.is_file())}, ensure_ascii=False))


def renew(dest: Path, cn: str, days: int):
    key, cert = make(cn, days, "rsa")
    p = dest / "pem" / "검증" / f"{cn.split('.')[0]}-renewed.crt"
    write(p, key, cert, "pem")
    print(json.dumps({"file": str(p), "not_after": _na(cert).isoformat()}))


def bulk(dest: Path, n: int):
    """KPI 측정용: n개 인증서, 만료일 분포(-30~400일), 형식 혼합. 기대 판정을 함께 저장."""
    random.seed(7)
    root = dest / "bulk"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    expected = []
    days_pool = [-30, -1, 0, 1, 3, 6, 7, 8, 15, 29, 30, 31, 45, 90, 200, 400]
    for i in range(n):
        days = days_pool[i % len(days_pool)] if i < len(days_pool) * 2 else random.randint(-30, 400)
        fmt = ["pem", "der", "pfx", "pem"][i % 4]
        cn = f"svc{i:03d}.example.co.kr"
        key, cert = make(cn, days, "ec" if i % 3 == 0 else "rsa", issuer_cn=["PoC Test CA", "Sectigo RSA DV CA", "DigiCert G2 CA"][i % 3])
        ext = {"pem": "pem", "der": "cer", "pfx": "pfx"}[fmt]
        sub = root / f"server{i % 10:02d}"
        sub.mkdir(exist_ok=True)
        write(sub / f"{cn}.{ext}", key, cert, fmt)
        expected.append({"cn": cn, "days": days, "not_after": _na(cert).isoformat(), "fmt": fmt})
    (dest / "bulk_expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"dir": str(root), "count": n}))


if __name__ == "__main__":
    d = Path(sys.argv[1])
    d.mkdir(parents=True, exist_ok=True)
    if "--renew" in sys.argv:
        i = sys.argv.index("--renew")
        renew(d, sys.argv[i + 1], int(sys.argv[i + 2]))
    elif "--bulk" in sys.argv:
        bulk(d, int(sys.argv[sys.argv.index("--bulk") + 1]))
    else:
        build(d)

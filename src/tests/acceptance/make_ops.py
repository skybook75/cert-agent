"""인수 테스트용 '운영 시뮬레이션' 데이터.

서버 4대(nginx·Apache·Tomcat·IIS)를 흉내 낸 폴더에 실제 형식의 인증서를 두고,
담당자가 손으로 관리해 온 엑셀 관리대장(오류 포함본·수정본)과 정답표(expected.json)를 만든다.
발급기관 이름은 실제 CA 이름을 흉내 낸 테스트용이며, 모두 이 스크립트가 만든 테스트 키로 서명한다.

  python tests/acceptance/make_ops.py <dest>            → <dest>/servers, <dest>/관리대장_*.xlsx, <dest>/expected.json
  python tests/acceptance/make_ops.py <dest> --renew   → www 갱신본을 web02 에 배치 (갱신 시나리오)
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

NOW = datetime.now(timezone.utc)
KST = timezone(timedelta(hours=9))
PW = {"tomcat": "Tomcat!2026", "iis_portal": "Iis#Portal9", "iis_hr": "Hr-Unknown-77"}   # 테스트 전용 비밀번호


def name(cn, org="SK AX Demo"):
    return x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


CA_KEY = rsa.generate_private_key(65537, 2048)


def ca_cert(cn):
    return (x509.CertificateBuilder().subject_name(name(cn, "Demo Trust (TEST)")).issuer_name(name("Demo Root CA (TEST)", "Demo Trust (TEST)"))
            .public_key(CA_KEY.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=900)).not_valid_after(NOW + timedelta(days=2000))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(CA_KEY, hashes.SHA256()))


def leaf(cn, issuer_cn, days_left, kt="rsa", sans=None, life=365):
    key = rsa.generate_private_key(65537, 2048) if kt == "rsa" else ec.generate_private_key(ec.SECP256R1())
    na = NOW + timedelta(days=days_left, hours=6)
    b = (x509.CertificateBuilder().subject_name(name(cn)).issuer_name(name(issuer_cn, "Demo Trust (TEST)")).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(na - timedelta(days=life)).not_valid_after(na))
    if sans is not False:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in (sans or [cn])]), critical=False)
    return key, b.sign(CA_KEY, hashes.SHA256())


def pem(c):
    return c.public_bytes(serialization.Encoding.PEM)


def p12(key, cert, pw, extra=()):
    return pkcs12.serialize_key_and_certificates(b"server", key, cert, list(extra) or None, serialization.BestAvailableEncryption(pw.encode()))


def main(dest: Path, renew=False):
    S = dest / "servers"
    if renew:
        k, c = leaf("www.skax-demo.co.kr", "Sectigo RSA Domain Validation Secure Server CA", 395, sans=["www.skax-demo.co.kr", "skax-demo.co.kr"], life=396)
        (S / "web02-apache/etc/httpd/ssl/www.skax-demo.co.kr_2027.crt").write_bytes(pem(c))
        print(json.dumps({"renewed": "www.skax-demo.co.kr", "not_after": c.not_valid_after_utc.isoformat()}))
        return
    shutil.rmtree(dest, ignore_errors=True)
    inter = ca_cert("DigiCert Global G2 TLS RSA SHA256 2020 CA1")
    exp = []

    def put(path, data, **meta):
        p = S / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        exp.append({"path": path, **meta})

    # web01: nginx (PEM 체인 + 개인키 파일 + 백업 확장자)
    k, c = leaf("shop.skax-demo.co.kr", "DigiCert Global G2 TLS RSA SHA256 2020 CA1", 210, sans=["shop.skax-demo.co.kr", "m.shop.skax-demo.co.kr"])
    put("web01-nginx/etc/nginx/ssl/shop.skax-demo.co.kr/fullchain.pem", pem(c) + pem(inter), kind="leaf+chain", cn=["shop.skax-demo.co.kr", "DigiCert Global G2 TLS RSA SHA256 2020 CA1"], days=210)
    put("web01-nginx/etc/nginx/ssl/shop.skax-demo.co.kr/privkey.pem", k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), kind="key-only")
    k2, c2 = leaf("api.skax-demo.co.kr", "R11", 20, kt="ec", life=90)
    put("web01-nginx/etc/letsencrypt/live/api.skax-demo.co.kr/cert.pem", pem(c2), kind="leaf", cn=["api.skax-demo.co.kr"], days=20)
    put("web01-nginx/etc/letsencrypt/live/api.skax-demo.co.kr/cert.pem.bak", pem(c2), kind="ignored-ext")
    # web02: Apache (D-7, 이전 버전 사본, 와일드카드 DER, 손상 파일)
    k3, c3 = leaf("www.skax-demo.co.kr", "Sectigo RSA Domain Validation Secure Server CA", 6, sans=["www.skax-demo.co.kr", "skax-demo.co.kr"])
    put("web02-apache/etc/httpd/ssl/www.skax-demo.co.kr.crt", pem(c3), kind="leaf", cn=["www.skax-demo.co.kr"], days=6)
    k4, c4 = leaf("www.skax-demo.co.kr", "Sectigo RSA Domain Validation Secure Server CA", -359, sans=["www.skax-demo.co.kr"])
    put("web02-apache/etc/httpd/ssl/old/www.skax-demo.co.kr_2025.crt", pem(c4), kind="old-version", cn=["www.skax-demo.co.kr"], days=-359)
    k5, c5 = leaf("*.skax-demo.co.kr", "GlobalSign GCC R3 DV TLS CA 2020", 380, sans=["*.skax-demo.co.kr", "skax-demo.co.kr"])
    put("web02-apache/etc/httpd/ssl/wildcard_skax-demo.cer", c5.public_bytes(serialization.Encoding.DER), kind="leaf-der", cn=["*.skax-demo.co.kr"], days=380)
    put("web02-apache/etc/httpd/ssl/corrupted.crt", b"-----BEGIN CERTIFICATE-----\nTUlJQ2Jyb2tlbg==\n-----END CERTIFICATE-----\n", kind="broken")
    # was01: Tomcat (PKCS12, 같은 비밀번호 2개 → 폴더 범위 등록)
    k6, c6 = leaf("pay.skax-demo.co.kr", "DigiCert Global G2 TLS RSA SHA256 2020 CA1", 45)
    put("was01-tomcat/opt/tomcat/conf/ssl/pay.skax-demo.co.kr.p12", p12(k6, c6, PW["tomcat"], [inter]), kind="pfx", cn=["pay.skax-demo.co.kr", "DigiCert Global G2 TLS RSA SHA256 2020 CA1"], days=45, password="tomcat")
    k7, c7 = leaf("erp.skax-demo.co.kr", "DigiCert Global G2 TLS RSA SHA256 2020 CA1", -3)
    put("was01-tomcat/opt/tomcat/conf/ssl/erp.skax-demo.co.kr.p12", p12(k7, c7, PW["tomcat"]), kind="pfx", cn=["erp.skax-demo.co.kr"], days=-3, password="tomcat")
    # win01: IIS (PFX 비밀번호 서로 다름, 하나는 끝내 모름) + 공동인증서
    k8, c8 = leaf("portal.skax-demo.co.kr", "GlobalSign GCC R3 DV TLS CA 2020", 15)
    put("win01-iis/C/inetpub/certs/portal.pfx", p12(k8, c8, PW["iis_portal"]), kind="pfx", cn=["portal.skax-demo.co.kr"], days=15, password="iis_portal")
    k9, c9 = leaf("hr.skax-demo.co.kr", "GlobalSign GCC R3 DV TLS CA 2020", 120)
    put("win01-iis/C/inetpub/certs/hr.pfx", p12(k9, c9, PW["iis_hr"]), kind="pfx-unknown-pw", cn=["hr.skax-demo.co.kr"], days=120, password="iis_hr")
    k10, c10 = leaf("홍길동()0012345678", "yessignCA Class 2", 90, sans=False)
    put("win01-iis/Users/admin/NPKI/yessign/USER/signCert.der", c10.public_bytes(serialization.Encoding.DER), kind="leaf-der", cn=["홍길동()0012345678"], days=90)

    # 엑셀 관리대장 (담당자가 손으로 관리해 온 모습: 날짜 형식 제각각·오타·누락·중복·이미 없는 서버)
    def kdate(c, shift=0):
        return (c.not_valid_after_utc.astimezone(KST) + timedelta(days=shift)).strftime("%Y-%m-%d")
    head = ["No", "인증서명(CN)", "발급기관", "만료일", "관리담당자", "담당자 이메일", "업무시스템", "일련번호", "갱신상태", "비고"]
    rows = [
        [1, "shop.skax-demo.co.kr", "DigiCert", kdate(c), "김민수", "minsu.kim@skax-demo.co.kr", "쇼핑몰", format(c.serial_number, "X"), "미조치", ""],
        [2, "api.skax-demo.co.kr", "Let's Encrypt", kdate(c2, 61), "이서연", "seoyeon.lee@skax-demo.co.kr", "오픈API", "", "미조치", "지난번 갱신 후 날짜 안 고침"],
        [3, "www.skax-demo.co.kr", "Sectigo", kdate(c3).replace("-", "."), "박지훈", "jihoon.park@skax-demo.co.kr", "대표홈페이지", "", "예정", "점(.) 날짜"],
        [4, "*.skax-demo.co.kr", "GlobalSign", kdate(c5), "박지훈", "jihoon.park@skax-demo.co.kr", "공통", "", "미조치", ""],
        [5, "pay.skax-demo.co.kr", "DigiCert", kdate(c6), "이서연", "seoyeon.lee@skax-demo.co.kr", "결제", "", "미조치", ""],
        [6, "erp.skax-demo.co.kr", "DigiCert", kdate(c7), "", "", "ERP", "", "미조치", "담당자 퇴사"],
        [7, "portal.skax-demo.co.kr", "GlobalSign", "2026-13-15", "김민수", "minsu.kim@skax-demo.co.kr", "그룹포털", "", "미조치", "오타"],
        [8, "hr.skax-demo.co.kr", "GlobalSign", kdate(c9), "최유진", "yujin.choi@skax-demo", "인사", "", "미조치", "이메일 오타"],
        [9, "old-gw.skax-demo.co.kr", "Thawte", "2026-12-01", "김민수", "minsu.kim@skax-demo.co.kr", "구 그룹웨어", "", "미조치", "서버 폐기됨(대장 미정리)"],
        [10, "shop.skax-demo.co.kr", "DigiCert", kdate(c), "김민수", "minsu.kim@skax-demo.co.kr", "쇼핑몰", format(c.serial_number, "X"), "미조치", "복사 실수(중복)"],
    ]
    def save(fname, rws):
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "인증서 관리대장"
        ws.append(["인증서 관리대장 (2026년 하반기)"]); ws.append([])
        ws.append(head)
        for r in rws:
            ws.append(r)
        for col, w in zip("ABCDEFGHIJ", (5, 26, 14, 12, 10, 28, 14, 22, 10, 26)):
            ws.column_dimensions[col].width = w
        wb.save(dest / fname)
    save("관리대장_수기_원본.xlsx", rows)
    fixed = [list(r) for r in rows]
    fixed[5][4], fixed[5][5], fixed[5][9] = "정하늘", "haneul.jung@skax-demo.co.kr", "담당자 변경"
    fixed[6][3], fixed[6][9] = kdate(c8), "오타 수정"
    fixed[7][5], fixed[7][9] = "yujin.choi@skax-demo.co.kr", "이메일 수정"
    fixed = fixed[:9]   # 중복 행 삭제
    save("관리대장_수정본.xlsx", fixed)
    (dest / "expected.json").write_text(json.dumps({"generated_at": NOW.isoformat(), "files": exp, "passwords_note": "테스트 전용 비밀번호는 make_ops.py PW 참고"}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"files": len(exp), "dest": str(dest)}, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]), "--renew" in sys.argv)

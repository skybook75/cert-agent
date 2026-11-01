#!/usr/bin/env python3
"""AWS 테스트 서버용 '테스트 전용' 인증서 만들기 (실제 회사 인증서 아님, 사설 테스트 CA 로 서명)

  python3 lab_certs.py web01 init      nginx: shop.demo.test(D-6, 체인+키), api.demo.test(D-25, DER), old-gw(만료)
  python3 lab_certs.py web01 renew     같은 키로 새 인증서(400일) → /etc/nginx/ssl/incoming/  (갱신 시나리오)
  python3 lab_certs.py web01 badrenew  키가 다른 인증서 → incoming/  (배포 실패·자동 원복 시나리오)
  python3 lab_certs.py was01 init      Tomcat: pay.p12(비밀번호 보호, D-40), batch.cer(D-120)
  python3 lab_certs.py win01 init      IIS: C:\\certs\\portal.pfx(D-20) · intranet.cer(만료) — 부트스트랩이 저장소에 넣고 443 에 바인딩
테스트 PFX 비밀번호는 이 파일에만 있는 '테스트 값'이며, 플랫폼에는 담당자가 보관함 화면에서 직접 입력한다.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

TEST_PW = {"pay": "Tomcat!2026", "portal": "Iis#Portal9"}
NOW = datetime.now(timezone.utc)
WIN = os.name == "nt"
LAB = Path(r"C:\cert-lab" if WIN else "/opt/cert-lab")


def nm(cn, o="Cert Lab (TEST)"):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, o)])


def key_at(p: Path):
    if p.exists():
        return serialization.load_pem_private_key(p.read_bytes(), None)
    k = rsa.generate_private_key(65537, 2048)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return k


CA_K = key_at(LAB / "ca" / "ca.key")
CA = (x509.CertificateBuilder().subject_name(nm("Cert Lab TLS CA (TEST)")).issuer_name(nm("Cert Lab TLS CA (TEST)")).public_key(CA_K.public_key())
      .serial_number(1).not_valid_before(datetime(2026, 1, 1, tzinfo=timezone.utc)).not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(CA_K, hashes.SHA256()))


def leaf(cn, key, days_left, life=365):
    na = NOW + timedelta(days=days_left)
    nb = min(NOW - timedelta(hours=1), na - timedelta(days=life))
    return (x509.CertificateBuilder().subject_name(nm(cn)).issuer_name(CA.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False).sign(CA_K, hashes.SHA256()))


pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
der = lambda c: c.public_bytes(serialization.Encoding.DER)
fp = lambda c: c.fingerprint(hashes.SHA256()).hex(":").upper()


def write(p: Path, data, mode=0o644):
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
    try:
        os.chmod(p, mode)
    except OSError:
        pass


def main():
    role, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "init")
    out = {}
    if role == "web01":
        ssl = Path("/etc/nginx/ssl")
        k = key_at(ssl / "shop" / "privkey.key")
        if mode == "init":
            c = leaf("shop.demo.test", k, 6)
            write(ssl / "shop" / "fullchain.pem", pem(c) + pem(CA))
            ka = key_at(ssl / "api" / "api.key")
            write(ssl / "api" / "api.crt", der(leaf("api.demo.test", ka, 25)))
            write(ssl / "old" / "old-gw.pem", pem(leaf("old-gw.demo.test", rsa.generate_private_key(65537, 2048), -3)))
            out = {"shop": fp(c)}
        elif mode in ("renew", "badrenew"):
            k2 = k if mode == "renew" else rsa.generate_private_key(65537, 2048)
            c = leaf("shop.demo.test", k2, 397, life=398)
            write(ssl / "incoming" / f"shop_{mode}.pem", pem(c))
            out = {mode: fp(c)}
    elif role == "was01":
        d = Path("/opt/tomcat/conf/ssl")
        kp = rsa.generate_private_key(65537, 2048)
        c = leaf("pay.demo.test", kp, 40)
        write(d / "pay.p12", pkcs12.serialize_key_and_certificates(b"pay", kp, c, [CA], serialization.BestAvailableEncryption(TEST_PW["pay"].encode())), 0o600)
        write(d / "batch.cer", der(leaf("batch.demo.test", rsa.generate_private_key(65537, 2048), 120)))
        out = {"pay": fp(c)}
    elif role == "win01":
        d = Path(r"C:\certs")
        kp = rsa.generate_private_key(65537, 2048)
        c = leaf("portal.demo.test", kp, 20)
        write(d / "portal.pfx", pkcs12.serialize_key_and_certificates(b"portal", kp, c, [CA], serialization.BestAvailableEncryption(TEST_PW["portal"].encode())))
        write(d / "intranet.cer", der(leaf("intranet.demo.test", rsa.generate_private_key(65537, 2048), -10)))
        write(LAB / "ca.cer", der(CA))
        out = {"portal": fp(c)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()

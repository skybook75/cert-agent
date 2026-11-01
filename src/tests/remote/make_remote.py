"""원격 Agent 테스트 데이터: web01(nginx 흉내, 체인+키), was01(PFX). --renew/--badrenew 로 갱신 파일 배치."""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
W = Path(sys.argv[1]); NOW = datetime.now(timezone.utc)
def nm(cn): return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
def load_or(p, gen):
    if p.exists(): return serialization.load_pem_private_key(p.read_bytes(), None)
    k = gen(); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())); return k
ca_k = load_or(W / "ca" / "ca.key", lambda: rsa.generate_private_key(65537, 2048))
def cert(cn, key, days, issuer="Demo TLS CA (TEST)", life=None):
    na = NOW + timedelta(days=days); nb = NOW - timedelta(days=1) if life is None else na - timedelta(days=life)
    return (x509.CertificateBuilder().subject_name(nm(cn)).issuer_name(nm(issuer)).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(nb).not_valid_after(na).add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False).sign(ca_k, hashes.SHA256()))
inter = (x509.CertificateBuilder().subject_name(nm("Demo TLS CA (TEST)")).issuer_name(nm("Demo Root (TEST)")).public_key(ca_k.public_key()).serial_number(1)
         .not_valid_before(NOW - timedelta(days=900)).not_valid_after(NOW + timedelta(days=900)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(ca_k, hashes.SHA256()))
pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
ssl_dir = W / "web01" / "etc/nginx/ssl/shop"
key = load_or(ssl_dir / "privkey.key", lambda: rsa.generate_private_key(65537, 2048))
mode = sys.argv[2] if len(sys.argv) > 2 else "init"
if mode == "init":
    c = cert("shop.demo.test", key, 6, life=360)
    (ssl_dir / "fullchain.pem").write_text(pem(c) + pem(inter))
    pk = rsa.generate_private_key(65537, 2048); pc = cert("pay.demo.test", pk, 40)
    p = W / "was01/opt/tomcat/conf/ssl"; p.mkdir(parents=True, exist_ok=True)
    (p / "pay.p12").write_bytes(pkcs12.serialize_key_and_certificates(b"pay", pk, pc, None, serialization.BestAvailableEncryption(b"Tomcat!2026")))
    print(json.dumps({"shop": c.fingerprint(hashes.SHA256()).hex(":").upper()}))
elif mode in ("renew", "badrenew"):
    k2 = key if mode == "renew" else rsa.generate_private_key(65537, 2048)     # badrenew: 서버 키와 짝이 맞지 않는 인증서
    c = cert("shop.demo.test", k2, 400 if mode == "renew" else 420, life=None)
    inc = W / "web01" / "etc/nginx/ssl/incoming"; inc.mkdir(parents=True, exist_ok=True)
    (inc / f"shop_{mode}.pem").write_text(pem(c))
    print(json.dumps({mode: c.fingerprint(hashes.SHA256()).hex(":").upper()}))

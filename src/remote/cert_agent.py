#!/usr/bin/env python3
"""인증서 관리 플랫폼 — 서버 Agent (Linux · Windows 공용, Python 3.9+ · cryptography)

  수집  : python3 cert_agent.py collect            지정 경로를 스캔해 인증서(공개 부분)만 플랫폼에 올림
  배포  : python3 cert_agent.py deploy             승인된 배포를 받아 설치 → 설정 검사 → 재적용 → 실제 응답 확인, 실패 시 자동 원복
  둘 다 : python3 cert_agent.py run                (작업 스케줄러·cron·systemd timer 용)

설정 파일(/etc/cert-agent/agent.json, Windows: C:\\ProgramData\\cert-agent\\agent.json)
  {"platform": "http://10.0.1.10:8000", "token": "...", "scan": ["/etc/nginx/ssl", "/etc/letsencrypt/live"],
   "service": {"check": "nginx -t", "reload": "systemctl reload nginx", "verify": "127.0.0.1:443"},
   "winstore": ["LocalMachine\\My"]}   # Windows 만: 인증서 저장소도 수집

보안
  - 개인키·PFX 파일은 보내지 않음. 보내는 것은 인증서(공개) DER 와 경로뿐
  - PFX 비밀번호는 플랫폼 보관함에서 이 Agent 의 공개키(RSA-OAEP)로 감싸 받은 뒤 메모리에서만 풂(디스크·로그 기록 없음)
  - Agent 키쌍은 설정 폴더에 600 권한으로 보관, 토큰은 플랫폼에서 Agent 마다 따로 발급
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12

VERSION = "1.0"
try:                                             # Windows 콘솔(cp1252 등)에서도 한글 로그가 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
CONF_DIR = Path(os.environ.get("CERT_AGENT_HOME") or (r"C:\ProgramData\cert-agent" if os.name == "nt" else "/etc/cert-agent"))
EXTS = {".pem", ".crt", ".cer", ".der", ".pfx", ".p12"}
LOG = []


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    LOG.append(line)
    print(line, flush=True)


def conf():
    return json.loads((CONF_DIR / "agent.json").read_text(encoding="utf-8-sig"))


def call(c, method, path, body=None):
    req = urllib.request.Request(c["platform"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Agent " + c["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def keypair():
    kf = CONF_DIR / "agent_key.pem"
    if not kf.exists():
        k = rsa.generate_private_key(65537, 3072)
        kf.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            os.chmod(kf, 0o600)
        except OSError:
            pass
    k = serialization.load_pem_private_key(kf.read_bytes(), None)
    return k, k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def hello(c):
    _, pub = keypair()
    r = call(c, "POST", "/api/agent-api/hello", {"os": f"{platform.system()} {platform.release()}", "version": VERSION, "pubkey": pub})
    log(f"Plan: 등록 확인 → 호스트 {r['host']} · 배포 단계 {r.get('deploy_stage') or '없음'}")
    return r


def norm(p: Path) -> str:
    return str(p).replace("\\", "/")


def certs_in(path: Path, passwords):
    data = path.read_bytes()
    if path.suffix.lower() in (".pfx", ".p12"):
        for i, pw in enumerate([None] + passwords):
            try:
                _, cert, extra = pkcs12.load_key_and_certificates(data, pw.encode() if pw else None)
                return ([cert] if cert else []) + list(extra), (i - 1 if i else None)
            except ValueError:
                continue
        raise ValueError("비밀번호 필요")
    if b"-----BEGIN CERTIFICATE" in data:
        return x509.load_pem_x509_certificates(data), None
    if b"-----BEGIN" in data:
        raise ValueError("인증서가 아닌 PEM(개인키 등)")
    return [x509.load_der_x509_certificate(data)], None


def collect(c):
    roots = [Path(x) for x in c.get("scan", [])]
    files = [p for r in roots if r.exists() for p in sorted(r.rglob("*")) if p.is_file() and not p.is_symlink() and (p.suffix.lower() in EXTS or p.name.lower() == "signcert.der")]
    log(f"Plan: 스캔 {len(roots)}개 경로 → 대상 {len(files)}개 파일 → PFX 비밀번호 요청 → 파싱 → 업로드")
    pfx = [norm(p) for p in files if p.suffix.lower() in (".pfx", ".p12")]
    wrapped = {}
    if pfx:
        k, _ = keypair()
        for f in call(c, "POST", "/api/agent-api/secrets", {"files": pfx})["files"]:
            wrapped[f["path"]] = f["candidates"]
        log(f"보관함: PFX {len(pfx)}개 중 {sum(1 for v in wrapped.values() if v)}개에 비밀번호 후보(암호화 전달) 수신")
    out = []
    for p in files:
        item = {"path": norm(p)}
        cands = wrapped.get(norm(p), [])
        pws, ids = [], []
        if cands:
            k, _ = keypair()
            for cnd in cands:
                try:
                    pws.append(k.decrypt(base64.b64decode(cnd["wrapped"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=cnd["secret_id"].encode())).decode())
                    ids.append(cnd["secret_id"])
                except Exception:  # noqa: BLE001
                    pass
        try:
            certs, used = certs_in(p, pws)
            item["ders"] = [base64.b64encode(x.public_bytes(serialization.Encoding.DER)).decode() for x in certs]
            if used is not None:
                item["secret_id"] = ids[used]
        except Exception as e:  # noqa: BLE001
            msg = str(e) if str(e) else type(e).__name__
            item["error"] = f"비밀번호 불일치(보관함 {len(pws)}개 시도)" if msg == "비밀번호 필요" and pws else msg[:200]
        finally:
            pws.clear()
        out.append(item)
    for store in c.get("winstore", []):                  # Windows 인증서 저장소(예: LocalMachine\\My) — 공개 인증서만 읽음
        ps = f"Get-ChildItem Cert:\\{store} | ForEach-Object {{ $_.Thumbprint + ' ' + [Convert]::ToBase64String($_.RawData) }}"
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
        rows = [ln.split(" ", 1) for ln in p.stdout.splitlines() if " " in ln]
        log(f"Windows 인증서 저장소 {store}: {len(rows)}개")
        out += [{"path": f"Cert:/{store.replace(chr(92), '/')}/{tp}", "ders": [b64]} for tp, b64 in rows]
    r = call(c, "POST", "/api/agent-api/ingest", {"files": out})
    log(f"업로드: 파일 {r['files']}개 → 승인 대기 {r['new_pending']}건(갱신본 {r['renewal_candidates']}) · 이미 관리 중 {r['known']} · 실패 {r['failed']}")
    return r


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()[-400:]


def served_fp(hostport, sni):
    h, port = hostport.rsplit(":", 1)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection((h, int(port)), timeout=10) as s, ctx.wrap_socket(s, server_hostname=sni) as t:
        der = t.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der).fingerprint(hashes.SHA256()).hex(":").upper()


def deploy(c):
    tasks = call(c, "GET", "/api/agent-api/deploy-tasks")["tasks"]
    if not tasks:
        log("배포: 적용할 승인 배포 없음")
        return []
    svc = c.get("service", {})
    done = []
    for t in tasks:
        log(f"Plan: {t['subject_cn']} → {t['stage']} · 대상 파일 {len(t['targets'])}개 · 백업 → 교체 → 설정 검사 → 재적용 → 응답 확인 → (실패 시) 원복")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backups, ok, detail, fp = [], False, "", ""
        try:
            new_leaf = t["pem"].strip() + "\n"
            for tgt in t["targets"]:
                p = Path(tgt)
                if not p.exists() or p.suffix.lower() in (".pfx", ".p12"):
                    continue
                b = p.with_name(p.name + f".bak-{stamp}")
                shutil.copy2(p, b)
                backups.append((p, b))
                old = p.read_text(errors="ignore")
                # 체인 파일이면 맨 앞(서버 인증서)만 바꾸고 중간 인증서는 유지
                blocks = old.split("-----END CERTIFICATE-----")
                chain = "-----END CERTIFICATE-----".join(blocks[1:]).strip() if len(blocks) > 2 else ""
                p.write_text(new_leaf + (chain + "\n" if chain else ""))
                log(f"교체: {p} (백업 {b.name})")
            if not backups:
                raise RuntimeError("교체할 PEM 파일이 없음")
            if svc.get("check"):
                rc, outp = sh(svc["check"])
                log(f"설정 검사: {svc['check']} → {'OK' if rc == 0 else '실패'} {outp[-120:]}")
                if rc:
                    raise RuntimeError(f"설정 검사 실패: {outp[-200:]}")
            if svc.get("reload"):
                rc, outp = sh(svc["reload"])
                if rc:
                    raise RuntimeError(f"재적용 실패: {outp[-200:]}")
                log(f"재적용: {svc['reload']} → OK")
                time.sleep(1)
            if svc.get("verify"):
                fp = served_fp(svc["verify"], t["subject_cn"].lstrip("*."))
                log(f"응답 확인: {svc['verify']} 가 보내는 인증서 지문 {fp[:23]}… {'= 배포본' if fp == t['fingerprint'] else '≠ 배포본'}")
                if fp != t["fingerprint"]:
                    raise RuntimeError("서버가 보내는 인증서가 배포본과 다름")
            ok, detail = True, f"설치·검사·재적용·응답 확인 완료 ({len(backups)}개 파일)"
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:300]
            log(f"Self-Correction: 실패 감지({detail[:80]}) → 백업으로 자동 원복")
            for p, b in backups:
                shutil.copy2(b, p)
            if backups and svc.get("reload"):
                sh(svc["reload"])
            detail += " → 백업 파일로 자동 원복"
        call(c, "POST", "/api/agent-api/deploy-result", {"env_id": t["env_id"], "cert_id": t["cert_id"], "ok": ok, "detail": detail,
                                                       "served_fingerprint": fp, "rolled_back": not ok})
        log(f"결과 보고: {'성공' if ok else '실패'} · {detail}")
        done.append({"task": t["env_id"], "ok": ok})
    return done


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    c = conf()
    hello(c)
    if cmd in ("collect", "run"):
        collect(c)
    if cmd in ("deploy", "run"):
        deploy(c)


if __name__ == "__main__":
    main()

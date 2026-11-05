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
import re
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



def _load_pem_certs(data: bytes):
    """여러 인증서가 든 PEM — cryptography 39 미만에는 load_pem_x509_certificates 가 없어 직접 나눈다."""
    if hasattr(x509, "load_pem_x509_certificates"):
        return x509.load_pem_x509_certificates(data)
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", data, re.S)
    if not blocks:
        raise ValueError("PEM 인증서가 없음")
    return [x509.load_pem_x509_certificate(b) for b in blocks]

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
    log(f"plan: register host={r['host']} deploy_stage={r.get('deploy_stage') or 'none'}")
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
        return _load_pem_certs(data), None
    if b"-----BEGIN" in data:
        raise ValueError("인증서가 아닌 PEM(개인키 등)")
    return [x509.load_der_x509_certificate(data)], None


def collect(c, only_paths=None, target_ids=None):
    """only_paths: '수집 실행' 요청으로 받은 경로만. 없으면 로컬 설정 + 플랫폼에 등록된 이 서버의 경로 전체."""
    if only_paths is None:
        try:
            cfg = call(c, "GET", "/api/agent-api/config")
            plat, target_ids = cfg.get("scan", []), cfg.get("targets", [])
        except Exception:  # noqa: BLE001
            plat = []
        paths = list(dict.fromkeys(list(c.get("scan", [])) + plat))
        if plat:
            log(f"config: {len(plat)} scan path(s) from platform")
    else:
        paths = only_paths
    roots = [Path(x) for x in paths]
    files = [p for r in roots if r.exists() for p in sorted(r.rglob("*")) if p.is_file() and not p.is_symlink() and (p.suffix.lower() in EXTS or p.name.lower() == "signcert.der")]
    log(f"scan: paths={len(roots)} files={len(files)} -> request pfx secrets -> parse -> upload")
    pfx = [norm(p) for p in files if p.suffix.lower() in (".pfx", ".p12")]
    wrapped = {}
    if pfx:
        k, _ = keypair()
        for f in call(c, "POST", "/api/agent-api/secrets", {"files": pfx})["files"]:
            wrapped[f["path"]] = f["candidates"]
        log(f"vault: received wrapped secrets for {sum(1 for v in wrapped.values() if v)}/{len(pfx)} pfx file(s) (RSA-OAEP)")
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
        log(f"certstore: {store} entries={len(rows)}")
        out += [{"path": f"Cert:/{store.replace(chr(92), '/')}/{tp}", "ders": [b64]} for tp, b64 in rows]
    r = call(c, "POST", "/api/agent-api/ingest", {"files": out, "target_ids": target_ids or []})
    log(f"upload: files={r['files']} pending={r['new_pending']} renewal={r['renewal_candidates']} known={r['known']} failed={r['failed']}")
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
        t.sendall(f"HEAD / HTTP/1.1\r\nHost: {sni}\r\nUser-Agent: cert-agent/1.0 (verify)\r\nConnection: close\r\n\r\n".encode())
        try:
            status = t.recv(64).split(b"\r\n", 1)[0].split(b" ")[1].decode()     # 실제 HTTP 응답 코드(웹서버 접근 로그에도 남음)
        except (OSError, IndexError):
            status = "-"
    return x509.load_der_x509_certificate(der).fingerprint(hashes.SHA256()).hex(":").upper(), status


def deploy(c):
    tasks = call(c, "GET", "/api/agent-api/deploy-tasks")["tasks"]
    if not tasks:
        log("deploy: no approved task")
        return []
    svc = c.get("service", {})
    done = []
    for t in tasks:
        log(f"deploy: task cn={t['subject_cn']} stage={t['stage']} files={len(t['targets'])} plan=backup,replace,configtest,reload,verify,rollback-on-fail")
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
                log(f"replace: {p} (backup {b.name})")
            if not backups:
                raise RuntimeError("교체할 PEM 파일이 없음")
            if svc.get("check"):
                rc, outp = sh(svc["check"])
                log(f"configtest: {svc['check']} rc={rc}")
                for ln in outp.splitlines()[-2:]:
                    log(f"  {ln}")
                if rc:
                    raise RuntimeError(f"설정 검사 실패: {outp[-200:]}")
            if svc.get("reload"):
                rc, outp = sh(svc["reload"])
                if rc:
                    raise RuntimeError(f"재적용 실패: {outp[-200:]}")
                log(f"reload: {svc['reload']} rc=0")
                time.sleep(1)
            if svc.get("verify"):
                fp, status = served_fp(svc["verify"], t["subject_cn"].lstrip("*."))
                log(f"verify: {svc['verify']} sni={t['subject_cn'].lstrip('*.')} http={status} served_sha256={fp[:23]}... {'MATCH' if fp == t['fingerprint'] else 'MISMATCH'}")
                if fp != t["fingerprint"]:
                    raise RuntimeError("서버가 보내는 인증서가 배포본과 다름")
            ok, detail = True, f"설치·검사·재적용·응답 확인 완료 ({len(backups)}개 파일)"
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:300]
            log(f"rollback: failure detected -> restoring {len(backups)} backup(s) and reloading")
            for p, b in backups:
                shutil.copy2(b, p)
            if backups and svc.get("reload"):
                sh(svc["reload"])
            detail += " → 백업 파일로 자동 원복"
        call(c, "POST", "/api/agent-api/deploy-result", {"env_id": t["env_id"], "cert_id": t["cert_id"], "ok": ok, "detail": detail,
                                                       "served_fingerprint": fp, "rolled_back": not ok})
        log(f"report: env={t['env_id']} ok={str(ok).lower()} rolled_back={str(not ok).lower()}")
        done.append({"task": t["env_id"], "ok": ok})
    return done


def poll(c):
    """1분마다: 플랫폼의 '수집 실행' 요청과 승인된 배포만 확인(가벼움)."""
    tasks = call(c, "GET", "/api/agent-api/collect-tasks").get("tasks", [])
    for t in tasks:
        log(f"collect: run requested target={t['target_id']} paths={len(t['paths'])}")
        collect(c, t["paths"], [t["target_id"]])
    deploy(c)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    c = conf()
    if cmd == "poll":
        return poll(c)
    hello(c)
    if cmd in ("collect", "run"):
        collect(c)
    if cmd in ("deploy", "run"):
        deploy(c)


if __name__ == "__main__":
    main()

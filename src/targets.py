"""수집 대상 서버 목록 — 서버·접속정보(비밀번호는 AES-256-GCM 암호화)·경로를 등록해 두고 수집 Agent 가 활용

  방식 agent : 그 서버의 서버 Agent 가 플랫폼에서 경로 목록을 받아 스캔(로컬 설정 불필요). '수집 실행' 은 요청을 남기고
               Agent 가 1분 안에 가져가 실행(poll).
  방식 ssh   : 에이전트 설치 없이 플랫폼이 SSH(SFTP)로 접속해 인증서 파일만 읽어 와 파싱(개인키·PFX 파일은 플랫폼 메모리에서만 처리,
               저장하지 않음). 비밀번호는 실행 순간에만 복호화.
권한: 관리자는 전체, 담당자는 자기가 등록한 대상만 등록·수정·삭제·수집 실행.
"""
from __future__ import annotations

import posixpath
import re
import stat
import tempfile
from pathlib import Path

import agents

METHODS = ("agent", "ssh")
EXTS = (".pem", ".crt", ".cer", ".der", ".pfx", ".p12")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")


def clean(b: dict, existing: dict | None = None) -> dict:
    name = str(b.get("name", "")).strip()[:60]
    host = str(b.get("host", "")).strip().lower()[:253]
    if not name:
        raise ValueError("이름을 입력하세요")
    if not HOST_RE.match(host):
        raise ValueError("호스트는 서버 이름·IP·도메인으로 입력하세요 (예: web01, 10.0.1.20)")
    method = b.get("method") if b.get("method") in METHODS else "agent"
    os_ = "Windows" if b.get("os") == "Windows" else "Linux"
    try:
        port = int(b.get("port") or (22 if method == "ssh" else 0))
    except ValueError:
        raise ValueError("포트는 숫자로 입력하세요") from None
    if method == "ssh" and not (1 <= port <= 65535):
        raise ValueError("SSH 포트를 확인하세요")
    paths = b.get("paths") or []
    if isinstance(paths, str):
        paths = paths.splitlines()
    paths = [p.strip() for p in paths if str(p).strip()][:20]
    if not paths:
        raise ValueError("수집할 경로를 한 줄에 하나씩 입력하세요 (예: /etc/nginx/ssl)")
    for p in paths:
        if len(p) > 300 or ".." in p.replace("\\", "/").split("/"):
            raise ValueError(f"경로 형식 오류: {p[:60]}")
    user = str(b.get("username", "")).strip()[:64]
    if method == "ssh" and not user:
        raise ValueError("SSH 접속 계정을 입력하세요")
    return {"name": name, "host": host, "os": os_, "method": method, "port": port if method == "ssh" else None,
            "username": user, "paths": paths, "memo": str(b.get("memo", "")).strip()[:200]}


def public(t: dict) -> dict:
    out = {k: v for k, v in t.items() if k != "enc"}
    out["has_secret"] = bool(t.get("enc"))
    return out


def agent_paths(store, host: str) -> list[str]:
    seen = []
    for t in store.list("targets"):
        if t.get("method") == "agent" and t.get("host") == host:
            for p in t.get("paths", []):
                if p not in seen:
                    seen.append(p)
    return seen


def agent_tasks(store, host: str) -> list[dict]:
    return [{"target_id": t["id"], "name": t["name"], "paths": t.get("paths", [])} for t in store.list("targets")
            if t.get("method") == "agent" and t.get("host") == host and int(t.get("run_seq") or 0) > int(t.get("done_seq") or 0)]


def _walk(sftp, root, depth=0, out=None):
    out = out if out is not None else []
    if depth > 4 or len(out) > 500:
        return out
    try:
        st = sftp.stat(root)
    except OSError:
        return out
    if stat.S_ISREG(st.st_mode):
        if root.lower().endswith(EXTS) and st.st_size <= 1024 * 1024:
            out.append(root)
        return out
    for e in sftp.listdir_attr(root):
        p = posixpath.join(root, e.filename)
        if stat.S_ISDIR(e.st_mode) and not stat.S_ISLNK(e.st_mode):
            _walk(sftp, p, depth + 1, out)
        elif stat.S_ISREG(e.st_mode) and e.filename.lower().endswith(EXTS) and e.st_size <= 1024 * 1024:
            out.append(p)
    return out


def ssh_collect(store, vault, t: dict, actor: str) -> dict:
    """SSH(SFTP) 로 인증서 파일을 읽어 와 서버에서 파싱 → 수집 승인 대기로 등록. 위치는 '호스트:경로'."""
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("SSH 수집에는 paramiko 가 필요합니다 (서버에 pip install paramiko)") from None
    import certlib
    pw = vault.decrypt(t["id"], t["enc"]) if t.get("enc") else None
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())      # 테스트 환경: 최초 접속 키 자동 등록(운영은 known_hosts 고정 권장)
    try:
        cli.connect(t["host"], port=int(t.get("port") or 22), username=t["username"], password=pw, timeout=15,
                    allow_agent=False, look_for_keys=False)
    finally:
        pw = None
    reg = agents._Registrar(store, f"{t['host']} (SSH 수집: {t['name']})")
    try:
        sftp = cli.open_sftp()
        files = []
        for root in t.get("paths", []):
            files += _walk(sftp, root)
        with tempfile.TemporaryDirectory() as td:
            for f in files:
                loc = f"{t['host']}:{f}"
                reg.res["files"] += 1
                try:
                    with sftp.open(f, "rb") as fh:
                        data = fh.read(1024 * 1024)
                    local = Path(td) / Path(f).name
                    local.write_bytes(data)
                    rel = f"{t['host']}/{f.lstrip('/')}"
                    cands = []
                    if local.suffix.lower() in (".pfx", ".p12"):
                        import vault as vault_mod
                        cands = vault_mod.candidates(store.list("secrets"), rel)
                    pws = [vault.decrypt(c["id"], c["enc"]) for c in cands]
                    certs, used = certlib.load_certs_ex(local, pws)
                    pws.clear()
                    local.unlink()
                    for c in certs:
                        from cryptography.hazmat.primitives import serialization
                        reg.add(c, loc, c.public_bytes(serialization.Encoding.PEM).decode())
                except Exception as e:  # noqa: BLE001
                    reg.fail(loc, str(e)[:200] or type(e).__name__)
    finally:
        cli.close()
    return reg.finish(actor, {"host": t["host"], "engine": "SSH 수집", "target_id": t["id"]})

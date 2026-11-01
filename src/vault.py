"""PFX 비밀번호 보관함 (PC 서버용).

담당자가 플랫폼에서 입력한 PFX 비밀번호를 AES-256-GCM 으로 암호화해 DB(secrets 컬렉션)에 보관하고,
수집 Agent 가 PFX 파일을 열 때만 메모리에서 복호화해 쓴다.

  - 암호화 키: 32바이트 무작위 키. DB 와 다른 곳에 둔다.
      1) 환경변수 CERT_VAULT_KEY (base64) 가 있으면 그것을 쓰고
      2) 없으면 키 파일(기본: %APPDATA%\\cert_platform\\vault.key, 리눅스 ~/.cert_platform/vault.key)을 쓰며, 없으면 처음 실행 때 만든다.
    → DB 파일만 유출되면 비밀번호를 풀 수 없다. 키 파일은 OneDrive·백업 폴더에 두지 않는다.
  - 암호문마다 새 nonce(12바이트), 추가 인증 데이터(AAD)에 문서 ID 를 넣어 다른 문서로 옮겨 붙이면 복호화가 실패한다.
  - 평문 비밀번호는 API 응답·감사 로그·화면 어디에도 다시 내보내지 않는다(조회 불가, 교체·삭제만 가능).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALG = "AES-256-GCM"


def default_key_path() -> Path:
    if sys.platform.startswith("win") and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "cert_platform" / "vault.key"
    return Path.home() / ".cert_platform" / "vault.key"


class Vault:
    def __init__(self, key_path: str | Path | None = None):
        env = os.environ.get("CERT_VAULT_KEY")
        if env:
            key = base64.b64decode(env)
            self.source = "환경변수 CERT_VAULT_KEY"
        else:
            p = Path(key_path) if key_path else default_key_path()
            if p.exists():
                key = base64.b64decode(p.read_text().strip())
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                key = _secrets.token_bytes(32)
                fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(base64.b64encode(key).decode())
                if sys.platform.startswith("win"):            # 윈도우: 상속 권한을 끊고 현재 사용자만 읽기·쓰기
                    import subprocess
                    subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                                   capture_output=True, check=False)
            self.source = str(p)
        if len(key) != 32:
            raise ValueError("보관함 키는 32바이트여야 합니다")
        self._aes = AESGCM(key)
        self.kid = hashlib.sha256(key).hexdigest()[:12]   # 키 식별자(키 교체·불일치 확인용, 키 자체는 아님)

    @staticmethod
    def _aad(secret_id: str) -> bytes:
        return f"cert-platform/pfx-secret/{secret_id}".encode()

    def encrypt(self, secret_id: str, password: str) -> dict:
        nonce = _secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, password.encode("utf-8"), self._aad(secret_id))
        return {"alg": ALG, "kid": self.kid, "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}

    def decrypt(self, secret_id: str, enc: dict) -> str:
        if enc.get("kid") != self.kid:
            raise ValueError("보관함 키가 달라 복호화할 수 없습니다")
        return self._aes.decrypt(base64.b64decode(enc["nonce"]), base64.b64decode(enc["ct"]), self._aad(secret_id)).decode("utf-8")


# ───────── 적용 범위: 전체 / 폴더(하위 포함) / 파일 (스캔 폴더 기준 상대 경로)
SCOPES = ("all", "folder", "file")


def norm_rel(p: str) -> str:
    p = str(p or "").replace("\\", "/").strip().strip("/")
    if p.startswith(("/", "~")) or ":" in p or any(part == ".." for part in p.split("/")):
        raise ValueError("적용 범위는 스캔 폴더 기준 상대 경로로 입력하세요")
    return p


def matches(sec: dict, rel: str) -> bool:
    rel = rel.replace("\\", "/")
    sc, v = sec.get("scope"), sec.get("scope_value", "")
    if sc == "all":
        return True
    if sc == "file":
        return rel == v
    if sc == "folder":
        return rel == v or rel.startswith(v + "/")
    return False


def candidates(secret_docs: list[dict], rel: str) -> list[dict]:
    """파일에 적용할 비밀번호 순서: 파일 지정 → 폴더(깊은 폴더 우선) → 전체, 같은 범위면 최근에 성공한 것부터."""
    rank = {"file": 0, "folder": 1, "all": 2}
    hits = [s for s in secret_docs if s.get("active", True) and matches(s, rel)]
    hits.sort(key=lambda s: s.get("last_used_at") or "", reverse=True)          # 최근 성공 우선 (안정 정렬로 아래 기준 안에서 유지)
    return sorted(hits, key=lambda s: (rank.get(s.get("scope"), 3), -len(s.get("scope_value", ""))))


def public(sec: dict) -> dict:
    """화면에 보내는 정보(암호문 제외)."""
    return {k: sec.get(k) for k in ("id", "label", "scope", "scope_value", "owner", "created_by", "created_at", "updated_at",
                                    "last_used_at", "last_used_file", "use_count", "last_result", "active")} | \
        {"alg": (sec.get("enc") or {}).get("alg"), "kid": (sec.get("enc") or {}).get("kid")}

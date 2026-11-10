#!/usr/bin/env python3
"""인증서 관리 플랫폼 MCP 서버 (Model Context Protocol, stdio · JSON-RPC 2.0)

외부 AI Agent(Claude Desktop·Claude Code·사내 Agent)가 플랫폼 기능을 '도구'로 호출하게 한다.
  등록 예 (Claude Code):  claude mcp add cert-platform -- python3 /opt/cert-agent-src/src/mcp_server.py --db /var/lib/cert-platform/platform.db
읽기 도구는 자유롭게, 상태를 바꾸는 도구(run_orchestration)는 알림 생성까지만 하며 배포·삭제 도구는 제공하지 않는다.

프로필
  external(기본)  외부 AI 용 도구 5종 (위 설명)
  internal        오케스트레이터 전용: 각 Agent 기능 9종(collector_status · verifier_pending · notifier_run · notifier_send ·
                  deployer_plan · deployer_status · qa_brief · maint_diagnose · report_summary).
                  오케스트레이터가 이 서버를 자식 프로세스로 띄워 MCP(tools/call)로 Agent 를 호출한다. 배포 실행 도구는 여기에도 없다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agents  # noqa: E402
import certlib  # noqa: E402
import ops_agents  # noqa: E402

TOOLS = [
    {"name": "list_expiring", "description": "만료가 가까운 인증서 목록(남은 일수 기준)", "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "default": 30}}}},
    {"name": "get_certificate", "description": "인증서 이름(CN) 일부로 상세 조회", "inputSchema": {"type": "object", "properties": {"cn": {"type": "string"}}, "required": ["cn"]}},
    {"name": "deployment_status", "description": "단계별(개발·테스트·교육·운영) 현재 배포본과 서버 적용 결과", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "diagnose", "description": "유지보수 Agent 자가 진단(버그·운영·데이터 품질)", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_orchestration", "description": "오케스트레이터에 목표를 주고 실행(알림 생성·발송까지, 배포는 제안만)", "inputSchema": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}},
]


INTERNAL_TOOLS = [{"name": k.replace(".", "_"), "description": f"[{v[0]}] {v[1]}",
                   "inputSchema": {"type": "object", "properties": {"actor": {"type": "string"}}}} for k, v in ops_agents.TOOLS.items()]


def call_internal(store, name, args):
    tool = name.replace("_", ".", 1)
    if tool not in ops_agents.TOOLS:
        raise ValueError(f"unknown tool {name}")
    ctx = {}
    r = ops_agents._run_tool(store, tool, ctx, str(args.get("actor") or "오케스트레이터"))
    return dict(r, ctx=ctx)


def call(store, name, args):
    now = datetime.now(timezone.utc)
    certs = [c for c in store.list("certificates") if not c.get("replaced_by") and c.get("not_after")]
    if name == "list_expiring":
        lim = int(args.get("days", 30))
        rows = []
        for c in certs:
            st, d = certlib.status_of(c.get("not_before") or "1970-01-01T00:00:00+00:00", c["not_after"], now)
            if d <= lim:
                rows.append({"cn": c["subject_cn"], "days": d, "status": st, "issuer": c.get("issuer_cn"), "owner": c.get("owner"), "renewal": c.get("renewal_status")})
        return sorted(rows, key=lambda r: r["days"])
    if name == "get_certificate":
        q = str(args.get("cn", "")).lower()
        return [{k: c.get(k) for k in ("subject_cn", "issuer_cn", "not_before", "not_after", "serial_number", "fingerprint", "owner", "owner_email", "locations", "renewal_status")}
                for c in certs if q in str(c.get("subject_cn", "")).lower()][:10]
    if name == "deployment_status":
        return [{"stage": e.get("stage_name"), "cn": e.get("subject_cn"), "cert_id": e.get("cert_id"), "applied": e.get("applied", {})} for e in store.list("environments")]
    if name == "diagnose":
        return [{k: f.get(k) for k in ("severity", "kind", "title", "proposal", "status")} for f in ops_agents.diagnose(store)][:20]
    if name == "run_orchestration":
        r = ops_agents.orchestrate(store, str(args.get("goal", "일일 운영 점검")), "MCP 클라이언트")
        return {"status": r["status"], "result": r["result"], "steps": [{"agent": s["agent"], "label": s["label"], "summary": s["summary"]} for s in r["steps"]]}
    raise ValueError(f"unknown tool {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("CERT_DB"), required=not os.environ.get("CERT_DB"))
    ap.add_argument("--profile", choices=["external", "internal"], default="external")
    a = ap.parse_args()
    store = agents.Store(agents.resolve_db(a.db))
    tools, fn = (INTERNAL_TOOLS, call_internal) if a.profile == "internal" else (TOOLS, call)
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                     # notification (initialized 등)
            continue
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                       "serverInfo": {"name": "cert-platform" + ("-internal" if a.profile == "internal" else ""), "version": "1.1"}}
            elif method == "tools/list":
                res = {"tools": tools}
            elif method == "tools/call":
                out = fn(store, params.get("name"), params.get("arguments") or {})
                res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]}
            elif method == "ping":
                res = {}
            else:
                raise ValueError(f"method not found: {method}")
            reply = {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601 if "not found" in str(e) else -32000, "message": str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

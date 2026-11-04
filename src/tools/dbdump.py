"""저장소 원문 덤프(테스트·점검용): 문서마다 한 줄 JSON. SQLite 파일·PostgreSQL 모두 지원.
  python3 tools/dbdump.py <db파일|postgresql://…> [컬렉션]     (CERT_TEST_DB 가 있으면 그것을 우선)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402

db = agents.resolve_db(sys.argv[1])
s = agents.Store(db)
colls = [sys.argv[2]] if len(sys.argv) > 2 else sorted(agents.COLLECTIONS)
for c in colls:
    for d in s.list(c):
        print(json.dumps(dict(d, _coll=c), ensure_ascii=False))

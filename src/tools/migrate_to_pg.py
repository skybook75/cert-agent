"""SQLite 저장소 → PostgreSQL 이전(무중단 준비용, 여러 번 실행해도 결과 같음)

  CERT_DB=postgresql://… python3 tools/migrate_to_pg.py /var/lib/cert-platform/platform.db

  1) PostgreSQL 에 docs 테이블(JSONB) 생성
  2) 한 트랜잭션으로 전 문서 복사(갱신 시각 보존, 이미 있으면 덮어씀)
  3) 검증: 컬렉션별 건수 + 문서 내용(JSON 값) 전수 비교 → 하나라도 다르면 실패 코드로 종료
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
import pgwire  # noqa: E402


def main(src: str, url: str) -> int:
    lite = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    rows = lite.execute("SELECT coll, id, data, updated_at FROM docs ORDER BY coll, id").fetchall()
    agents.Store(url)                                    # 테이블 생성
    pg = pgwire.connect(url)
    pg.query("BEGIN")
    try:
        for coll, id_, data, upd in rows:
            pg.query("INSERT INTO docs (coll, id, data, updated_at) VALUES ($1,$2,$3::jsonb,COALESCE($4::timestamptz, now())) "
                     "ON CONFLICT (coll, id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at", [coll, id_, data, upd])
        pg.query("COMMIT")
    except Exception:
        pg.query("ROLLBACK")
        raise
    src_cnt, bad = {}, []
    for coll, id_, data, _ in rows:
        src_cnt[coll] = src_cnt.get(coll, 0) + 1
        r = pg.query("SELECT data::text FROM docs WHERE coll=$1 AND id=$2", [coll, id_])
        if not r or json.loads(r[0][0]) != json.loads(data):
            bad.append(f"{coll}/{id_}")
    dst_cnt = {c: int(n) for c, n in pg.query("SELECT coll, count(*) FROM docs GROUP BY coll")}
    print(json.dumps({"source": src, "docs": len(rows), "collections": src_cnt, "target_counts": dst_cnt,
                      "content_mismatch": bad[:10], "ok": not bad and all(dst_cnt.get(c) == n for c, n in src_cnt.items())}, ensure_ascii=False))
    return 0 if not bad and all(dst_cnt.get(c) == n for c, n in src_cnt.items()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], os.environ["CERT_DB"]))

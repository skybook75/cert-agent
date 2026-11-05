# 녹화 중 서버에 실제로 찍힌 로그(journalctl -o short-iso-precise 원문) → 장면별 터미널 창 이벤트
#   python3 make_events_live.py <rec_dir> <platform_log.txt> <agents_log.txt>
#   시각 기준: 녹화기가 기록한 창별 녹화 시작 시각(marks.json videos[].t0, epoch ms) — 추정 없음
#   줄 내용은 손대지 않는다(요약·번역·시각 조정 없음). 같은 시각 줄은 같은 순간에 나타난다.
import json, re, sys
from datetime import datetime
from pathlib import Path

REC, PLOG, ALOG = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
MK = json.loads((REC / "marks.json").read_text(encoding="utf-8"))
TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)([+-]\d{4})\s")


NGX = re.compile(r'\[(\d\d/\w{3}/\d{4}:\d\d:\d\d:\d\d) ([+-]\d{4})\]')
SCENES = set((sys.argv[4] if len(sys.argv) > 4 else "targets,apply,maint").split(","))   # 핵심 장면만


def ep(line):
    m = TS.match(line)
    if not m:
        n = NGX.search(line)                       # nginx access.log 형식
        return datetime.strptime(n.group(1) + " " + n.group(2), "%d/%b/%Y:%H:%M:%S %z").timestamp() if n else None
    z = m.group(2); return datetime.fromisoformat(m.group(1) + z[:3] + ":" + z[3:]).timestamp()


EXCL = r": GET \S+ [23]\d\d |no approved task|/api/me 401|POST /api/login |auth\.login"   # = grep -vE (반복되는 정상 줄 제외, 나머지는 원문)
lines = []
for f in (PLOG, ALOG):
    for r in f.read_text(encoding="utf-8", errors="replace").splitlines():
        e = ep(r)
        if e is not None and ("systemd[1]" not in r or "nginx" in r) and " audit[" not in r and not re.search(EXCL, r):   # 정상 조회(GET 2xx/3xx)만 제외 = grep -v
            lines.append((e, r.rstrip()))
lines.sort(key=lambda a: a[0])
ev = {}
for v in MK["videos"]:
    s = v["scene"]; t0 = v["t0"] / 1000
    end = max(m["t"] for m in MK["marks"] if m["scene"] == s) + 5
    if s in ev or s not in SCENES: continue
    ev[s] = {"title": "journalctl -f (platform, web01) + nginx access.log | grep -vE 'GET .* 200|no approved task|login'", "t0": t0,
             "lines": [[e, x] for e, x in lines if t0 - 2 <= e <= t0 + end + 60]}
    print(s, len(ev[s]["lines"]))
bad = [x for v in ev.values() for _, x in v["lines"] if re.search("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]", x)] + \
      [v["title"] for v in ev.values() if re.search("[\uac00-\ud7a3]", v["title"])]
if bad:                                            # 로그 창에 한글이 한 글자라도 있으면 영상을 만들지 않는다
    sys.exit("HANGUL IN LOG PANEL:\n" + "\n".join(bad[:20]))
(REC.parent / "events_live.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")

# 녹화 장면 위에 '실시간 로그' 작은 창을 입힌다 (실제 서버 로그를 녹화 시각에 맞춰 표시)
#   python3 logpanel.py <rec> <events.json> <out_rec>
#   events.json: {"<scene>": {"title": "...", "t0": <장면 영상 0초의 UTC epoch 초>, "lines": [[epoch, "텍스트"], ...]}}
#   같은 장면의 여러 창(deploy: admin/reviewer/approver)은 같은 시각 기준을 쓴다(동시에 열림).
import json, re, subprocess, sys, shutil
from pathlib import Path

REC, EV, OUT = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), Path(sys.argv[3])
MK = json.loads((REC / "marks.json").read_text(encoding="utf-8"))
X0, Y0, PW, PH, ROWS, COLS = 596, 128, 676, 262, 13, 102   # 오른쪽 위 터미널 창(제목 칩·하단 자막과 겹치지 않는 자리) · 원문을 줄바꿈해서 그대로 표시
FONT = "Noto Sans Mono CJK KR"


def ts(s):
    s = max(0.0, s); return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def esc(t):
    return t.replace("\\", "/").replace("{", "(").replace("}", ")")


def dur(f):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)]).decode())


def wrap(t, n=COLS):
    rows, w, o = [], 0, ""
    for ch in t:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > n: rows.append(o); o, w = "  ", 2
        o += ch; w += cw
    return rows + [o]


def col(m):                                       # 터미널 색: 감사=노랑, 서버 Agent=하늘, 5xx·오류=빨강, 나머지=회색 (BGR)
    if re.search(r" 5\d\d \d+ms|Error|Traceback|실패 감지|결과 보고: 실패", m): return "&H6B6BFF&"
    if " AUDIT " in m: return "&H7FE0F5&"
    if re.search(r" (web01|was01|win01) ", m): return "&HF0D27C&"
    return "&HBDBDBD&"


def ass(scene, D, t0=None):
    e = EV[scene]; t0 = e["t0"] if t0 is None else t0
    items = sorted((ln[0] - t0, ln[1]) for ln in e["lines"])
    items = [(max(0.3, t), m) for t, m in items if t < D - 0.3]  # 장면 시작 전 줄은 시작 시 이미 화면에 있음
    box = f"m 0 0 l {PW} 0 l {PW} {PH} l 0 {PH}"
    ev = [f"Dialogue: 0,{ts(0)},{ts(D)},B,,0,0,0,,{{\\pos({X0},{Y0})\\p1}}{box}",
          f"Dialogue: 1,{ts(0)},{ts(D)},H,,0,0,0,,{{\\pos({X0 + 10},{Y0 + 7})}}$ {esc(e['title'])}"]
    groups = []                                   # 같은 시각(초)에 찍힌 줄은 한꺼번에 나타남 — 원문 그대로
    for t, m in items:
        if groups and abs(groups[-1][0] - t) < 0.01: groups[-1][1].append(m)
        else: groups.append((t, [m]))
    buf = []
    for i, (t, ms) in enumerate(groups):
        new = [(r, col(m)) for m in ms for r in wrap(m)]
        buf = buf + new
        show = buf[-ROWS:]
        txt = "\\N".join("{\\c" + c + "}" + esc(r) for r, c in show)
        end = groups[i + 1][0] if i + 1 < len(groups) else D
        ev.append(f"Dialogue: 2,{ts(t)},{ts(end)},L,,0,0,0,,{{\\pos({X0 + 10},{Y0 + 30})}}{txt}")
    if not items:
        ev.append(f"Dialogue: 2,{ts(0)},{ts(D)},L,,0,0,0,,{{\\pos({X0 + 10},{Y0 + 30})}}{{\\c&H909090&}}_")
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            "Style: B,Arial,10,&H0C100C0A,&H00000000,&H00000000,0,1,0,0,7,0,0,0\n"
            f"Style: H,{FONT},17,&H00B8E37C,&H00000000,&H00000000,1,1,0,0,7,0,0,0\n"
            f"Style: L,{FONT},17,&H00CFCFCF,&H00000000,&H00000000,0,1,0,0,7,0,0,0\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(ev) + "\n")


OUT.mkdir(parents=True, exist_ok=True)
vids = []
for v in MK["videos"]:
    src = REC / v["file"]; dst = OUT / v["file"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if v["scene"] not in EV:
        shutil.copy(src, dst); vids.append(v); continue
    D = dur(src); a = dst.with_suffix(".ass"); a.write_text(ass(v["scene"], D, v["t0"] / 1000 if v.get("t0") else None), encoding="utf-8")   # 창별 실제 녹화 시작 시각
    out = dst.with_suffix(".mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"subtitles={a}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-an", str(out)], check=True)
    vids.append({**v, "file": str(out.relative_to(OUT))}); print("panel", v["scene"], v["ctx"], f"{D:.1f}s")
(OUT / "marks.json").write_text(json.dumps({**MK, "videos": vids}, ensure_ascii=False, indent=1), encoding="utf-8")

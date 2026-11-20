"""offline/README.md → install/index.html (GitHub Pages 설치 가이드 페이지). 사용: python3 offline/build_page.py 1.0.0"""
import re
import sys
from pathlib import Path

import markdown

VER = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
R = Path(__file__).resolve().parent.parent
md = (R / "offline" / "README.md").read_text(encoding="utf-8")
md = re.sub(r"^# .*\n", "", md, count=1)                                  # 제목은 머리말에서
body = markdown.markdown(md, extensions=["tables", "fenced_code", "toc", "sane_lists"], extension_configs={"toc": {"slugify": lambda s, sep: re.sub(r"[^0-9A-Za-z가-힣]+", "-", s).strip("-")}})
REL = f"https://github.com/skybook75/cert-agent/releases/download/v{VER}"
html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>폐쇄망 설치 가이드</title>
<style>
:root{{--bg:#f4f6f7;--card:#fff;--ink:#16232a;--muted:#5a6b73;--line:#d8e0e3;--acc:#0b6e69;--code:#eef2f3;--warn:#fff6e0}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0f1719;--card:#152124;--ink:#e3ecec;--muted:#93a6ab;--line:#2a3a3e;--acc:#5cc7bf;--code:#1b2a2e;--warn:#2b2614}}}}
:root[data-theme="dark"]{{--bg:#0f1719;--card:#152124;--ink:#e3ecec;--muted:#93a6ab;--line:#2a3a3e;--acc:#5cc7bf;--code:#1b2a2e;--warn:#2b2614}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:40px 16px 80px}}
header h1{{font-size:26px;margin:0 0 4px}} header p{{color:var(--muted);margin:0 0 22px}}
.dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:0 0 28px}}
.dl a{{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;color:inherit;text-decoration:none}}
.dl a:hover{{border-color:var(--acc)}} .dl b{{color:var(--acc)}} .dl span{{display:block;color:var(--muted);font-size:13px;margin-top:2px}}
h2{{font-size:20px;margin:40px 0 10px;padding-top:12px;border-top:1px solid var(--line)}} h3{{font-size:16px;margin:24px 0 8px}}
a{{color:var(--acc)}}
table{{border-collapse:collapse;width:100%;margin:10px 0 16px;background:var(--card);font-size:14px;display:block;overflow-x:auto}}
th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}} th{{background:var(--code);white-space:nowrap}}
code{{font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:4px}}
pre{{position:relative;background:var(--code);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto}}
pre code{{background:none;padding:0;white-space:pre}}
pre button{{position:absolute;top:8px;right:8px;font:12px system-ui;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:3px 8px;cursor:pointer}}
blockquote{{margin:12px 0;padding:10px 14px;background:var(--warn);border-left:4px solid #d39b00;border-radius:6px}}
blockquote p{{margin:0}}
hr{{display:none}}
footer{{color:var(--muted);font-size:13px;margin-top:40px}}
</style>
</head>
<body>
<main>
<header>
<h1>인증서 관리 AI Agent — 폐쇄망 설치 가이드</h1>
<p>v{VER} · 관리 플랫폼(Linux) + 서버 Agent(Linux·Windows) · 인터넷 없이 설치</p>
</header>
<div class="dl">
<a href="{REL}/cert-agent-offline-{VER}.tar.gz"><b>전체 묶음 내려받기</b><span>cert-agent-offline-{VER}.tar.gz · 소스·설치 스크립트·Qdrant</span></a>
<a href="{REL}/cert-agent-agent-{VER}.zip"><b>Agent 만 내려받기</b><span>cert-agent-agent-{VER}.zip · 각 서버에 복사</span></a>
<a href="{REL}/SHA256SUMS-{VER}.txt"><b>SHA-256 해시</b><span>반입 전·후 무결성 확인</span></a>
<a href="https://github.com/skybook75/cert-agent/tree/main/src"><b>소스 보기 (GitHub)</b><span>src/ · offline/ (설치 스크립트)</span></a>
</div>
{body}
<footer>2026 AI Master Project · 인증서 관리 AI Agent · 이 페이지는 저장소의 offline/README.md 로 만들어집니다.</footer>
</main>
<script>
document.querySelectorAll("pre").forEach(function (p) {{
  var b = document.createElement("button"); b.type = "button"; b.textContent = "복사";
  b.onclick = function () {{ var t = p.querySelector("code").innerText;
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(function () {{ b.textContent = "복사됨"; setTimeout(function () {{ b.textContent = "복사"; }}, 1500); }}, function () {{ b.textContent = "선택해 복사하세요"; }}); }};
  p.appendChild(b);
}});
</script>
</body>
</html>
"""
out = R / "install" / "index.html"
out.parent.mkdir(exist_ok=True)
out.write_text(html, encoding="utf-8")
print("wrote", out)

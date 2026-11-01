"""claude.ai 밖(일반 웹 호스팅)에 올릴 정적 사이트 묶음을 만든다.

  python tools/build_static.py [출력폴더]   (기본: ../site_static)

결과
  <출력>/index.html               두 사이트로 가는 첫 화면
  <출력>/platform/                관리 플랫폼 (웹 시연 모드: 시연 데이터, 검색형 QA, 규칙 기반 답변 검증)
  <출력>/poc-review/              PoC 단계별 리뷰 사이트
Netlify Drop(app.netlify.com/drop)에 <출력> 폴더를 끌어다 놓거나 GitHub Pages 저장소 루트에 그대로 올리면 된다.
"""
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # platform/
POC = BASE.parent / "poc_review"
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else BASE.parent / "site_static"
PLATFORM_URL = "https://claude.ai/artifact/4iV6TMCsYUsgaYuZmg7axH"
POC_URL = "https://claude.ai/artifact/8Dmy99QR2AyK6QmzpXS6B5"


def page(body: str, title: str) -> str:
    """claude.ai 가 자동으로 씌우던 문서 골격을 직접 씌운다."""
    return ("<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            f"<title>{title}</title>\n</head>\n<body>\n{body}\n</body>\n</html>\n")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "platform").mkdir(parents=True)
    (OUT / "poc-review").mkdir(parents=True)

    # 관리 플랫폼
    web = BASE / "web"
    html = (web / "index.html").read_text(encoding="utf-8").replace(POC_URL, "../poc-review/")
    html = html.replace('<meta name="claude:viewport-fit-sides" content="cover">\n', "")
    (OUT / "platform" / "index.html").write_text(page(html, "인증서 관리 플랫폼"), encoding="utf-8")
    for f in ("core.js", "xlsx-lite.js", "scanner.js", "seed.json", "samples.json"):
        shutil.copy2(web / f, OUT / "platform" / f)
    shutil.copy2(BASE / "guides" / "guides.json", OUT / "platform" / "guides.json")
    shutil.copytree(web / "vendor", OUT / "platform" / "vendor")

    # PoC 리뷰
    poc = (POC / "index.html").read_text(encoding="utf-8").replace(PLATFORM_URL, "../platform/")
    poc = poc.replace('<meta name="claude:viewport-fit-sides" content="cover">\n', "")
    (OUT / "poc-review" / "index.html").write_text(page(poc, "인증서 수집 PoC 리뷰"), encoding="utf-8")

    # 첫 화면
    (OUT / "index.html").write_text(page("""
<style>
:root{--bg:#f2f5f5;--ink:#142127;--muted:#586a72;--line:#d5dfe0;--acc:#0b6e69;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1719;--ink:#e3ecec;--muted:#93a6ab;--line:#2a3a3e;--acc:#5cc7bf;--card:#152124}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:760px;margin:0 auto;padding:48px 16px}
h1{font-size:24px;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 28px}
a.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:0 0 14px;color:inherit;text-decoration:none}
a.card:hover{border-color:var(--acc)}a.card b{color:var(--acc);font-size:17px}a.card p{margin:4px 0 0;color:var(--muted)}
footer{color:var(--muted);font-size:13px;margin-top:28px}
</style>
<main>
<h1>인증서 관리 AI Agent</h1>
<p class="sub">2026 AI Master Project · SK AX AI Talent Lab</p>
<a class="card" href="https://certagent-ai.duckdns.org/"><b>AWS 운영 플랫폼(실서버) →</b><p>AWS EC2 서버 4대(nginx·Tomcat·IIS)와 연결된 실제 운영판 · 서버 Agent 수집·배포 · AI 운영(오케스트레이터·유지보수 Agent) · 로그인 필요</p></a>
<a class="card" href="poc-review/"><b>PoC 단계별 리뷰 →</b><p>스캔 → 파싱 → 저장 → 조회 단계별 결과와 근거, 수집 진행 현황, 브라우저에서 직접 실행</p></a>
<a class="card" href="platform/"><b>인증서 관리 플랫폼 →</b><p>대시보드 · 관리대장 · 수집 승인 · 4단계 배포 · 알림 · 보고서 · PFX 비밀번호 보관함 · QA 챗봇</p></a>
<a class="card" href="test-report/"><b>인수 테스트 결과 →</b><p>관리자 관점 운영 시나리오 12개 + 실제 Claude QA · 단계별 증적 화면 · 발견사항과 조치</p></a>
<footer>공개 시연판: 시연 데이터로 동작하며 바꾼 내용은 방문자 브라우저에만 남습니다. QA 챗봇은 가이드 원문 검색형 답변으로 동작합니다.</footer>
</main>""", "인증서 관리 AI Agent"), encoding="utf-8")
    import subprocess
    subprocess.run([sys.executable, str(BASE / "tools" / "build_test_report.py"), str(OUT / "test-report")], check=True)
    n = sum(1 for _ in OUT.rglob("*") if _.is_file())
    print(f"정적 사이트: {OUT} (파일 {n}개)")


if __name__ == "__main__":
    main()

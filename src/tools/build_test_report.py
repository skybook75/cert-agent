"""인수 테스트 결과 보고 페이지 생성: results/acceptance/report.json + 발견사항 → <출력>/index.html (+ shots/)

  python tools/build_test_report.py <출력폴더>
"""
import html
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ACC = BASE / "results" / "acceptance"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE.parent / "site_static" / "test-report"
R = json.loads((ACC / "report.json").read_text(encoding="utf-8"))
QA = json.loads((ACC / "qa_live_claude.json").read_text(encoding="utf-8"))
SCE = json.loads((BASE / "results" / "self_correction_eval.json").read_text(encoding="utf-8"))["summary"]
e = html.escape

FINDINGS = [  # (시나리오, 증상, 원인, 조치, 확인)
    ("S03", "대장의 'Let's Encrypt'와 인증서의 발급자 'R11'이 달라 정상 인증서가 '불일치'로 분류", "인증서에는 중간 CA 이름(R10~R14·E5~E9)이 적히는데 문자열로만 비교",
     "발급기관 브랜드 정규화(Let's Encrypt·DigiCert·Sectigo·GlobalSign·국내 CA)로 비교", "불일치 2 → 1(만료일 차이만)"),
    ("S03·S05", "서버에 남은 2025년 사본이 대장 행과 먼저 맞춰져 '불일치'로 승인되고, 현재 인증서가 '신규'로 중복 등록", "같은 회차에 수집된 같은 이름의 더 최신 파일을 고려하지 않음",
     "같은 이름의 더 최신 파일이 함께 수집되면 '이전 버전'으로 분류, 승인 버튼을 막고 반려 권장", "www 2건 → 1건, 이전 버전 1건 반려"),
    ("S07", "반려한 사본이 다음 수집 때 다시 '승인 대기'로 되살아남", "수집 결과 문서 ID가 지문 기준이라 반려 기록을 덮어씀",
     "반려 이력이 있는 지문은 건너뛰고 '이전에 반려한 인증서 N건 제외'로 표시(서버판·웹판 모두)", "승인 대기 2건 → 1건(갱신본만)"),
    ("S07", "서버판 수집 결과 문구에 갱신본 건수가 없음", "웹판과 문구가 달랐음", "'승인 대기 N건(갱신본 M)'으로 통일", "확인"),
    ("S08", "처음 배포한 단계는 원복할 수 없음(직전본 없음)", "플랫폼 밖에서 쓰던 인증서를 직전본으로 기록하지 않음",
     "첫 배포 때는 갱신 전 인증서를 직전 배포본으로 기록 → 운영 원복 시 그 파일로 되돌림", "운영 원복 → 갱신 전 파일 복원, 갱신본은 history 보관"),
    ("S03", "대장 대조 칩에 '이전 버전' 건수가 없음", "표시 순서 목록에서 빠짐", "칩 추가", "확인"),
    ("S13", "답변 검증 AI가 등록 정보 표기 [인증서]를 '근거 번호 형식이 다르다'고 지적(오탐)", "검증 프롬프트에 [인증서] 표기 규칙이 없었음", "검증 프롬프트에 근거 번호 규칙 명시", "재질문 시 오탐 사라짐(적합 93/84)"),
    ("S06", "폐기한 서버(old-gw)의 인증서도 대장에 남아 있으면 D-30 알림 발생", "설계상 정상(대장 기준 알림)", "운영 안내: 미수집 상태로 남은 대장 건은 정리 대상 — 대조 화면 '미수집'으로 확인", "운영 가이드 반영"),
]


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "shots").mkdir(parents=True)
    for f in (ACC / "shots").iterdir():
        shutil.copy2(f, OUT / "shots" / f.name)
    sc_html = []
    for s in R["scenarios"]:
        ok = sum(x["pass"] for x in s["steps"]); n = len(s["steps"])
        rows = "".join(f"<tr><td>{e(x['action'])}</td><td>{e(x['expected'])}</td><td class='mono'>{e(x['actual'][:300])}</td><td class='{'ok' if x['pass'] else 'bad'}'>{'통과' if x['pass'] else '실패'}</td></tr>" for x in s["steps"])
        shots = "".join(f"<figure><a href='shots/{e(sh['file'])}' target='_blank'><img loading='lazy' src='shots/{e(sh['file'])}' alt='{e(sh['caption'])}'></a><figcaption>{e(sh['caption'])}</figcaption></figure>" for sh in s["shots"])
        sc_html.append(f"""<section class="sc" id="{s['id']}"><h3><span class="id">{s['id']}</span> {e(s['title'])} <span class="badge {'ok' if ok == n else 'bad'}">{ok}/{n}</span></h3>
<p class="goal">{e(s['goal'])}</p><div class="tbl"><table><thead><tr><th>조작</th><th>기대 결과</th><th>실제 결과</th><th>판정</th></tr></thead><tbody>{rows}</tbody></table></div><div class="shots">{shots}</div></section>""")
    f_rows = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td>{e(c)}</td><td>{e(d)}</td><td>{e(x)}</td></tr>" for a, b, c, d, x in FINDINGS)
    qa_rows = "".join(f"<tr><td>{e(c['q'])}</td><td>{e(c['result'])}</td><td>{e(c.get('verify_after_fix') or c.get('verify', ''))}{(' <br><span class=note>수정 전: ' + e(c['verify_before_fix']) + '</span>') if c.get('verify_before_fix') else ''}</td></tr>" for c in QA["cases"])
    files = "".join(f"<tr><td class='mono'>{e(x['path'])}</td><td>{e(x.get('kind', ''))}</td><td>{e(', '.join(x.get('cn', [])) if isinstance(x.get('cn'), list) else '')}</td><td class='num'>{x.get('days', '')}</td></tr>" for x in R["data"]["files"])
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>인수 테스트 결과</title><style>
:root{{--bg:#f6f8fa;--card:#fff;--ink:#16202b;--muted:#5a6675;--line:#dce2e9;--acc:#1A365D;--mint:#3BB8B0;--ok:#1f7a4d;--okbg:#e2f3e9;--bad:#b3261e;--badbg:#fbe5e3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#10151c;--card:#172029;--ink:#e5eaf0;--muted:#98a4b3;--line:#29343f;--acc:#82b2dd;--ok:#70ce9b;--okbg:#163125;--bad:#ef8a80;--badbg:#391c1a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:28px 16px 60px}}h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:19px;margin:34px 0 10px;color:var(--acc)}}h3{{font-size:16px;margin:0 0 6px}}
.sub{{color:var(--muted);margin:0 0 18px}}.kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.kpi b{{font-size:26px;color:var(--acc);display:block}}.kpi span{{color:var(--muted);font-size:13px}}
.sc,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}}.goal{{color:var(--muted);margin:0 0 10px}}
.id{{display:inline-block;background:var(--acc);color:#fff;border-radius:6px;padding:0 7px;font-size:13px;margin-right:4px}}
.badge{{font-size:12px;border-radius:999px;padding:1px 8px;margin-left:6px}}.badge.ok{{background:var(--okbg);color:var(--ok)}}.badge.bad{{background:var(--badbg);color:var(--bad)}}
.tbl{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:720px}}th,td{{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--muted);background:var(--bg)}}
td.ok{{color:var(--ok);font-weight:700}}td.bad{{color:var(--bad);font-weight:700}}.mono{{font-family:Consolas,ui-monospace,monospace;font-size:12px;word-break:break-all}}.num{{text-align:right}}.note{{color:var(--muted);font-size:12px}}
.shots{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin-top:10px}}figure{{margin:0}}figure img{{width:100%;border:1px solid var(--line);border-radius:8px;display:block}}figcaption{{font-size:12px;color:var(--muted);margin-top:4px}}
nav.toc a{{display:inline-block;margin:0 10px 6px 0;color:var(--acc)}}.callout{{border-left:0;background:var(--okbg);border-radius:10px;padding:12px 14px}}
@media (max-width:700px){{.kpis{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
</style></head><body><main>
<h1>인증서 관리 플랫폼 · 인수 테스트 결과</h1>
<p class="sub">관리자 관점 운영 시나리오 {len(R['scenarios'])}개 + 실제 Claude QA · 실행 {e(R['at'][:16].replace('T', ' '))} UTC · 실제 PC 서버판 + Chromium 화면 조작(자동 기록) · <a href="../">← 시연 사이트</a></p>
<div class="kpis"><div class="kpi"><b>{R['pass']}/{R['total']}</b><span>시나리오 검증 항목 통과</span></div><div class="kpi"><b>{len(FINDINGS)}건</b><span>테스트 중 발견 → 수정·재검증</span></div>
<div class="kpi"><b>{SCE['fixed']}/{SCE['n']}</b><span>오답 자가 수정(Self-Correction) 실험</span></div><div class="kpi"><b>{R['seconds']}초</b><span>12개 시나리오 전체 자동 재실행 시간</span></div></div>
<h2>테스트 환경과 데이터</h2>
<div class="card"><p>서버 4대(nginx·Apache·Tomcat·IIS)를 흉내 낸 폴더에 실제 형식의 인증서 13개(체인 PEM·DER·PKCS#12·공동인증서·손상 파일·개인키 파일·백업 확장자·2025년 사본)를 두고,
담당자가 손으로 관리해 온 엑셀 관리대장(제목 행·점 날짜·담당자 누락·날짜 오타·이메일 오타·중복 행·폐기 서버 포함 10행)을 사용했습니다. 사용자는 관리자 1·담당자 4·검토자·승인자·조회자 8명입니다.</p>
<div class="tbl"><table><thead><tr><th>서버 경로</th><th>종류</th><th>인증서(CN)</th><th>남은 일수</th></tr></thead><tbody>{files}</tbody></table></div></div>
<h2>배포 범위 (명확히)</h2>
<div class="card callout"><p><b>현재:</b> 배포 승인 시 플랫폼(서버)이 인증서 파일을 단계별 배포 폴더 <span class="mono">deploy/&lt;개발·테스트·교육·운영&gt;/&lt;인증서명&gt;/</span>에 놓고 DEPLOYED.json(지문·만료일)을 기록하며, 이전 파일은 history/에 보관하고 원복 시 되돌립니다.
웹서버(nginx·IIS 등)에 직접 설치하고 재시작하는 단계는 <b>각 서버 담당자</b>가 수행합니다 — Agent가 서버 설정을 바꾸지는 않습니다.</p>
<p><b>다음 단계(AWS 테스트):</b> 각 서버의 배포 Agent가 승인된 배포 폴더를 가져가 설치 → 설정 검사(nginx -t 등) → 재적용 → 실패 시 자동 원복하도록 확장합니다.</p></div>
<h2>시나리오별 결과</h2><nav class="toc">{''.join(f"<a href='#{s['id']}'>{s['id']} {e(s['title'])}</a>" for s in R['scenarios'])}<a href="#qa">S13 QA(실제 Claude)</a><a href="#findings">발견사항</a></nav>
{''.join(sc_html)}
<section class="sc" id="qa"><h3><span class="id">S13</span> QA 챗봇 — 실제 Claude 답변과 답변 검증 AI</h3><p class="goal">{e(QA['where'])}</p>
<div class="tbl"><table><thead><tr><th>질문</th><th>답변 요약</th><th>답변 검증 AI</th></tr></thead><tbody>{qa_rows}</tbody></table></div>
<p class="note">{e(QA['self_correction_note'])} — 오류 주입 실험: 부적합 탐지 {SCE['detected']}/{SCE['n']}, 재작성 후 적합 {SCE['fixed']}/{SCE['n']}, 평균 정확도 {SCE['acc_before']} → {SCE['acc_after']}점.</p>
<div class="shots"><figure><img src="shots/S13_qa_claude_verify.jpg" alt=""><figcaption>Claude 답변 + 답변 검증 AI(적합) + Agent 추론 로그</figcaption></figure><figure><img src="shots/S13_qa_claude_caution_guard.jpg" alt=""><figcaption>근거 없는 질문 → 검증 AI '주의'(추측성 안내 지적), 비밀번호·개인키 요구 → 가드레일 차단</figcaption></figure></div></section>
<h2 id="findings">테스트 중 발견사항과 조치</h2>
<div class="card"><div class="tbl"><table><thead><tr><th>시나리오</th><th>증상</th><th>원인</th><th>조치</th><th>재검증</th></tr></thead><tbody>{f_rows}</tbody></table></div>
<p class="note">모든 조치 후 회귀 테스트: 서버판 E2E 59/59 · 웹판 12/12 · QA·PDF 13/13 · 규칙 일치 4/4 · 인수 시나리오 {R['pass']}/{R['total']}</p></div>
</main></body></html>"""
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print("report:", OUT / "index.html")


if __name__ == "__main__":
    main()

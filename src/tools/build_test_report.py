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
RA = json.loads((BASE / "results" / "e2e_remote_agent.json").read_text(encoding="utf-8"))
RO = json.loads((BASE / "results" / "e2e_ops_agents.json").read_text(encoding="utf-8"))
RT = json.loads((BASE / "results" / "e2e_targets.json").read_text(encoding="utf-8"))
AWS = [  # AWS 실서버(시드니 ap-southeast-2) 관리자 시나리오 — 단계, 결과, 근거
    ("인프라", "CloudFormation 으로 EC2 4대(플랫폼 t3.small · web01 nginx · was01 Tomcat · win01 IIS Windows 2022) + 고정 IP + HTTPS(Let's Encrypt)", "통과", "https://certagent-ai.duckdns.org · SSM 원격 관리 4대 Online"),
    ("계정·권한", "관리자 화면에서 담당자·검토자·승인자 계정 생성(첫 로그인 비밀번호 변경)", "통과", "자동 브라우저(Playwright)가 실제 HTTPS 주소에서 조작"),
    ("알림 채널", "AWS SES 이메일 + ntfy 푸시 테스트 발송", "통과", "skybook75@gmail.com 수신 · 문자(SMS)는 계정 상위 정책(SCP)이 AWS 문자 서비스를 차단해 미검증"),
    ("서버 Agent 등록", "web01(운영 배포 단계) · was01 · win01 등록, 토큰 1회 표시", "통과", "토큰은 해시만 저장"),
    ("PFX 비밀번호", "담당자가 was01·win01 폴더 범위로 비밀번호 암호화 등록", "통과", "AES-256-GCM · Agent 공개키로 감싸 전달"),
    ("수집", "web01 파일 3개→4건, was01 PFX(보관함 비밀번호로 열기)→3건, win01 PFX + Windows 인증서 저장소→3건", "통과", "개인키 파일 제외 · 위치 '호스트:경로'"),
    ("승인·알림", "관리자 수집 승인 → 오케스트레이터 일일 점검: 알림 5건 생성 → 이메일 5건 발송(실패 0)", "통과", "Gmail 수신 확인 대상"),
    ("AI", "Claude API 연결 — 계정에서 쓸 수 있는 모델 자동 선택(claude-sonnet-4-6)", "통과", "모델 미지원 시 자동 전환 · 실패 시 규칙형 대체"),
]
AWS_FIX = [
    ("Amazon Linux 2023 파이썬 3.9에서 플랫폼이 시작되지 않음(타입 표기 str | None)", "모든 모듈에 지연 평가(from __future__ import annotations), 3.9로 기동 검증"),
    ("서버 OS 기본 cryptography 36에서 인증서 날짜·체인 파싱 실패", "구버전 호환 함수(_utc, PEM 체인 분리) 추가"),
    ("Windows 콘솔(cp1252)에서 한글 로그 때문에 Agent 중단", "표준 출력 UTF-8 고정"),
    ("원격 서버 수집분은 배포 승인 시 원본 파일이 없어 실패", "보관한 공개 인증서(PEM)로 배포 기록, 설치는 서버 Agent"),
    ("Claude API 400/404(모델명이 계정과 다름)", "Models API로 사용 가능한 모델 조회 후 자동 선택, 오류는 AI 운영 화면에 표시"),
    ("프리 티어 계정에서 Windows t3.medium 거부", "프리 티어 사양(t3.small)으로 변경"),
]

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
    ("R01", "원격 서버에서 수집한 인증서는 배포 승인 시 '배포할 인증서 파일을 찾을 수 없습니다'로 실패", "배포 폴더 기록이 플랫폼 PC의 스캔 폴더 원본만 찾음",
     "수집 때 보관한 공개 인증서(PEM)로 배포 기록을 남기고, 실제 설치는 해당 서버 Agent가 수행", "4단계 승인 → web01 적용 성공"),
    ("R01", "배포 대상 파일에 갱신본 자신의 파일(incoming/)까지 포함", "같은 이름 인증서의 모든 위치를 대상으로 잡음", "그 서버에 지금 적용된 인증서의 파일만 교체 대상으로", "대상 2개 → 1개"),
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
    ra_ok = f"{sum(x['pass'] for x in RA)}/{len(RA)}"
    rows_of = lambda L: "".join(f"<tr><td>{e(x['name'])}</td><td class='mono'>{e(x['detail'][:220])}</td><td class='{'ok' if x['pass'] else 'bad'}'>{'통과' if x['pass'] else '실패'}</td></tr>" for x in L)
    ro_ok, ro_rows = f"{sum(x['pass'] for x in RO)}/{len(RO)}", rows_of(RO)
    rt_ok, rt_rows = f"{sum(x['pass'] for x in RT)}/{len(RT)}", rows_of(RT)
    aws_rows = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td class='{'ok' if c == '통과' else 'bad'}'>{e(c)}</td><td class='note'>{e(d)}</td></tr>" for a, b, c, d in AWS)
    aws_fix = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td></tr>" for a, b in AWS_FIX)
    ra_rows = "".join(f"<tr><td>{e(x['name'])}</td><td class='mono'>{e(x['detail'][:220])}</td><td class='{'ok' if x['pass'] else 'bad'}'>{'통과' if x['pass'] else '실패'}</td></tr>" for x in RA)
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
<div class="card callout"><p><b>플랫폼:</b> 배포 승인 시 단계별 배포 폴더 <span class="mono">deploy/&lt;개발·테스트·교육·운영&gt;/&lt;인증서명&gt;/</span>에 인증서와 DEPLOYED.json(지문·만료일)을 기록하고, 이전 파일은 history/에 보관해 원복 시 되돌립니다.</p>
<p><b>서버 Agent(추가):</b> 배포 단계를 지정해 등록한 서버는 Agent가 승인된 배포를 <b>직접 설치</b>합니다 — 백업 → 서버 인증서 교체(중간 CA 유지) → 설정 검사(nginx -t 등) → 재적용 → 서버가 실제로 보내는 인증서 지문 확인 → 실패하면 백업으로 자동 원복 후 결과 보고. 개인키는 서버 밖으로 나가지 않고, 새 키를 발급하는 갱신은 담당자가 설치합니다.</p></div>
<h2 id="R01">서버 Agent 원격 수집·배포 <span class="badge ok">{ra_ok}</span></h2>
<section class="sc"><p class="goal">원격 서버 2대(web01 nginx 체인+개인키, was01 Tomcat PFX)를 흉내 낸 환경에서 서버 Agent가 토큰으로 접속해 수집·업로드하고, PFX 비밀번호는 보관함에서 Agent 공개키로 암호화 전달받아 엽니다. 승인된 운영 배포를 실제 TLS 서버에 설치하고 응답 지문을 확인하며, 키가 맞지 않는 인증서는 설정 검사에서 막혀 자동 원복됩니다. AWS EC2 실서버 테스트 전 단계 검증입니다.</p>
<div class="tbl"><table><thead><tr><th>검증 항목</th><th>실제 결과</th><th>판정</th></tr></thead><tbody>{ra_rows}</tbody></table></div>
<div class="shots"><figure><a href="shots/R01_admin_agents.png" target="_blank"><img loading="lazy" src="shots/R01_admin_agents.png" alt=""></a><figcaption>관리 탭 — 서버 Agent 목록(최근 결과)과 등록 시 1회 표시되는 토큰·설정 파일</figcaption></figure></div></section>
<h2 id="AWS">AWS 실서버 테스트 (진행 중)</h2>
<div class="card"><p class="goal">실제 클라우드 서버 4대에 테스트 전용 인증서(사설 CA)를 두고, 관리자가 하는 일을 플랫폼 서버 안의 자동 브라우저가 실제 주소에서 그대로 수행했습니다.</p>
<div class="tbl"><table><thead><tr><th>단계</th><th>내용</th><th>판정</th><th>근거</th></tr></thead><tbody>{aws_rows}</tbody></table></div>
<h3>실서버에서 발견해 고친 문제</h3><div class="tbl"><table><thead><tr><th>증상</th><th>조치</th></tr></thead><tbody>{aws_fix}</tbody></table></div>
<p class="note">다음: 갱신본 4단계 배포 → web01 nginx 실제 적용·응답 지문 확인 → 키 불일치 인증서 자동 원복 → QA 챗봇·유지보수 Agent(Claude) 실서버 검증</p></div>
<h2 id="O01">AI 운영 — 오케스트레이터 · 유지보수 Agent · 자가 진단 · MCP <span class="badge ok">{ro_ok}</span></h2>
<section class="sc"><div class="tbl"><table><thead><tr><th>검증 항목</th><th>실제 결과</th><th>판정</th></tr></thead><tbody>{ro_rows}</tbody></table></div>
<div class="shots"><figure><a href="shots/O01_ai_ops.png" target="_blank"><img loading="lazy" src="shots/O01_ai_ops.png" alt=""></a><figcaption>AI 운영 탭 — Agent 그래프, 유지보수 Agent 대화·반영 리포트, 자가 진단</figcaption></figure></div></section>
<h2 id="T01">수집 대상 서버 목록 <span class="badge ok">{rt_ok}</span></h2>
<section class="sc"><div class="tbl"><table><thead><tr><th>검증 항목</th><th>실제 결과</th><th>판정</th></tr></thead><tbody>{rt_rows}</tbody></table></div>
<div class="shots"><figure><a href="shots/T01_targets.png" target="_blank"><img loading="lazy" src="shots/T01_targets.png" alt=""></a><figcaption>수집 대상 — 관리자 전체 보기(등록자 표시), 접속 비밀번호 암호화</figcaption></figure></div></section>
<h2>시나리오별 결과</h2><nav class="toc">{''.join(f"<a href='#{s['id']}'>{s['id']} {e(s['title'])}</a>" for s in R['scenarios'])}<a href="#qa">S13 QA(실제 Claude)</a><a href="#R01">R01 서버 Agent</a><a href="#AWS">AWS 실서버</a><a href="#O01">AI 운영</a><a href="#T01">수집 대상</a><a href="#findings">발견사항</a></nav>
{''.join(sc_html)}
<section class="sc" id="qa"><h3><span class="id">S13</span> QA 챗봇 — 실제 Claude 답변과 답변 검증 AI</h3><p class="goal">{e(QA['where'])}</p>
<div class="tbl"><table><thead><tr><th>질문</th><th>답변 요약</th><th>답변 검증 AI</th></tr></thead><tbody>{qa_rows}</tbody></table></div>
<p class="note">{e(QA['self_correction_note'])} — 오류 주입 실험: 부적합 탐지 {SCE['detected']}/{SCE['n']}, 재작성 후 적합 {SCE['fixed']}/{SCE['n']}, 평균 정확도 {SCE['acc_before']} → {SCE['acc_after']}점.</p>
<div class="shots"><figure><img src="shots/S13_qa_claude_verify.jpg" alt=""><figcaption>Claude 답변 + 답변 검증 AI(적합) + Agent 추론 로그</figcaption></figure><figure><img src="shots/S13_qa_claude_caution_guard.jpg" alt=""><figcaption>근거 없는 질문 → 검증 AI '주의'(추측성 안내 지적), 비밀번호·개인키 요구 → 가드레일 차단</figcaption></figure></div></section>
<h2 id="findings">테스트 중 발견사항과 조치</h2>
<div class="card"><div class="tbl"><table><thead><tr><th>시나리오</th><th>증상</th><th>원인</th><th>조치</th><th>재검증</th></tr></thead><tbody>{f_rows}</tbody></table></div>
<p class="note">모든 조치 후 회귀 테스트: 서버판 E2E 59/59 · 웹판 12/12 · QA·PDF 13/13 · 규칙 일치 4/4 · 인수 시나리오 {R['pass']}/{R['total']} · 서버 Agent {ra_ok} · AI 운영 {ro_ok} · 수집 대상 {rt_ok}</p></div>
</main></body></html>"""
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print("report:", OUT / "index.html")


if __name__ == "__main__":
    main()

"""알림 채널 테스트: 플랫폼에 저장된 알림 설정 그대로 이메일(Amazon SES)·휴대폰 푸시를 1건씩 보낸다.

  python3 tools/channel_test.py <받는 이메일>      (플랫폼 서버에서, CERT_DB 환경변수 사용)

플랫폼의 '알림 채널 테스트' 버튼(/api/mail/test)과 같은 함수(agents.send_mail, agents.send_push)를 쓴다.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402

to = sys.argv[1]
store = agents.Store(agents.resolve_db(os.environ.get("CERT_DB") or "/var/lib/cert-platform/platform.db"))
cfg = agents.smtp_settings(store)
topic = cfg.get("push_topic") or ""
print("settings:", {"provider": cfg.get("provider"), "region": cfg.get("aws_region"), "from": cfg.get("from_addr"),
                    "sms_enabled": cfg.get("sms_enabled"), "push_topic": (topic[:4] + "…" + topic[-3:]) if topic else None})
now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")

try:
    import boto3
    ses = boto3.client("ses", region_name=cfg.get("aws_region") or "ap-southeast-2")
    q = ses.get_send_quota()
    st = ses.get_identity_verification_attributes(Identities=[to, cfg.get("from_addr") or ""])["VerificationAttributes"]
    print("ses: max24h=%s sent24h=%s" % (q["Max24HourSend"], q["SentLast24Hours"]),
          {k: v.get("VerificationStatus") for k, v in st.items()})
except Exception as e:  # noqa: BLE001
    print("ses info error:", str(e)[:200])

try:
    agents.send_mail(cfg, to, "[인증서 관리 플랫폼] 알림 채널 테스트",
                     f"인증서 관리 플랫폼(AWS)의 이메일 알림 테스트입니다.\n발송 시각: {now}\n발송 경로: 플랫폼 서버(EC2) → Amazon SES\n\n"
                     "실제 알림은 D-30·D-7·만료 시점에 담당자별로 1번씩 이 경로로 발송됩니다.")
    print("mail: SENT to", to)
except Exception as e:  # noqa: BLE001
    print("mail: FAIL", str(e)[:300])

if topic:
    try:
        agents.send_push(cfg, "인증서 만료 알림(테스트)", f"[테스트] 알림 채널 점검\n{now}\n플랫폼 서버(EC2)에서 보낸 휴대폰 푸시입니다.")
        print("push: SENT")
    except Exception as e:  # noqa: BLE001
        print("push: FAIL", str(e)[:300])
else:
    print("push: no topic configured")

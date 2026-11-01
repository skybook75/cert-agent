# 시연 영상 내레이션 음성 생성: narration.json([{"id","text"}]) → out/demo_tts/<id>.mp3
#   목소리: 한국어 남성 신경망 음성(ko-KR-InJoonNeural), 차분하게 약간 느린 속도
#   python3 tts.py narration.json /var/lib/cert-lab-run/out/demo_tts
import asyncio, json, sys
from pathlib import Path
import edge_tts

VOICE, RATE, PITCH = "ko-KR-InJoonNeural", "-6%", "-2Hz"


async def main(src, out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    items = json.loads(Path(src).read_text(encoding="utf-8"))
    for it in items:
        f = out / f"{it['id']}.mp3"
        for attempt in range(3):
            try:
                await edge_tts.Communicate(it["text"], VOICE, rate=RATE, pitch=PITCH).save(str(f))
                break
            except Exception as e:  # noqa: BLE001
                print("retry", it["id"], repr(e)[:120])
                await asyncio.sleep(3)
        print("ok" if f.exists() and f.stat().st_size > 1000 else "FAIL", it["id"], f.stat().st_size if f.exists() else 0)


asyncio.run(main(sys.argv[1], sys.argv[2]))

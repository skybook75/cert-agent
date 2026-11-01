# 시연 영상 내레이션 음성 생성: narration.json([{"id","text","speak"?}]) → <out>/<id>.mp3
#   python3 tts.py narration.json <out폴더> [목소리]
#   speak: 발음용 문장(영문 약어·숫자를 한국어 읽기로 바꾼 것). 없으면 text 를 읽는다. 자막은 text 를 쓴다.
#   목소리 기본값: ko-KR-SunHiNeural(여성, 자연스러운 억양). 속도·음높이는 기본값(인위적 보정 없음)
import asyncio, json, sys
from pathlib import Path
import edge_tts

VOICE = sys.argv[3] if len(sys.argv) > 3 else "ko-KR-SunHiNeural"


async def main(src, out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    items = json.loads(Path(src).read_text(encoding="utf-8"))
    for it in items:
        f = out / f"{it['id']}.mp3"
        for attempt in range(3):
            try:
                await edge_tts.Communicate(it.get("speak") or it["text"], it.get("voice") or VOICE).save(str(f))
                break
            except Exception as e:  # noqa: BLE001
                print("retry", it["id"], repr(e)[:120])
                await asyncio.sleep(3)
        print("ok" if f.exists() and f.stat().st_size > 1000 else "FAIL", it["id"], f.stat().st_size if f.exists() else 0)


asyncio.run(main(sys.argv[1], sys.argv[2]))

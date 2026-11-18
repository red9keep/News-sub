"""Gemini 받아쓰기 모델 비교 시험. 영상마다 모델별로 받아쓰기를 해 걸린 시간 · 글자 수 · 마지막 시각 · 오류를 찍는다.

유튜브 주소는 Gemini가 직접 읽으므로 VPN · 유튜브 요청이 없다. 운영 수집과 같은 transcribe()를 쓴다
(재시도 · 대기 상한 포함). 결과 원문은 trial-out/transcribe/모델/영상ID.txt.

    GitHub Actions: trial.yml (mode=transcribe, videos · model 입력)
    python worker/trial_transcribe.py --videos A_hNNPNYOw0,hlyf2p7-t2k --models gemini-3.6-flash,gemini-3.7-flash
"""
import argparse
import os
import re
import time

import config
import transcribe


def last_stamp(text):
    """받아쓴 텍스트의 마지막 [mm:ss] · [h:mm:ss] → '분:초' (영상 끝까지 받았는지 보기 위해)."""
    stamps = re.findall(r"\[(\d+(?::\d{2}){1,2})\]", text)
    return stamps[-1] if stamps else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", required=True, help="쉼표로 구분한 영상 ID")
    ap.add_argument("--models", default=config.GEMINI_MODEL, help="쉼표로 구분한 모델 ID")
    ap.add_argument("--split", default="", help="나눠 받아쓰기 시험 'N,M': M분짜리 영상을 N분씩 나눠서 (예: 10,44)")
    ap.add_argument("--out", default="trial-out/transcribe")
    args = ap.parse_args()
    duration = None
    if args.split:
        seg, total = (int(x) for x in args.split.split(","))
        config.SEGMENT_MIN, config.MAX_DURATION_MIN, duration = seg, 0, total * 60

    videos = [v.strip() for v in args.videos.split(",") if v.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    rows = []
    config.GEMINI_MODEL_FALLBACKS = []  # 모델끼리 비교하므로 예비 모델로 넘어가지 않게
    for model in models:
        config.GEMINI_MODEL = model  # transcribe._call이 호출 때마다 읽는다
        os.makedirs(os.path.join(args.out, model), exist_ok=True)
        for vid in videos:
            notes = []
            start = time.time()
            try:
                text = transcribe.transcribe(f"https://www.youtube.com/watch?v={vid}", log=notes.append, duration_sec=duration)
                err = ""
            except Exception as e:  # 한도 초과 · 실패 모두 기록하고 다음으로
                text, err = "", f"{type(e).__name__}: {str(e)[:150]}"
            sec = int(time.time() - start)
            if text:
                with open(os.path.join(args.out, model, f"{vid}.txt"), "w", encoding="utf-8") as f:
                    f.write(text)
            rows.append((model, vid, sec, len(text), last_stamp(text), len(notes), err))
            print(f"[{model}] {vid}: {sec}초, {len(text)}자, 마지막 {last_stamp(text)}, 재시도 {len(notes)}"
                  + (f", 오류 {err}" if err else ""), flush=True)
            for n in notes:
                print(f"    {n}", flush=True)
            if text:
                print("    앞부분: " + text[:300].replace("\n", " / "), flush=True)
                if duration:  # 구간 이음매: 시각이 되돌아가거나 크게 비지 않는지
                    secs = [transcribe._seconds(m.group(1)) for m in transcribe.STAMP.finditer(text)]
                    back = [(a, b) for a, b in zip(secs, secs[1:]) if b < a - 5]
                    gaps = [(a, b) for a, b in zip(secs, secs[1:]) if b - a > 180]
                    print(f"    시각 {len(secs)}개, 되돌아감 {back[:5]}, 3분 넘게 빈 곳 {gaps[:5]}", flush=True)

    print("\n| 모델 | 영상 | 시간(초) | 글자 | 마지막 시각 | 재시도 | 오류 |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(str(c) for c in r) + " |")


if __name__ == "__main__":
    main()

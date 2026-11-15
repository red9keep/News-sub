"""종목 의견(picks) 시험 · 지난 영상 채우기. Firestore에 저장된 자막을 다시 요약해 picks만 찍는다.

자막은 transcripts에서 읽으므로 VPN · 유튜브 요청이 없다. 운영 수집과 같은 summarize()를 쓴다.
--save를 주면 그 영상의 summaries.picks와 picks 컬렉션만 고친다 (요약 · 이슈 · 날짜는 그대로).
결과는 trial-out/picks/picks.json · picks.md.

    GitHub Actions: trial.yml (mode=picks, channels · hours · videos · save 입력)
    python worker/trial_picks.py --channels 당잠사 --hours 72
    python worker/trial_picks.py --videos A_hNNPNYOw0 --save
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

import store
import summarize


def recent_summaries(channels, hours, scan=400):
    """최근 요약 [(영상ID, 문서)]. channels: 채널 이름에 들어가는 글자 (비우면 모든 채널)"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    docs = (store.init().collection("summaries")
            .order_by("createdAt", direction=firestore.Query.DESCENDING).limit(scan).stream())
    out = []
    for d in docs:
        s = d.to_dict()
        pub = s.get("publishedAt")
        if pub and datetime.fromisoformat(pub.replace("Z", "+00:00")) < since:
            continue
        if channels and not any(c in (s.get("channelName") or "") for c in channels):
            continue
        out.append((d.id, s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="", help="쉼표로 구분한 채널 이름 (일부만 써도 됨). 비우면 모든 채널")
    ap.add_argument("--hours", type=int, default=72, help="최근 몇 시간 안에 게시된 영상")
    ap.add_argument("--videos", default="", help="쉼표로 구분한 영상 ID (주면 channels · hours 대신)")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--save", action="store_true", help="picks를 Firestore에 저장 (지난 영상 채우기)")
    ap.add_argument("--out", default="trial-out/picks")
    args = ap.parse_args()

    db = store.init()
    if args.videos:
        ids = [v.strip() for v in args.videos.split(",") if v.strip()]
        rows = [(v, (db.collection("summaries").document(v).get().to_dict() or {})) for v in ids]
    else:
        rows = recent_summaries([c.strip() for c in args.channels.split(",") if c.strip()], args.hours)
    rows = rows[:args.max]
    if not rows:
        raise SystemExit("대상 영상이 없습니다.")

    os.makedirs(args.out, exist_ok=True)
    out, md = [], ["| 채널 | 영상 | 의견 | 종목 | 목표주가 | 말한 사람 | 이유 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for n, (vid, s) in enumerate(rows, 1):
        title, ch = s.get("titleKo") or s.get("title") or vid, s.get("channelName", "")
        text = store.get_transcript_text(vid)
        print(f"\n[{n}/{len(rows)}] [{ch}] {title[:60]} ({len(text or ''):,}자)", flush=True)
        if not text:
            print("  자막 없음")
            continue
        t0 = time.time()
        try:
            data = summarize.summarize(s.get("title") or title, ch, text)
        except summarize.SummarizeError as e:
            print(f"  실패: {e}")
            out.append({"id": vid, "channel": ch, "title": title, "error": str(e)})
            continue
        picks, before = data["picks"], len(s.get("picks") or [])
        print(f"  종목 의견 {len(picks)}건 (저장된 것 {before}건, {time.time() - t0:.0f}초)")
        for i in s.get("issues") or []:   # 저장된 요약 줄 · 수치 (고치기 전과 비교용)
            print(f"  (저장됨) ■ {i.get('category')} [{i.get('topic')}]")
            for ln in i.get("lines") or []:
                print(f"    · {ln}")
        for nb in s.get("numbers") or []:
            print(f"  (저장됨) # {nb.get('what')} = {nb.get('value')} ({nb.get('period')})")
        for i in data["issues"]:   # 이슈별 요약 줄 (11-05, 블로그 팝업용) 확인
            print(f"  ■ {i['category']} [{i['topic']}] {i['note'][:60]}")
            for ln in i["lines"]:
                print(f"    · {ln} ({len(ln)}자)")
        for nb in data["numbers"]:
            print(f"  # {nb['what']} = {nb['value']} ({nb['period']})")
        for p in picks:
            print("  " + summarize.pick_line(p))
            price = f"{p['targetPrice']:,} {p['currency']}" if p["targetPrice"] else ""
            md.append(f"| {ch} | {title[:30]} | {summarize.ACTION_KO[p['action']]} | {p['name']} {p['ticker']} | {price} "
                      f"| {p['speaker']}{' (리포트)' if p['source'] == 'report' else ''} | {p['reason']} |")
        out.append({"id": vid, "channel": ch, "title": title, "before": before, "picks": picks,
                    "issues": [{"topic": i["topic"], "note": i["note"], "lines": i["lines"]} for i in data["issues"]]})
        if args.save and s:
            store.set_summary_fields(vid, picks=picks)
            store.save_picks(vid, picks, s)
            print("  저장함")

    with open(os.path.join(args.out, "picks.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "picks.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    total = sum(len(o.get("picks", [])) for o in out)
    before = sum(o.get("before", 0) for o in out)
    print(f"\n영상 {len(out)}개, 종목 의견 {total}건 (저장된 것 {before}건){' (저장함)' if args.save else ''} → {args.out}")


if __name__ == "__main__":
    main()

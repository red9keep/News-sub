"""하루치 자막 시험 수집. Firebase · API 키 없이 자막 파일만 받아 폴더에 저장한다.

주제가 하루에 몇 개 나오는지 보기 위한 1회성 시험용이다. 운영 수집은 collector.py가 한다.

    GitHub Actions: trial.yml (집 공유기 VPN 경유)
    집 PC:          set CAPTIONS_MODE=direct
                    python worker/trial.py

결과: trial-out/날짜/[채널] 제목.txt, index.json (영상별 채널 · 제목 · 게시 시각 · 길이 · 자막 종류)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import captions
import config
import summarize
from store import sanitize_name

HERE = os.path.dirname(os.path.abspath(__file__))


def read_channels(path):
    """`[이름] | URL | 개수 | 시각` 형식의 줄 → [(이름, URL)]."""
    out = []
    for line in open(path, encoding="utf-8"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[1].startswith("http"):
            out.append((parts[0].strip("[]"), parts[1]))
    return out


def published(info):
    if info.get("timestamp"):
        return datetime.fromtimestamp(info["timestamp"], timezone.utc)
    if info.get("upload_date"):
        return datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default=os.path.join(HERE, "trial_channels.txt"))
    ap.add_argument("--hours", type=int, default=24, help="최근 몇 시간 안에 올라온 영상만")
    ap.add_argument("--limit", type=int, default=40, help="채널마다 살펴볼 최신 영상 수")
    ap.add_argument("--playlist-check", type=int, default=3, help="재생목록은 앞쪽 몇 개만 살펴봄")
    ap.add_argument("--max", type=int, default=config.MAX_CAPTIONS_PER_RUN,
                    help="자막을 받아 볼 영상 수 상한 (운영의 실행당 상한과 같게)")
    ap.add_argument("--no-summarize", dest="summarize", action="store_false",
                    help="자막만 받고 Gemini 요약은 하지 않음 (기본: GEMINI_API_KEY가 있으면 요약)")
    ap.add_argument("--videos", default="",
                    help="쉼표로 구분한 영상 ID. 주면 채널 목록 대신 이 영상들만 받는다 (요청 수 시험용)")
    ap.add_argument("--language", default=None, help="채널 language처럼 자막 언어를 지정 (예: ko)")
    ap.add_argument("--out", default="trial-out")
    args = ap.parse_args()

    if config.CAPTIONS_MODE == "off":
        sys.exit("CAPTIONS_MODE=off 라서 실행하지 않습니다.")
    if config.CAPTIONS_MODE == "home" and not config.YT_SOURCE_ADDRESS:
        sys.exit("집 공유기 VPN 주소(YT_SOURCE_ADDRESS)가 없어 실행하지 않습니다. "
                 "집 PC에서 직접 받으려면 CAPTIONS_MODE=direct 로 실행하세요.")

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    out_dir = os.path.join(args.out, datetime.now(config.KST).strftime("%Y-%m-%d"))
    os.makedirs(out_dir, exist_ok=True)

    index, seen, blocked = [], set(), None
    tried, skipped_max = 0, 0  # 운영과 같은 실행당 상한으로 자막 받기를 멈춘다
    try:
        videos = [v.strip() for v in args.videos.split(",") if v.strip()]
        if videos:
            since = datetime.min.replace(tzinfo=timezone.utc)  # 지정한 영상은 게시 시각으로 거르지 않는다
        for name, url in [("지정 영상", None)] if videos else read_channels(args.channels):
            if tried >= args.max:
                print(f"상한 {args.max}개에 도달해 [{name}] 이후 채널은 목록도 조회하지 않습니다.")
                break
            if videos:
                cands = videos
            else:
                entries = captions.latest_entries(url, args.limit)
                if "list=" in url or all(ts is None for _, ts in entries):
                    # 재생목록은 게시 시각이 없어 앞쪽 몇 개를 열어 보고 거른다
                    cands = [vid for vid, _ in entries[:args.playlist_check]]
                else:
                    # 대략적 게시 시각(“3시간 전”)이라 여유를 두고 거른 뒤, 영상 정보의 정확한 시각으로 다시 거른다
                    cands = [vid for vid, ts in entries if ts is None or ts >= since.timestamp() - 3 * 3600]
            print(f"[{name}] 후보 {len(cands)}개", flush=True)

            for vid in cands:
                if vid in seen:
                    print(f"  중복(다른 채널에서 받음): {vid}")
                    continue
                seen.add(vid)
                if tried >= args.max:
                    skipped_max += 1
                    continue
                tried += 1
                before, fb_before = captions.request_count(), captions.fallback_count()
                captions.take_trail()
                try:
                    info, res = captions.fetch_with_info(vid, args.language)
                except captions.CaptionsError as e:
                    print(f"  실패 {vid}: {e}")
                    print("    요청: " + " | ".join(captions.take_trail()))
                    index.append({"id": vid, "channel": name, "error": str(e)[:200]})
                    continue
                pub = published(info)
                if pub and pub < since:
                    print(f"  {args.hours}시간보다 오래됨: {info.get('title', vid)[:40]}")
                    continue
                if info.get("live_status") in ("is_live", "is_upcoming"):
                    print(f"  라이브·예정이라 건너뜀: {info.get('title', vid)[:40]}")
                    continue
                rec = {
                    "id": vid, "channel": name, "title": info.get("title"),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "published": pub.isoformat() if pub else None,
                    "durationSec": info.get("duration"),
                    "captions": res["kind"] if res else None,
                    "language": res["language"] if res else None,
                    "requests": captions.request_count() - before,
                    "fallback": captions.fallback_count() > fb_before,
                    "trail": captions.take_trail(),
                }
                print(f"    요청 {rec['requests']}번{' (기본 설정으로 다시 시도)' if rec['fallback'] else ''}: "
                      + " | ".join(rec["trail"]))
                if res:
                    fname = f"[{sanitize_name(name)}] {sanitize_name(info.get('title') or vid)}.txt"
                    header = (f"제목: {rec['title']}\n채널: {name}\n주소: {rec['url']}\n"
                              f"게시: {rec['published']}\n자막: {res['kind']} ({res['language']})")
                    with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
                        f.write(header + "\n\n" + res["text"] + "\n")
                    rec["file"], rec["chars"] = fname, len(res["text"])
                    print(f"  받음 ({res['kind']}, {res['language']}, {len(res['text']):,}자, {res['track']}): {rec['title'][:40]}")
                    if args.summarize and config.GEMINI_API_KEY:
                        try:
                            rec["summary"] = summarize.summarize(rec["title"], name, res["text"])
                            print(f"    요약: {', '.join(rec['summary']['categories']) or '분류 없음'}")
                        except summarize.SummarizeError as e:
                            rec["summaryError"] = str(e)[:200]
                            print(f"    요약 실패: {e}")
                else:
                    print(f"  자막 없음: {rec['title'][:40]}")
                index.append(rec)
    except captions.Blocked as e:
        blocked = str(e)[:200]
        print(f"유튜브가 요청을 막아 여기서 멈춥니다: {blocked}")

    got = [r for r in index if r.get("file")]
    summary = {
        "createdAt": now.isoformat(), "hours": args.hours, "videos": len(index),
        "withCaptions": len(got), "noCaptions": sum(1 for r in index if "error" not in r and not r.get("file")),
        "errors": sum(1 for r in index if "error" in r), "blocked": blocked,
        "totalMinutes": round(sum(r.get("durationSec") or 0 for r in got) / 60),
        "tried": tried, "skippedByMax": skipped_max,
        "youtubeRequests": captions.request_count(),  # 목록 조회 포함 전체
        "requestKinds": captions.request_breakdown(),
        "fallbacks": captions.fallback_count(),
        "playerClient": ",".join(config.YT_PLAYER_CLIENT), "playerSkip": ",".join(config.YT_PLAYER_SKIP),
        "requestsPerVideo": round(sum(r.get("requests", 0) for r in index) / max(tried, 1), 1),
    }
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "videos": index}, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    if blocked:
        sys.exit(2)


if __name__ == "__main__":
    main()

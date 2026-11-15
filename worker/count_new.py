"""하루에 새 영상이 몇 개 들어오는지 센다. YouTube Data API(공식)만 쓰고 유튜브 페이지 · 자막은 요청하지 않는다.

Firestore의 채널 설정(확인 개수 · 예약 시각)으로 가장 최근 하루(08:20 ~ 17:20 등)를 그대로 흉내 내어
예약 시각마다 새 영상이 몇 개였는지, 상한(실행당 · 하루) 때문에 얼마나 미뤄지는지,
확인 개수 밖으로 밀려 아예 못 보는 영상이 몇 개인지 계산한다.

    GitHub Actions: trial.yml mode=count (VPN 불필요)
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import config
import store
import topic
import youtube


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(config.KST)


def source_playlist(ch):
    if ch.get("playlistId"):
        return ch["playlistId"]
    kind, src = youtube.resolve_source(ch["url"])
    if kind != "playlist":
        raise youtube.YouTubeError(f"재생목록 · 채널 주소가 아닙니다: {ch['url']}")
    if src.startswith("UU"):
        try:  # 운영 수집기와 같은 "동영상" 탭 목록
            youtube._get("playlistItems", part="id", playlistId="UULF" + src[2:], maxResults=1)
            return "UULF" + src[2:]
        except youtube.YouTubeError:
            pass
    return src


def recent_videos(playlist_id, since, max_pages=6):
    """재생목록에서 since 이후 게시된 영상 [(게시 시각, 영상 ID)] (최신순)."""
    out, token = [], None
    for _ in range(max_pages):
        params = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            params["pageToken"] = token
        data = youtube._get("playlistItems", **params)
        for it in data.get("items", []):
            cd = it["contentDetails"]
            if cd.get("videoPublishedAt"):  # 비공개 · 삭제 영상은 시각이 없다
                out.append((parse_ts(cd["videoPublishedAt"]), cd["videoId"]))
        token = data.get("nextPageToken")
        oldest = min((t for t, _ in out), default=None)
        if not token or (oldest and oldest < since):
            break
    return sorted(out, reverse=True)


def at(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def main():
    store.init()
    now = datetime.now(config.KST)
    channels = [ch for _, ch in store.enabled_channels()]
    all_times = sorted({t for ch in channels for t in ch.get("times", [])})
    if not all_times:
        sys.exit("예약 시각이 있는 채널이 없습니다.")

    # 마지막 예약 시각이 지난 가장 최근 날을 하루로 본다. 그 전날 마지막 예약 시각을 출발점으로 삼는다
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now < at(day, all_times[-1]) + timedelta(minutes=10):
        day -= timedelta(days=1)
    start = at(day - timedelta(days=1), all_times[-1])
    print(f"흉내 낼 날: {day:%Y-%m-%d} (출발점 {start:%m-%d %H:%M}), "
          f"상한: 실행당 {config.MAX_CAPTIONS_PER_RUN}개 · 하루 {config.MAX_CAPTIONS_PER_DAY}개\n")

    videos = {}  # 채널 이름 → [(게시 시각, 영상 ID)]
    for ch in channels:
        try:
            videos[ch["name"]] = recent_videos(source_playlist(ch), start - timedelta(hours=6))
        except youtube.YouTubeError as e:
            print(f"[{ch['name']}] 조회 실패: {e}")
            videos[ch["name"]] = []

    def latest(ch, t):
        return [vid for ts, vid in videos[ch["name"]] if ts <= t][:int(ch.get("count", 5))]

    # 출발점: 전날 마지막 예약 시각에 이미 받은 것으로 친다
    seen = set()
    for ch in channels:
        seen.update(latest(ch, start))

    checks = sorted({at(day, t) for ch in channels for t in ch.get("times", [])})
    queue, day_taken, rows = [], 0, []
    found_by_ch = defaultdict(int)
    found_ids = set()
    for t in checks:
        new = []
        for ch in channels:
            if t.strftime("%H:%M") not in ch.get("times", []):
                continue
            for vid in latest(ch, t):
                if vid not in seen:
                    seen.add(vid)
                    found_ids.add(vid)
                    new.append(vid)
                    found_by_ch[ch["name"]] += 1
        queue += new
        take = min(len(queue), config.MAX_CAPTIONS_PER_RUN, config.MAX_CAPTIONS_PER_DAY - day_taken)
        queue, day_taken = queue[take:], day_taken + take
        rows.append({"time": f"{t:%H:%M}", "new": len(new), "taken": take, "waiting": len(queue)})

    # 출발점 ~ 마지막 예약 시각 사이에 올라왔는데 한 번도 확인 개수 안에 들지 못한 영상
    end = checks[-1]
    missed = {ch["name"]: sum(1 for ts, vid in videos[ch["name"]] if start < ts <= end and vid not in seen)
              for ch in channels}
    uploads = {ch["name"]: sum(1 for ts, _ in videos[ch["name"]] if start < ts <= end) for ch in channels}

    print(f"{'시각':<6} {'새 영상':>6} {'받음':>4} {'대기':>4}")
    for r in rows:
        print(f"{r['time']:<6} {r['new']:>6} {r['taken']:>4} {r['waiting']:>4}")
    print(f"\n합계: 새 영상 {sum(r['new'] for r in rows)}개, 받음 {day_taken}개, 다음 날로 넘어감 {len(queue)}개\n")
    print(f"{'채널':<16} {'올라옴':>5} {'찾음':>4} {'놓침':>4}   (출발점 ~ {end:%H:%M})")
    for ch in channels:
        n = ch["name"]
        print(f"{n:<16} {uploads[n]:>5} {found_by_ch[n]:>4} {missed[n]:>4}")

    # 주제 걸러내기 미리 보기: 필수가 아닌 채널에서 찾은 영상을 제목 · 설명으로 판정만 해 본다
    verdict_rows = []
    if config.TOPIC_FILTER and config.GEMINI_API_KEY:
        names = {ch["name"] for ch in channels if not ch.get("required")}
        ids = [vid for n in names for _, vid in videos[n] if vid in found_ids]
        details = youtube.video_details(ids)
        verdicts = topic.classify({v: details[v] for v in ids if v in details})
        if verdicts:
            for vid in ids:
                keep, why = verdicts.get(vid, (True, "판정 없음"))
                title = details.get(vid, {}).get("title", vid)
                verdict_rows.append({"id": vid, "keep": keep, "why": why, "title": title})
            dropped = [r for r in verdict_rows if not r["keep"]]
            print(f"\n주제 걸러내기 미리 보기: 일반 채널 {len(verdict_rows)}개 중 {len(dropped)}개 뺌")
            for r in sorted(verdict_rows, key=lambda r: r["keep"]):
                print(f"  {'받음' if r['keep'] else '뺌  '}  {r['why']:<14} {r['title'][:70]}")
        else:
            print(f"\n주제 판정 실패: {topic.last_error}")

    result = {"day": f"{day:%Y-%m-%d}", "start": start.isoformat(), "checks": rows, "topics": verdict_rows,
              "found": dict(found_by_ch), "uploads": uploads, "missed": missed,
              "taken": day_taken, "carryOver": len(queue),
              "limits": {"perRun": config.MAX_CAPTIONS_PER_RUN, "perDay": config.MAX_CAPTIONS_PER_DAY}}
    with open("count-result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

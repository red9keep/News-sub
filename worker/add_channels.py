"""채널 찾기 · 등록 (trial 모드 `channels`, 11-13). 웹 화면은 limitGroup · language를 못 넣어서 한국 채널은 여기서 등록한다.

    python worker/add_channels.py --find "하나TV 하나증권"   # Data API로 채널 후보 · 최근 업로드 제목 (유튜브 페이지 요청 없음)
    python worker/add_channels.py                            # NEW 목록을 미리 보기만
    python worker/add_channels.py --add                      # NEW 목록을 Firestore channels에 등록 (같은 이름은 건너뜀)

필드는 TEMPLATE 채널(기존 한국 채널)에서 복사한다: 수집 시각 · 묶음(kr) · 언어(ko) 등. 채널별로 다른 것만 NEW에 적는다.
"""
import argparse
import json

from firebase_admin import firestore

import store
import youtube

TEMPLATE = "SBS Biz 증시인사이트"
# 채널마다 다른 값. 나머지(times · limitGroup · language · enabled …)는 TEMPLATE에서 복사
NEW = [
    {"name": "한국경제TV 마켓 브리핑",
     "url": "https://www.youtube.com/playlist?list=PLh6kUo7pqm_7boGq7-3vPXgUN2Xq6vq8i", "count": 6},
    # 하나TV 채널 업로드에 섞여 올라옴 (11-13 확인: 평일 1개, "…l 261113 하나증권 리서치센터 모닝브리프").
    # "▼ 261113 모닝브리프 바로가기" 안내 영상 · 예능 영상은 빼려고 "리서치센터 모닝브리프"만 받는다 (collector.filter_titles)
    {"name": "하나증권 모닝브리프", "url": "https://www.youtube.com/channel/UCP8KMauNQ5YhTL8fzoSYcfQ", "count": 10,
     "titleMatch": r"리서치센터\s*모닝\s*브리프"},
]
# 복사하지 않는 필드 (채널마다 다르거나 수집기가 채우는 것)
NO_COPY = {"name", "url", "playlistId", "lastRunAt", "createdAt", "count", "dayMax", "titleMatch", "required",
           "doneDay", "doneCount", "weekdays"}


def find(query):
    found = youtube._get("search", part="snippet", type="channel", q=query, maxResults=5).get("items", [])
    for it in found:
        cid = it["snippet"]["channelId"]
        ch = (youtube._get("channels", part="snippet,contentDetails", id=cid).get("items") or [{}])[0]
        handle = ch.get("snippet", {}).get("customUrl", "")
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        print(f"\n== {it['snippet']['title']}  {handle}  https://www.youtube.com/channel/{cid}")
        if not uploads:
            continue
        items = youtube._get("playlistItems", part="snippet", playlistId=uploads, maxResults=15).get("items", [])
        for v in items:
            s = v["snippet"]
            print(f"   {s.get('publishedAt', '')[:10]}  {s['title'][:80]}")


def add(apply):
    db = store.init()
    col = db.collection("channels")
    docs = [(d.id, d.to_dict()) for d in col.stream()]
    names = {c.get("name") for _, c in docs}
    tpl = next((c for _, c in docs if c.get("name") == TEMPLATE), None)
    if not tpl:
        raise SystemExit(f"TEMPLATE 채널 '{TEMPLATE}'이 없음")
    base = {k: v for k, v in tpl.items() if k not in NO_COPY}
    print("TEMPLATE에서 복사:", json.dumps(base, ensure_ascii=False, default=str))
    for ch in NEW:
        if not ch["url"]:
            print(f"[건너뜀] {ch['name']}: url이 비어 있음 (--find로 찾아 NEW에 적기)")
            continue
        if ch["name"] in names:
            print(f"[건너뜀] {ch['name']}: 이미 있음")
            continue
        data = {**base, **ch, "enabled": True}
        print(("[등록] " if apply else "[미리 보기] ") + json.dumps(data, ensure_ascii=False, default=str))
        if apply:
            col.add({**data, "createdAt": firestore.SERVER_TIMESTAMP})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--find", default="")
    ap.add_argument("--add", action="store_true")
    a = ap.parse_args()
    if a.find:
        find(a.find)
    else:
        add(a.add)


if __name__ == "__main__":
    main()

"""요약을 묶음 날짜(edition)별 JSON 파일로 Storage에 발행한다 (11-16).

블로그(AlphaScope)는 Firestore `summaries`를 날짜 범위로 통째로 읽어 하루 읽기가 무료 몫 5만을 넘었다.
수집기가 날짜별 파일을 만들어 두면 블로그는 바뀐 파일만 내려받고 Firestore를 읽지 않는다.

    feed/index.json                 {"updatedAt", "editions": {날짜: {"count", "updatedAt"}}, "channelsUpdatedAt"}
    feed/editions/YYYY-MM-DD.json   {"edition", "updatedAt", "count", "items": [summaries 문서 + "id"]}
    feed/channels.json              {"updatedAt", "items": [channels 문서 + "id"]}

파일은 비공개 버킷에 있다 (Storage 규칙: 브라우저는 소유자만). 블로그는 읽기 전용 서비스 계정으로 받는다.
시각은 ISO 글자로 바꾼다 (블로그 media_feed가 파일 캐시에 쓰는 모양과 같음).

수집기는 요약을 쓰거나 고친 영상의 묶음만 다시 만든다 (store.touched_*). 묶음 하나 = 그 날 요약 수만큼 Firestore 읽기.
지난 날짜를 한꺼번에 만들려면: python worker/feed.py --days 35
"""
import argparse
import json
from datetime import date, datetime, timedelta

from firebase_admin import storage
from google.cloud.firestore_v1.base_query import FieldFilter

import config
import store

PREFIX = "feed"
CHANNELS_EVERY = timedelta(hours=6)  # channels.json은 이 간격보다 자주 다시 만들지 않는다


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _upload(path, data):
    blob = storage.bucket().blob(path)
    blob.cache_control = "no-cache"
    blob.upload_from_string(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            content_type="application/json; charset=utf-8")


def _download(path):
    blob = storage.bucket().blob(path)
    try:
        return json.loads(blob.download_as_bytes())
    except Exception:  # 없음 · 깨짐: 새로 만든다
        return None


def _now_iso():
    return datetime.now(config.KST).isoformat(timespec="seconds")


def edition_items(edition):
    docs = store.init().collection("summaries").where(filter=FieldFilter("edition", "==", edition)).stream()
    items = []
    for d in docs:
        r = _jsonable(d.to_dict() or {})
        r["id"] = d.id
        items.append(r)
    items.sort(key=lambda r: str(r.get("createdAt") or ""))
    return items


def publish_channels():
    items = []
    for d in store.init().collection("channels").stream():
        r = _jsonable(d.to_dict() or {})
        r["id"] = d.id
        items.append(r)
    _upload(f"{PREFIX}/channels.json", {"updatedAt": _now_iso(), "items": items})
    return len(items)


def publish(editions, log=print, channels=None):
    """editions의 파일을 다시 만들고 index.json을 고친다. 반환: {날짜: 개수}.
    channels: True면 channels.json도, None이면 마지막으로 만든 지 CHANNELS_EVERY가 지났을 때만."""
    editions = sorted({e for e in editions if e})
    if not editions and not channels:
        return {}
    index = _download(f"{PREFIX}/index.json") or {}
    index.setdefault("editions", {})
    out = {}
    for ed in editions:
        items = edition_items(ed)
        now = _now_iso()
        _upload(f"{PREFIX}/editions/{ed}.json", {"edition": ed, "updatedAt": now, "count": len(items), "items": items})
        index["editions"][ed] = {"count": len(items), "updatedAt": now}
        out[ed] = len(items)
    last = index.get("channelsUpdatedAt")
    if channels is None:
        try:
            channels = not last or datetime.now(config.KST) - datetime.fromisoformat(last) >= CHANNELS_EVERY
        except ValueError:
            channels = True
    if channels:
        n = publish_channels()
        index["channelsUpdatedAt"] = _now_iso()
        log(f"발행: 채널 {n}개 → {PREFIX}/channels.json")
    index["updatedAt"] = _now_iso()
    _upload(f"{PREFIX}/index.json", index)
    if out:
        log("발행: " + ", ".join(f"{ed} {n}개" for ed, n in out.items()) + f" → {PREFIX}/editions/")
    return out


def publish_touched(log=print):
    """이번 실행에서 쓰거나 고친 요약의 묶음만 다시 발행한다. 실패해도 수집은 끝난 것으로 둔다.
    실패한 묶음은 그 날짜 요약이 다시 바뀔 때 또는 trial feed로 다시 발행된다."""
    editions = set(store.touched_editions)
    try:
        if store.touched_videos:  # 묶음 날짜를 모르는 고침 (set_summary_fields): 문서에서 본다
            db = store.init()
            for vid in store.touched_videos:
                snap = db.collection("summaries").document(vid).get()
                if snap.exists:
                    editions.add((snap.to_dict() or {}).get("edition"))
        if not editions:
            return {}
        out = publish(editions, log=log)
        store.touched_editions.clear()
        store.touched_videos.clear()
        return out
    except Exception as e:  # noqa: BLE001 — 발행 실패로 수집 실행이 실패하면 안 된다
        log(f"발행 실패 ({', '.join(sorted(x for x in editions if x)) or '날짜 모름'}, 그 날짜 요약이 다시 바뀔 때 또는 trial feed로 다시): "
            f"{type(e).__name__}: {str(e)[:200]}")
        return {}


def main():
    ap = argparse.ArgumentParser(description="요약을 묶음 날짜별 파일로 Storage에 발행")
    ap.add_argument("--days", type=int, default=35, help="오늘 묶음부터 며칠 전까지 (기본 35)")
    ap.add_argument("--editions", default="", help="쉼표로 구분한 묶음 날짜 (주면 --days 대신)")
    args = ap.parse_args()
    store.init()
    if args.editions:
        eds = [e.strip() for e in args.editions.split(",") if e.strip()]
    else:
        today = date.fromisoformat(config.edition_day(datetime.now(config.KST)))
        eds = [(today - timedelta(days=i)).isoformat() for i in range(args.days + 1)]
    out = publish(eds, channels=True)
    print(f"묶음 {len(out)}개, 요약 {sum(out.values())}개 발행 (Firestore 읽기 약 {sum(out.values())}개)")


if __name__ == "__main__":
    main()

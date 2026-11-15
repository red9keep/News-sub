"""YouTube Data API v3 조회. 영상 자체는 내려받지 않는다."""
import re
from urllib.parse import parse_qs, urlparse

import requests

import config

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    pass


def _get(path, **params):
    params["key"] = config.YOUTUBE_API_KEY
    r = requests.get(f"{API}/{path}", params=params, timeout=20)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise YouTubeError(f"YouTube API {path} {r.status_code}: {msg}")
    return r.json()


def _uploads_playlist(**channel_filter):
    items = _get("channels", part="contentDetails", **channel_filter).get("items", [])
    if not items:
        raise YouTubeError(f"채널을 찾을 수 없습니다: {channel_filter}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def resolve_source(url):
    """채널/재생목록/영상 URL을 조회 대상으로 바꾼다.

    반환: ("playlist", 재생목록 ID) 또는 ("video", 영상 ID)
    """
    u = urlparse(url.strip())
    qs = parse_qs(u.query)
    path = u.path.rstrip("/")

    if "list" in qs:
        return "playlist", qs["list"][0]
    if u.netloc.endswith("youtu.be") and path:
        return "video", path.lstrip("/")
    if "v" in qs:
        return "video", qs["v"][0]

    m = re.match(r"^/(@[^/]+)", path)
    if m:
        return "playlist", _uploads_playlist(forHandle=m.group(1))
    m = re.match(r"^/channel/(UC[\w-]+)", path)
    if m:
        # 채널 ID의 UC를 UU로 바꾸면 업로드 재생목록 ID가 된다
        return "playlist", "UU" + m.group(1)[2:]
    m = re.match(r"^/user/([^/]+)", path)
    if m:
        return "playlist", _uploads_playlist(forUsername=m.group(1))
    raise YouTubeError(f"지원하지 않는 URL 형식입니다: {url}")


def _playlist_video_ids(playlist_id, count):
    data = _get("playlistItems", part="contentDetails", playlistId=playlist_id,
                maxResults=min(max(count, 1), 50))
    return [it["contentDetails"]["videoId"] for it in data.get("items", [])][:count]


def latest_video_ids(playlist_id, count):
    """재생목록의 앞쪽(채널 업로드 목록이면 최신순) 영상 ID 목록.

    채널 업로드 목록(UU…)이면 채널의 "동영상" 탭 목록(UULF…)을 대신 본다. 쇼츠와 지난 생방송이
    빠져 v1과 같은 범위가 된다. 공식 문서에 없는 목록이라 찾지 못하면(404) 전체 업로드 목록으로 돌아간다.
    """
    if re.fullmatch(r"UU[\w-]{22}", playlist_id):
        try:
            return _playlist_video_ids("UULF" + playlist_id[2:], count)
        except YouTubeError as e:
            if " 404:" not in str(e):
                raise
    return _playlist_video_ids(playlist_id, count)


def _parse_duration(iso):
    """ISO 8601 기간(PT1H2M3S)을 초로 변환."""
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def video_details(video_ids):
    """영상 ID → 정보 dict. 비공개·삭제된 영상은 결과에 없다."""
    out = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = _get("videos", part="snippet,contentDetails", id=",".join(chunk))
        for it in data.get("items", []):
            sn = it["snippet"]
            out[it["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "publishedAt": sn.get("publishedAt"),
                "live": sn.get("liveBroadcastContent", "none"),  # none / live / upcoming
                "durationSec": _parse_duration(it["contentDetails"].get("duration")),
            }
    return out

"""유튜브 자막 받기 모듈. yt-dlp로 자막 파일만 받는다 (영상 · 음성은 받지 않음).

GitHub Actions에서는 집 공유기 VPN의 주소(YT_SOURCE_ADDRESS)에 묶어서 요청하므로
VPN이 끊겨도 클라우드 IP로 새지 않고 연결 오류가 난다.
"""
import json
import random
import time
import urllib.parse

import yt_dlp
from yt_dlp.utils import DownloadError

import config
import reasons


class Blocked(Exception):
    """유튜브가 봇 확인 · 요청 제한으로 막았다. 이번 실행의 자막 받기를 멈춘다."""


class CaptionsError(Exception):
    pass


class Unavailable(CaptionsError):
    """다시 해도 못 받는 영상 (삭제 · 비공개 · 국가 제한 등, reasons.PERMANENT). 메시지는 한국어 이유, 원문은 __cause__"""


# yt-dlp 오류 문구 중 차단으로 보는 것
# ("Sign in to confirm your age" 같은 나이 제한은 차단이 아니므로 "confirm you"만으로 판단하지 않는다)
BLOCK_SIGNS = (
    "not a bot", "http error 429", "too many requests", "rate-limited", "try again later",
)

_last_request = 0.0
_requests = 0
_kinds = {}
_trail = []      # 요청 주소(쿼리 뺀 것) 기록. trial이 영상별로 찍는다
_fallbacks = 0   # 줄인 설정(YT_PLAYER_CLIENT)에서 자막을 못 찾아 기본 설정으로 다시 시도한 수
_shared = {}     # 자막 받기용 yt-dlp (실행 동안 재사용). 키: "slim"(줄인 설정) · "default"(yt-dlp 기본)


def _request_kind(req):
    url = req if isinstance(req, str) else getattr(req, "url", "")
    path = urllib.parse.urlparse(url).path
    if path.startswith("/youtubei/"):
        return "API"
    if path.startswith("/s/player/") or path.endswith(".js"):
        return "JS"
    if path.startswith("/api/timedtext"):
        return "자막"
    if path.startswith("/watch"):
        return "페이지"
    return path.strip("/").split("/")[0] or "기타"


class _CountingYDL(yt_dlp.YoutubeDL):
    """yt-dlp가 유튜브로 보내는 HTTP 요청 수를 센다 (페이지 · API · 자막 파일 모두 urlopen을 거친다)."""

    def urlopen(self, req):
        global _requests
        _requests += 1
        kind = _request_kind(req)
        _kinds[kind] = _kinds.get(kind, 0) + 1
        url = urllib.parse.urlparse(req if isinstance(req, str) else getattr(req, "url", ""))
        _trail.append(f"{kind} {url.netloc}{url.path}")
        return super().urlopen(req)


def request_count():
    """이 프로세스에서 지금까지 유튜브로 보낸 요청 수."""
    return _requests


def fallback_count():
    """줄인 설정에서 자막을 못 찾아 기본 설정으로 다시 시도한 수."""
    return _fallbacks


def take_trail():
    """지난번 호출 뒤로 보낸 요청 주소 목록을 돌려주고 비운다."""
    out = _trail[:]
    _trail.clear()
    return out


def request_breakdown():
    """요청 종류별 수 (예: "페이지 3 · API 6 · JS 1 · 자막 3"). 요청이 없으면 빈 문자열."""
    return " · ".join(f"{k} {n}" for k, n in sorted(_kinds.items(), key=lambda kv: -kv[1]))


def _wait_turn():
    """직전 요청과 5~15초(무작위) 간격을 둔다."""
    global _last_request
    lo, hi = config.CAPTION_DELAY_SEC
    gap = random.uniform(lo, hi) - (time.monotonic() - _last_request)
    if _last_request and gap > 0:
        time.sleep(gap)
    _last_request = time.monotonic()


def _ydl(slim=True, **extra):
    """slim이면 YT_PLAYER_CLIENT · YT_PLAYER_SKIP(흉내 낼 앱을 줄인 설정)을 쓴다. False면 yt-dlp 기본."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_retries": 0,  # 막혔을 때 거듭 두드리지 않는다
        "retries": 0,
    }
    if config.YT_SOURCE_ADDRESS:
        opts["source_address"] = config.YT_SOURCE_ADDRESS
    youtube_args = {}
    if slim and config.YT_PLAYER_CLIENT:
        youtube_args["player_client"] = config.YT_PLAYER_CLIENT
    if slim and config.YT_PLAYER_SKIP:
        youtube_args["player_skip"] = config.YT_PLAYER_SKIP
        # JS를 건너뛰면 영상 형식이 비어 "형식 없음" 오류가 난다. 자막만 받으므로 무시
        opts["ignore_no_formats_error"] = True
    extra["extractor_args"] = {**({"youtube": youtube_args} if youtube_args else {}),
                               **extra.get("extractor_args", {})}
    opts.update(extra)
    return _CountingYDL(opts)


def latest_entries(url, limit):
    """채널 · 재생목록의 앞쪽 영상 목록 (시험용, API 키 없이). [(영상 ID, 대략적 게시 시각 또는 None)]"""
    with _ydl(extract_flat=True, playlistend=limit, noplaylist=False,
              extractor_args={"youtubetab": {"approximate_date": [""]}}) as ydl:
        _wait_turn()
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            if _is_block(e):
                raise Blocked(str(e)[:200]) from e
            raise CaptionsError(f"목록 조회 실패: {reasons.explain(e)}") from e
    return [(e["id"], e.get("timestamp")) for e in info.get("entries") or [] if e and e.get("id")]


def _is_block(err):
    msg = str(err).lower()
    return any(s in msg for s in BLOCK_SIGNS)


def pick_track(info, language=None):
    """영상 정보에서 받을 자막을 고른다. 반환: (종류, 언어 키, 트랙 목록) 또는 None.

    1순위 사람이 올린 자막(원래 언어), 2순위 원래 언어의 자동 자막(`xx-orig`).
    자동 번역 자막은 쓰지 않는다. language(채널 설정)를 주면 영상의 언어 표시보다 먼저 쓴다.
    (10-22 당잠사 생방송: -orig가 en · id · ko 셋이고 Actions에서 언어가 en으로 읽혀 en-orig를 골랐다)
    """
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    base = (language or info.get("language") or "").split("-")[0].lower()
    if not base:
        orig = [k for k in auto if k.endswith("-orig")]
        base = orig[0].split("-")[0].lower() if len(orig) == 1 else ""

    def same_lang(key):
        return base and key.lower().split("-")[0] == base

    manual = sorted((k for k in subs if same_lang(k)), key=lambda k: (k.lower() != base, len(k)))
    if manual:
        return "manual", manual[0], subs[manual[0]]
    orig = sorted((k for k in auto if k.endswith("-orig") and same_lang(k)), key=len)
    if orig:
        return "auto", orig[0], auto[orig[0]]
    # 주의: 영어 영상은 원래 자막이 -orig 없이 "en"으로 오고, 엉뚱한 -orig(ar · bn 등)가 하나 붙기도 한다.
    # 그래서 "-orig가 하나뿐이면 그것"은 쓰지 않는다 (10-22 CNBC가 ar-orig로 받아짐). 언어 표시가 틀리는 채널은 채널 language로 정한다
    if base and base in auto and not _is_translation(auto[base]):
        return "auto", base, auto[base]
    # 채널 언어(ko)와 영상 원래 언어(en)가 다르면 auto["ko"]는 기계 번역(tlang=ko) 자막이다.
    # 번역 자막은 유튜브가 429로 막는다 (10-26 당잠사 톰 리 인터뷰가 세 번 연속 429 → 전체 수집이 하루 넘게 멈춤).
    # 이때는 영상의 원래 언어 자막을 받는다 (요약은 원문 언어와 관계없이 한국어로 만든다)
    real = (info.get("language") or "").split("-")[0].lower()
    if real and real != base:
        for key in (f"{real}-orig", real):
            if key in auto and not _is_translation(auto[key]):
                return "auto", key, auto[key]
    return None


def _is_translation(tracks):
    return any("tlang=" in (t.get("url") or "") for t in tracks)


def _fmt_time(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


def parse_json3(raw):
    """json3 자막 → [(시작 초, 문장)] (짧은 조각을 읽기 좋은 길이로 합친다)."""
    data = json.loads(raw)
    pieces = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if text:
            pieces.append((ev.get("tStartMs", 0) / 1000, " ".join(text.split())))

    lines, start, buf = [], None, []
    for t, text in pieces:
        if start is None:
            start = t
        buf.append(text)
        joined = " ".join(buf)
        if (len(joined) >= 120 and joined[-1] in ".?!。") or len(joined) >= 300:
            lines.append((start, joined))
            start, buf = None, []
    if buf:
        lines.append((start, " ".join(buf)))
    return lines


def to_text(lines):
    return "\n".join(f"{_fmt_time(t)} {text}" for t, text in lines)


def fetch(video_id, log=print, language=None):
    """영상 ID → {"text", "kind"(manual/auto), "language"} 또는 자막이 없으면 None.

    막히면 Blocked, 그 밖의 실패는 CaptionsError.
    """
    return fetch_with_info(video_id, language)[1]


def live_wait_reason(info):
    """라이브 중이거나 막 끝나 아직 영상이 덜 된 상태면 그 이유, 아니면 None.

    유튜브 API(liveBroadcastContent)는 방송이 끝나자마자 none이 되지만, 그때는 자동 자막이 아직 없고
    Gemini도 영상을 제대로 못 읽는다 (10-28 증시각도기 받아쓰기가 응답 없이 멈춤).
    """
    status = (info or {}).get("live_status")
    if status in ("is_live", "is_upcoming"):
        return "라이브 중"
    if status == "post_live":
        return "라이브가 끝나 영상 변환 중"
    if status == "was_live":
        start, dur = info.get("release_timestamp") or info.get("timestamp"), info.get("duration")
        if start and dur:
            ended_min = (time.time() - start - dur) / 60
            if ended_min < config.LIVE_SETTLE_MIN:
                return f"라이브가 끝난 지 {max(int(ended_min), 0)}분"
    return None


def fetch_with_info(video_id, language=None):
    """fetch()와 같되 yt-dlp 영상 정보(제목 · 게시일 · 길이 등)도 함께 돌려준다: (info, 결과).

    흉내 낼 앱을 줄인 설정(YT_PLAYER_CLIENT)이 있으면 먼저 그것으로 받고, 자막을 못 찾으면
    yt-dlp 기본 설정(앱 여러 개)으로 한 번 더 시도한다. 줄인 설정이 유튜브 변경으로 자막을 못 주게 되어도
    모두 Gemini 받아쓰기로 넘어가지 않게 하기 위해서다.
    """
    global _fallbacks
    if not (config.YT_PLAYER_CLIENT or config.YT_PLAYER_SKIP):
        return _fetch_once(video_id, language, slim=False)
    try:
        info, result = _fetch_once(video_id, language, slim=True)
        if result:
            return info, result
    except Unavailable:
        raise   # 설정을 바꿔도 못 받는다
    except CaptionsError:
        pass
    _fallbacks += 1
    return _fetch_once(video_id, language, slim=False)


def _fetch_once(video_id, language, slim):
    # yt-dlp 객체를 영상마다 새로 만들면 플레이어 JS(base.js)를 매번 다시 받는다 → 실행 동안 하나를 쓴다
    key = "slim" if slim else "default"
    if key not in _shared:
        _shared[key] = _ydl(slim=slim)
    ydl = _shared[key]
    url = f"https://www.youtube.com/watch?v={video_id}"
    _wait_turn()
    try:
        info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        if _is_block(e):
            raise Blocked(str(e)[:200]) from e
        reason = reasons.permanent(e)
        if reason:
            raise Unavailable(reason) from e
        raise CaptionsError(f"영상 정보 조회 실패: {reasons.explain(e)}") from e

    picked = pick_track(info, language)
    if not picked:
        return info, None
    kind, lang, tracks = picked
    track = next((t for t in tracks if t.get("ext") == "json3"), None)
    if not track:
        raise CaptionsError(f"json3 형식 자막이 없습니다 ({lang})")

    _wait_turn()
    try:
        raw = ydl.urlopen(track["url"]).read().decode("utf-8")
    except Exception as e:
        if _is_block(e):
            raise Blocked(str(e)[:200]) from e
        raise CaptionsError(f"자막 파일 받기 실패: {reasons.explain(e)}") from e

    lines = parse_json3(raw)
    if not lines:
        return info, None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(track["url"]).query)
    return info, {"text": to_text(lines), "kind": kind, "language": lang.removesuffix("-orig"),
                  "track": " ".join(f"{k}={qs[k][0]}" for k in ("lang", "kind", "tlang") if k in qs)}

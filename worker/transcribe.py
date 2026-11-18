"""받아쓰기 모듈. Gemini가 유튜브 URL을 직접 읽는다.

다른 방식(예: yt-dlp + Whisper)으로 바꿀 때는 이 파일의 transcribe()만 같은 형태로 다시 구현하면 된다.
"""
import re
import time

import httpx
from google import genai
from google.genai import errors, types

import config
import reasons

PROMPT = """이 영상의 음성을 들리는 그대로 빠짐없이 받아써 주세요.

규칙:
- 요약하거나 문장을 다듬지 말고, 말한 내용을 처음부터 끝까지 그대로 적습니다.
- 줄마다 맨 앞에 [mm:ss] 형식의 시작 시각을 붙입니다. 1시간이 넘으면 [h:mm:ss]로 적습니다.
- 말하는 언어 그대로 적습니다. 번역하지 않습니다.
- 음성이 없는 구간은 건너뜁니다.
- 받아쓴 내용 외의 설명이나 머리말은 쓰지 않습니다."""


class QuotaExceeded(Exception):
    """일일 한도 초과. 남은 영상은 다음 실행으로 미룬다."""


class TranscribeError(Exception):
    pass


class Busy(TranscribeError):
    """모든 시도가 Gemini 서버 쪽 오류(5xx, 대개 503 혼잡). 실패로 세지 않고 다음 실행으로 미룬다."""


_client = None


def _get_client():
    global _client
    if _client is None:
        # 대기 상한이 없으면 응답이 안 올 때 워크플로 60분 제한까지 멈춘다 (10-28 증시각도기). 단위는 ms
        _client = genai.Client(api_key=config.GEMINI_API_KEY,
                               http_options=types.HttpOptions(timeout=config.TRANSCRIBE_TIMEOUT_MIN * 60_000))
    return _client


def _clock(sec):
    h, rest = divmod(int(sec), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def models():
    """받아쓰기 모델 차례: GEMINI_MODEL 다음 예비 모델들 (겹치는 것은 뺌)"""
    out = []
    for m in [config.GEMINI_MODEL, *config.GEMINI_MODEL_FALLBACKS]:
        if m and m not in out:
            out.append(m)
    return out


last_model = ""  # 마지막으로 받아쓰기에 성공한 모델 (로그 · 출처 표시용)


def _call(video_url, start=None, end=None, model=None):
    """start · end(초)를 주면 그 구간만 받아쓴다 (Gemini 영상 자르기, 유튜브 주소에도 된다)"""
    clip, prompt = None, PROMPT
    if start is not None:
        clip = types.VideoMetadata(start_offset=f"{start}s", end_offset=f"{end}s")
        prompt += f"\n- 영상의 {_clock(start)} ~ {_clock(end)} 구간만 받아씁니다. 시각은 영상 처음부터 센 시각으로 적습니다."
    resp = _get_client().models.generate_content(
        model=model or config.GEMINI_MODEL,
        contents=types.Content(parts=[
            types.Part(file_data=types.FileData(file_uri=video_url), video_metadata=clip),
            types.Part(text=prompt),
        ]),
        config=types.GenerateContentConfig(
            temperature=0,
            # 음성만 필요하므로 화면은 낮은 해상도로 읽어 토큰을 아낀다
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        ),
    )
    text = (resp.text or "").strip()
    if not text:
        raise TranscribeError("Gemini가 빈 답을 줌")
    return text


class TooLong(TranscribeError):
    """영상이 길어 Gemini 입력 한도를 넘음. 길이를 알면 나눠 받아쓴다"""


def transcribe(video_url, log=print, duration_sec=None):
    """영상 URL → 받아쓴 텍스트. 실패하면 TranscribeError, 한도 초과면 QuotaExceeded.

    duration_sec(영상 길이)가 MAX_DURATION_MIN을 넘거나 한 번에 받다 입력 한도를 넘으면
    SEGMENT_MIN분씩 나눠 받아써 이어 붙인다 (10-29)."""
    if duration_sec and duration_sec > config.MAX_DURATION_MIN * 60:
        return _segmented(video_url, duration_sec, log)
    try:
        return _with_models(lambda m: _call(video_url, model=m), log)
    except TooLong:
        if not duration_sec:
            raise
        log(f"영상이 길어 Gemini 입력 한도를 넘어, {config.SEGMENT_MIN}분씩 나눠 다시 받아씁니다")
        return _segmented(video_url, duration_sec, log)


def _segmented(video_url, duration_sec, log):
    seg = config.SEGMENT_MIN * 60
    starts = list(range(0, int(duration_sec), seg))
    parts = []
    for n, start in enumerate(starts, 1):
        end = min(start + seg, int(duration_sec))
        log(f"나눠 받아쓰기 {n}/{len(starts)} ({_clock(start)} ~ {_clock(end)})")
        text = _with_models(lambda m: _call(video_url, start, end, model=m), log)
        parts.append(_shift(text, start))
    return "\n".join(parts)


class _ModelGone(TranscribeError):
    """모델 이름이 없거나(404) 종료됨(410). 예비 모델이 있으면 넘어간다"""


def _with_models(call, log):
    """받아쓰기 모델을 차례로 쓴다. 429 · 계속된 503 · 모델 없음이면 다음 모델로.

    모두 429면 QuotaExceeded(그 실행의 받아쓰기를 멈춤), 429 · 503만 섞였으면 Busy(실패로 세지 않고 다음 실행),
    그 밖의 실패는 그대로 올린다."""
    global last_model
    order = models()
    errors_seen = []
    for i, model in enumerate(order):
        has_next = i + 1 < len(order)
        try:
            text = _with_retries(lambda: call(model), log, has_next=has_next)
        except (QuotaExceeded, Busy, _ModelGone) as e:
            errors_seen.append(e)
            if has_next:
                log(f"{model} 받아쓰기 안 됨 ({e}) → 예비 모델 {order[i + 1]}로 다시")
                continue
            break
        last_model = model
        return text
    if all(isinstance(e, QuotaExceeded) for e in errors_seen):
        raise errors_seen[-1]
    if all(isinstance(e, (QuotaExceeded, Busy)) for e in errors_seen):
        raise Busy(f"모델 {len(order)}개 모두 한도(429) · 혼잡(503): {errors_seen[-1]}") from errors_seen[-1]
    raise errors_seen[-1]


STAMP = re.compile(r"\[(\d+(?::\d{1,2}){1,2})\]")


def _shift(text, start):
    """구간 받아쓰기의 시각이 구간 처음부터 센 것이면 영상 처음부터로 바꾼다 (모델이 지시를 안 따를 때)"""
    stamps = [_seconds(m.group(1)) for m in STAMP.finditer(text)]
    if not start or not stamps or stamps[0] >= start - 120:
        return text
    return STAMP.sub(lambda m: f"[{_clock(_seconds(m.group(1)) + start)}]", text)


def _seconds(stamp):
    sec = 0
    for part in stamp.split(":"):
        sec = sec * 60 + int(part)
    return sec


def _with_retries(call, log, has_next=False):
    """has_next(예비 모델 있음)면 429에서 60초 기다리지 않고 바로 넘긴다"""
    last_err = None
    rate_limited = False
    server_only = True  # 모든 시도가 5xx였는지
    for attempt in range(1, config.ATTEMPTS_PER_RUN + 1):
        try:
            return call()
        except errors.APIError as e:
            last_err = e
            server_only = server_only and e.code in (500, 503, 504)
            if e.code == 400 and "token" in str(e).lower():
                raise TooLong(reasons.explain(e)) from e
            if e.code in (404, 410):
                raise _ModelGone(f"Gemini 오류: {reasons.explain(e)}") from e
            if e.code == 429:
                # 분당 한도면 1분 기다리면 풀리고, 두 번 연속이면 일일 한도로 본다. 예비 모델이 있으면 바로 넘어간다
                if rate_limited or has_next:
                    raise QuotaExceeded(reasons.explain(e)) from e
                rate_limited = True
                log(f"Gemini 요청 한도에 걸려 60초 기다립니다 ({attempt}회차)")
                time.sleep(60)
                continue
            if e.code in (400, 500, 503, 504):
                # 400은 유튜브 URL 입력에서 간헐적으로 발생한다는 보고가 있어 재시도한다
                wait = 10 * attempt
                log(f"Gemini 오류 {e.code}, {wait}초 후 재시도 ({attempt}/{config.ATTEMPTS_PER_RUN})")
                time.sleep(wait)
                continue
            raise TranscribeError(f"Gemini 오류: {reasons.explain(e)}") from e
        except httpx.TimeoutException as e:
            # 다시 기다려도 같을 가능성이 커서 재시도하지 않는다 (재시도하면 한 영상이 30분을 잡는다)
            raise TranscribeError(f"Gemini 응답 없음 ({config.TRANSCRIBE_TIMEOUT_MIN}분 넘음)") from e
        except httpx.TransportError as e:
            # Server disconnected 등 연결 끊김도 혼잡 때 난다 (10-23 · 10-28 시험). 5xx처럼 다시 시도한다
            last_err = e
            wait = 10 * attempt
            log(f"Gemini 연결 끊김, {wait}초 후 재시도 ({attempt}/{config.ATTEMPTS_PER_RUN})")
            time.sleep(wait)
        except TranscribeError as e:
            last_err = e
            server_only = False
            time.sleep(5)
    if server_only:
        raise Busy(f"{config.ATTEMPTS_PER_RUN}회 모두 {reasons.explain(last_err)}") from last_err
    raise TranscribeError(f"{config.ATTEMPTS_PER_RUN}회 시도 실패: {reasons.explain(last_err)}") from last_err

"""텍스트 → JSON 호출을 제공자별로 감싼다 (요약 · 주제 걸러내기가 쓴다).

    LLM_PROVIDER=gemini (기본) : Gemini API (GEMINI_API_KEY · GEMINI_TEXT_MODEL)
    LLM_PROVIDER=nvidia        : NVIDIA Build, OpenAI 호환 (NVIDIA_API_KEY · NVIDIA_MODEL, 기본 z-ai/glm-5.3)

받아쓰기(transcribe.py)는 유튜브 주소를 직접 읽어야 해서 제공자와 관계없이 Gemini를 쓴다.
한 번 호출로 JSON 하나를 받는다. 429 · 5xx는 잠깐 쉬고 다시, 깨진 JSON은 한 번 더.
"""
import json
import re
import time

import requests

import config
import reasons

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
ATTEMPTS = 4
TIMEOUT_ATTEMPTS = 2   # 시간 초과는 두 번만 (한 영상에 10분 넘게 매달리지 않도록)
NVIDIA_DEADLINE = 240  # 요청 하나에 허용하는 전체 시간(초)
_last_call = 0.0


class LLMError(Exception):
    """재시도해도 안 됐다. 부르는 쪽이 pending · 건너뜀 처리한다."""


def provider():
    return config.LLM_PROVIDER


def _gap():
    """제공자별 호출 간격: Gemini 무료 등급 분당 10회(6초), NVIDIA 분당 40회(2초)."""
    return 2 if config.LLM_PROVIDER == "nvidia" else 6


def _wait_turn():
    global _last_call
    gap = _gap() - (time.monotonic() - _last_call)
    if _last_call and gap > 0:
        time.sleep(gap)
    _last_call = time.monotonic()


def _extract_json(text):
    """응답에서 JSON만 꺼낸다. ```json 울타리나 앞뒤 설명이 붙어도 받아 준다."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("JSON이 없음")
    s = min(starts)
    e = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[s:e + 1])


class _Retry(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class _Timeout(Exception):
    """응답이 너무 늦다. 짧게 쉬고 TIMEOUT_ATTEMPTS번까지만 다시."""


def gemini_models():
    """요약 · 걸러내기에 차례로 쓸 Gemini 모델: 첫 모델 + 예비(GEMINI_TEXT_FALLBACK, "off"면 없음)."""
    models = [config.GEMINI_TEXT_MODEL]
    fb = (config.GEMINI_TEXT_FALLBACK or "").strip()
    if fb and fb.lower() != "off" and fb not in models:
        models.append(fb)
    return models


def last_model():
    """마지막으로 JSON을 받아 온 모델 ("gemini:모델" · "nvidia:모델"). 저장 때 어느 모델 결과인지 남긴다."""
    return _last_model or f"{config.LLM_PROVIDER}:{config.NVIDIA_MODEL if config.LLM_PROVIDER == 'nvidia' else config.GEMINI_TEXT_MODEL}"


_last_model = ""


def _call_gemini(prompt, model):
    from google.genai import errors, types
    from transcribe import _get_client
    try:
        resp = _get_client().models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
        )
    except errors.APIError as e:
        raise _Retry(e.code, f"Gemini 오류 {e.code}: {str(e)[:200]}") from e
    return resp.text or ""


def _call_nvidia(prompt, log=print):
    """NVIDIA Build 호출. 스트리밍으로 받는다: 응답이 느려도 조각이 계속 오므로 읽기 제한에 걸리지
    않고, 전체 시간(NVIDIA_DEADLINE)을 넘기면 끊는다. 10-21 시험에서 비스트리밍 + 180초 제한은
    매 요청이 제한에 걸려 1시간 30분 동안 아무것도 못 받았다."""
    if not config.NVIDIA_API_KEY:
        raise LLMError("NVIDIA_API_KEY 없음")
    body = {
        "model": config.NVIDIA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4096,
        "stream": True,
        # 생각(reasoning)은 끈다. JSON만 필요한데 GLM-5.3은 생각만 14,000자 쓰다 상한에 닿았다 (10-21).
        # 키 이름이 모델마다 달라 알려진 것을 모두 보낸다: vLLM 식 enable_thinking, Z.ai 식 thinking.type
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
    }
    if config.NVIDIA_MODEL.startswith("z-ai/"):
        body["thinking"] = {"type": "disabled"}
    started = time.monotonic()
    parts = []
    reasoning = 0       # 모델이 "생각"에 쓴 글자 수 (content와 따로 온다)
    finish = None
    try:
        with requests.post(NVIDIA_URL, json=body, stream=True, timeout=(15, 90),
                           headers={"Authorization": f"Bearer {config.NVIDIA_API_KEY}",
                                    "Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                raise _Retry(r.status_code, f"NVIDIA 오류 {r.status_code}: {r.text[:200]}")
            for line in r.iter_lines():
                if time.monotonic() - started > NVIDIA_DEADLINE:
                    raise _Timeout(f"NVIDIA 응답이 {NVIDIA_DEADLINE}초를 넘겨 끊음 (받은 글자 {sum(map(len, parts))})")
                if not line or not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    choice = chunk["choices"][0]
                    delta = choice.get("delta") or {}
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                finish = choice.get("finish_reason") or finish
                if delta.get("content"):
                    parts.append(delta["content"])
                reasoning += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
    except requests.Timeout as e:
        raise _Timeout(f"NVIDIA 응답 없음 {int(time.monotonic() - started)}초 (받은 글자 {sum(map(len, parts))})") from e
    log(f"  nvidia {int(time.monotonic() - started)}초, 본문 {sum(map(len, parts))}자, 생각 {reasoning}자, 종료 {finish}")
    if not parts:
        if reasoning:
            # 생각만 하다 토큰 상한에 닿았다. 다시 보내도 같으므로 재시도하지 않는다
            raise LLMError(f"NVIDIA가 생각({reasoning}자)만 하고 본문을 안 냄 (종료 {finish}). 모델 · 옵션을 바꿔야 함")
        raise _Retry(500, "NVIDIA 응답이 비어 있음")
    return "".join(parts)


class _Switch(Exception):
    """이 모델은 그만 쓰고 예비 모델로 넘어간다."""


def generate_json(prompt, log=print, what=""):
    """prompt → 파싱된 JSON. 실패하면 LLMError.

    Gemini는 첫 모델이 404(모델 없음) · 429(하루 한도) · 5xx 두 번이면 예비 모델(GEMINI_TEXT_FALLBACK)로 넘어간다."""
    global _last_model
    if config.LLM_PROVIDER == "nvidia":
        _last_model = ""
        data = _generate_with(lambda p: _call_nvidia(p, log=log), prompt, log, what, has_next=False)
        _last_model = f"nvidia:{config.NVIDIA_MODEL}"
        return data
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY 없음")
    models = gemini_models()
    for i, model in enumerate(models):
        has_next = i + 1 < len(models)
        try:
            data = _generate_with(lambda p, m=model: _call_gemini(p, m), prompt, log, what, has_next)
        except _Switch as e:
            log(f"{model} 실패 ({e}) → 예비 모델 {models[i + 1]}로 다시 ({what})")
            continue
        _last_model = f"gemini:{model}"
        return data
    raise LLMError("모든 모델 실패")  # _Switch는 다음 모델이 있을 때만 나므로 여기 오지 않는다


def _generate_with(call, prompt, log, what, has_next):
    """한 모델로 재시도하며 JSON을 받는다. has_next면 404 · 429 · 5xx 두 번째에 _Switch."""
    last = None
    timeouts = 0
    for attempt in range(1, ATTEMPTS + 1):
        _wait_turn()
        try:
            return _extract_json(call(prompt))
        except _Retry as e:
            last = e
            if has_next and (e.code in (404, 429) or (e.code in (500, 502, 503, 504) and attempt >= 2)):
                raise _Switch(reasons.explain(e)) from e
            if attempt == ATTEMPTS:
                break
            if e.code == 429:
                log(f"{config.LLM_PROVIDER} 요청 한도, 60초 기다립니다 ({what} {attempt}/{ATTEMPTS})")
                time.sleep(60)
            elif e.code in (500, 502, 503, 504):
                wait = 20 * attempt
                log(f"{reasons.explain(e)}, {wait}초 후 다시 ({what} {attempt}/{ATTEMPTS})")
                time.sleep(wait)
            else:
                raise LLMError(reasons.explain(e)) from e
        except (ValueError, TypeError) as e:
            last = e  # 깨진 JSON이면 한 번 더
        except (_Timeout, requests.RequestException) as e:
            last = e
            timeouts += 1
            log(f"{reasons.explain(e)} ({what} {attempt}/{ATTEMPTS})")
            if timeouts >= TIMEOUT_ATTEMPTS:
                break
            time.sleep(10)
    raise LLMError(f"{attempt}회 실패: {reasons.explain(last)}") from last

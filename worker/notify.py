"""Slack 알림 (11-12, AlphaScope 알림과 같은 채널 · 같은 규칙).

- 주소는 비밀값 SLACK_WEBHOOK_URL. 없으면 print만 한다. 보내기가 실패해도 수집은 멈추지 않는다 (주소는 어디에도 찍지 않음)
- 문구는 한국어 원인 한 마디만. 영어 원문 · 예외 이름은 실행 로그(detail)에만 남기고, 실행 로그 링크를 끝 줄에 붙인다
- 정상 결과는 보내지 않는다. 같은 알림(key)은 같은 날(한국 날짜) 한 번만 (Firestore meta/alerts에 보낸 날을 적음)

보내는 것: 수집 0건 · 필수 채널 빠짐 · 자막 실패 많음 (회차 끝에 한 메시지로 모음),
Gemini 하루 한도 · 저장 실패 · 실행 중단 (바로)
"""
import os
from datetime import datetime

import requests

import config

HEAD = "[영상 수집기]"
WEEKDAYS = "월화수목금토일"


def run_url():
    """GitHub Actions 실행 로그 주소 (Actions 밖이면 None)"""
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def _today():
    return datetime.now(config.KST).strftime("%Y-%m-%d")


def _already_sent(key):
    """오늘 이미 보낸 알림이면 True. Firestore를 못 읽으면 보낸다 (알림이 빠지는 것보다 겹치는 게 낫다)"""
    try:
        import store
        return store.alert_day(key) == _today()
    except Exception:
        return False


def _mark_sent(key):
    try:
        import store
        store.set_alert_day(key, _today())
    except Exception as e:
        print(f"알림 보낸 날 기록 실패 (다음 회차에 같은 알림이 또 갈 수 있음): {type(e).__name__}")


def send(lines, key=None, link=True):
    """lines: 앞머리 없는 한국어 줄 목록. key가 있으면 같은 날 한 번만. 보냈으면 True"""
    lines = [l for l in lines if l]
    if not lines:
        return False
    if key and _already_sent(key):
        print(f"오늘 이미 보낸 알림이라 건너뜀: {key}")
        return False
    text = "\n".join(f"{HEAD} {l}" for l in lines)
    url = run_url() if link else None
    if url:
        text += f"\n실행 로그: {url}"
    hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not hook:
        print(f"(Slack 주소 없음, 보내지 않음)\n{text}")
        return False
    try:
        r = requests.post(hook, json={"text": text, "unfurl_links": False}, timeout=10)
        ok = r.status_code == 200
        print(f"Slack 알림 {'보냄' if ok else f'실패 (응답 {r.status_code})'}: {lines[0][:60]}")
    except requests.RequestException as e:
        ok = False
        print(f"Slack 알림 보내기 실패 ({type(e).__name__}), 수집은 계속")
    if ok and key:
        _mark_sent(key)
    return ok


# ---------- 바로 보내는 것 ----------

def gemini_day_limit(what, remaining=None):
    """what: '요약' · '받아쓰기'"""
    left = f", 남은 {remaining}개는 한도가 풀리면 처리" if remaining else ", 남은 영상은 한도가 풀리면 처리"
    send([f"{what} 못 함 — Gemini 하루 한도 다 씀{left}"], key=f"gemini-day-{what}")


def save_failed(cause, count=None):
    n = f", 이번 회차 요약 {count}개 저장 안 됨" if count else ", 요약 저장 안 됨 (다음 실행에서 다시)"
    send([f"저장 실패 — Firestore 쓰기 {cause}{n}"], key="save-failed")


crash_sent = False


def crashed(cause):
    global crash_sent
    crash_sent = True
    send([f"실행 중단 — {cause}, 이번 회차가 끝까지 못 감"], key=f"crashed-{cause}")


# ---------- 회차 끝에 모아 보내는 것 ----------

def is_day_limit(err_text):
    """Gemini 오류 원문(원인 사슬 전체)이 하루 한도(PerDay)면 True"""
    t = (err_text or "").lower()
    return "perday" in t or "per day" in t or "requests_per_day" in t


def required_missing(channels, now, day_key, cutoff_hour=12):
    """필수 채널 중 오늘 영상을 못 받은 채널 이름. 낮 cutoff_hour시 전이거나 그 채널이 쉬는 요일이면 보지 않는다.
    channels: [(id, 채널 설정)]. 요일(월=0)은 채널 weekdays → config.REQUIRED_WEEKDAYS → 월 ~ 금"""
    if now.hour < cutoff_hour:
        return []
    out = []
    for _, ch in channels:
        if not ch.get("required") or ch.get("enabled") is False:
            continue
        days = ch.get("weekdays") or config.REQUIRED_WEEKDAYS.get(ch.get("name")) or [0, 1, 2, 3, 4]
        if now.weekday() not in days:
            continue
        if ch.get("doneDay") != day_key:
            out.append(ch.get("name", "이름 없음"))
    return out


def run_digest(now, zero=None, missing=None, captions_failed=None, summary_failed=None):
    """회차 끝 알림을 한 메시지로. 종류마다 하루 한 번 (key는 종류별로 따로 적음).
    zero: (대상 수, 원인) · missing: [채널 이름] · captions_failed: (실패, 대상) · summary_failed: (개수, 원인)"""
    day = f"{now.month}.{now.day:02d}"
    picks = []
    if zero:
        picks.append(("zero", f"수집 0건 — 받을 영상 {zero[0]}개 중 0개 받음 ({zero[1]})"))
    if missing:
        picks.append(("missing-" + "·".join(sorted(missing)),
                      f"필수 채널 빠짐 — {' · '.join(missing)} ({day} 하루 영상 없음)"))
    if captions_failed:
        picks.append(("captions-failed", f"자막 받기 실패가 많음 — {captions_failed[1]}개 중 {captions_failed[0]}개 실패"))
    if summary_failed:
        picks.append(("summary-failed", f"요약 못 함 — {summary_failed[1]}, {summary_failed[0]}개 (다음 실행에서 다시)"))
    fresh = [(k, l) for k, l in picks if not _already_sent(k)]
    if not fresh:
        return False
    ok = send([l for _, l in fresh])
    if ok:
        for k, _ in fresh:
            _mark_sent(k)
    return ok


if __name__ == "__main__":
    # 시험: 가짜 저장 실패 한 줄을 실제 Slack으로 보낸다 (trial mode notify). 하루 한 번 규칙은 시험 key로 피한다
    import sys
    if "--test" in sys.argv:
        ok = send([f"시험 — 가짜 실패 (저장 실패 흉내: Firestore 쓰기 접속 거부), 무시해도 됨 "
                   f"({datetime.now(config.KST):%m.%d %H:%M})"])
        sys.exit(0 if ok else 1)


def is_mad_money_episode(channel, title):
    """매드머니 본방송인가 — 채널 'CNBC Mad Money' + 원제목 'Mad Money 월/일/연' (클립 · 쇼츠 제외, 요청 문서 40절)."""
    import re
    return channel == config.MAD_MONEY_CHANNEL and bool(re.match(config.MAD_MONEY_TITLE, title or ""))


def blog_dispatch():
    """블로그 방송 기록 실행(media-daily.yml)을 한 번 건다 (요청 문서 39절 Q36). (성공 여부, 한국어 사유).
    토큰은 어디에도 찍지 않는다. 실패해도 수집은 그대로 — 블로그는 11:50 예비 확인으로 받는다."""
    if not config.BLOG_DISPATCH_TOKEN:
        return False, "토큰(ALPHASCOPE_DISPATCH_TOKEN) 없음"
    try:
        r = requests.post(config.BLOG_DISPATCH_URL, timeout=10, json={
            "ref": "main", "inputs": {"scheduled": "true", "phase": "2", "records_only": "true"}},
            headers={"Authorization": f"Bearer {config.BLOG_DISPATCH_TOKEN}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
    except requests.RequestException as e:
        return False, f"요청 실패 ({type(e).__name__})"
    if r.status_code == 204:
        return True, "204"
    return False, f"응답 {r.status_code}"

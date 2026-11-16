"""알파스코프 시황 해설 (11-16, News-doc 요청-알파스코프-시황-해설.md).

하루 두 편. 그 편 시간대에 받은 방송 요약(summaries) + 블로그 묶음 파일(briefing)을 재료로
오름 쪽 · 내림 쪽 시각을 따로 호출해 쓰고, Firestore columns/{날짜}-{am|pm}에 칸으로 나눠 저장한다.
블로그는 status가 ready인 것만 발행한다.

    am  아침 편: 미국 마감 해설 + 오늘 한국장 관전 포인트. 재료 = 전날 20:40 ~ 당일 08:30, 마감 08:30
    pm  저녁 편: 한국 마감 해설 + 오늘 밤 미국장 관전 포인트. 재료 = 당일 08:30 ~ 20:40, 마감 20:40

쓸지 말지는 AI가 아니라 여기서 재료를 세어 정한다 (count_evidence). 근거가 모자란 쪽은 쓰지 않는다.

    python worker/commentary.py --slot am                 # 예약 실행 (이미 있으면 그냥 끝냄)
    python worker/commentary.py --slot pm --date 2026-11-16 --force --dry   # 지난 편 시험, 저장 안 함
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

import requests

import config
import llm

BRIEFING_URL = "https://raw.githubusercontent.com/red6keep-droid/alphascope/gh-pages/briefing/{key}.json"

# 편별 재료 시간대 · 마감 (KST). start는 (며칠 전, 시, 분)
SLOTS = {
    "am": {"start": (-1, 20, 40), "end": (0, 8, 30), "market": "US", "next": "KR"},
    "pm": {"start": (0, 8, 30), "end": (0, 20, 40), "market": "KR", "next": "US"},
}
# 근거 건수에서 뺄 범주: stance가 주가가 아니라 유가 · 통화 · 코인 가격 방향 (Q6)
NOT_EQUITY = {"OIL_ENERGY", "FX_GLOBAL", "CRYPTO"}
MIN_EVIDENCE = 2           # 서로 다른 채널 또는 데이터에서 근거 이만큼이면 그 시각을 쓴다 (처음 2 ~ 3주 보고 맞춤)
MAX_MATERIAL_CHARS = 60_000
# 첫 모델 · 예비 (AI 호출 실패나 틀 어긋남이면 예비로 한 번 더, 그래도 안 되면 그 편 skip)
MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

DIR_KO = {"bull": "오름", "bear": "내림"}
# 2-3절 3 · 5: 독자에게 내보내지 않는 말 (블로그 발행기도 같은 검사를 한다)
FORBIDDEN = re.compile(r"매수|매도|과매수|과매도|추천|목표 진입|D\+\d+|edition|stance|\bA/B\b|유보|테스트 중|"
                       r"강세론|약세론|칼럼|bullish|bearish", re.I)


class Skip(Exception):
    """이 편은 쓰지 않는다. reason은 columns.reason에 그대로 들어간다."""


# ---------- 시간 ----------

def window(day, slot):
    """(재료 시작, 재료 끝 = 작성 마감) KST datetime."""
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=config.KST)

    def at(t):
        d, h, m = t
        return base + timedelta(days=d, hours=h, minutes=m)
    s = SLOTS[slot]
    return at(s["start"]), at(s["end"])


def points_title(day, slot):
    """관전 포인트 칸 제목. 다음 장이 주말이면 '다음 주 관전 포인트'."""
    wd = datetime.strptime(day, "%Y-%m-%d").weekday()   # 월 0 ~ 일 6
    if slot == "am":   # 오늘 한국장
        return "다음 주 관전 포인트" if wd >= 5 else "오늘 한국장 관전 포인트"
    return "다음 주 관전 포인트" if wd >= 5 else "오늘 밤 미국장 관전 포인트"


# ---------- 재료 ----------

def load_summaries(start, end):
    """그 편 시간대에 받은(요약이 저장된) 영상. createdAt 범위 쿼리 하나."""
    import store
    from google.cloud.firestore_v1.base_query import FieldFilter
    q = (store.init().collection("summaries")
         .where(filter=FieldFilter("createdAt", ">=", start))
         .where(filter=FieldFilter("createdAt", "<", end)))
    items = []
    for d in q.stream():
        r = d.to_dict() or {}
        r["id"] = d.id
        items.append(r)
    items.sort(key=lambda r: r.get("createdAt") or start)
    return items


def load_briefing(day, slot, log=print):
    """블로그 묶음 파일. 없음(404) · 깨짐 · 그날 것이 아님이면 None → 방송만으로 쓴다."""
    key = f"{day}-{slot}"
    try:
        r = requests.get(BRIEFING_URL.format(key=key), timeout=15)
    except requests.RequestException as e:
        log(f"묶음 파일 받기 실패 ({e.__class__.__name__}) → 방송만으로")
        return None
    if r.status_code != 200:
        log(f"묶음 파일 없음 (HTTP {r.status_code}) → 방송만으로")
        return None
    try:
        b = r.json()
    except ValueError:
        log("묶음 파일이 JSON이 아님 → 방송만으로")
        return None
    if b.get("date") != day or b.get("slot") != slot or not str(b.get("generated") or "").startswith(day):
        log(f"묶음 파일이 그날 것이 아님 (date {b.get('date')}, generated {b.get('generated')}) → 방송만으로")
        return None
    return b


def count_evidence(items, briefing, market):
    """오름 · 내림 쪽 근거를 센다 (2-1절).

    방송: scope market 전부 + sector 중 NOT_EQUITY를 뺀 이슈의 stance. country가 해설 시장과 다르게 적힌 것은 뺀다(빈 값은 넣음).
    데이터: 묶음 signals[].dir (up · down).
    """
    pool = []   # 건수에 들어가는 이슈 전부 (neutral 포함, 빠진 쪽 한 줄의 '이슈 n건 중'에 씀)
    side = {"bull": {"issues": 0, "channels": set(), "signals": 0, "videos": set()},
            "bear": {"issues": 0, "channels": set(), "signals": 0, "videos": set()}}
    for s in items:
        ch = s.get("channelName") or s.get("channelId") or "?"
        for it in s.get("issues") or []:
            scope, cat, country = it.get("scope"), it.get("category"), (it.get("country") or "").upper()
            if scope == "market" or (scope == "sector" and cat not in NOT_EQUITY):
                if country and country != market:
                    continue
                pool.append(it)
                d = {"bullish": "bull", "bearish": "bear"}.get(it.get("stance"))
                if d:
                    side[d]["issues"] += 1
                    side[d]["channels"].add(ch)
                    side[d]["videos"].add(s["id"])
    for sg in (briefing or {}).get("signals") or []:
        d = {"up": "bull", "down": "bear"}.get(sg.get("dir"))
        if d:
            side[d]["signals"] += 1
    counts = {d: {"issues": v["issues"], "channels": len(v["channels"]), "signals": v["signals"]}
              for d, v in side.items()}
    videos = {d: v["videos"] for d, v in side.items()}
    return counts, len(pool), videos


def enough(c):
    """서로 다른 채널 또는 데이터에서 근거 MIN_EVIDENCE건 이상."""
    return c["channels"] + c["signals"] >= MIN_EVIDENCE


def missing_line(d, c, pool):
    return (f"오늘 {DIR_KO[d]} 쪽 근거는 거의 없었습니다 — 방송 이슈 {pool}건 중 {c['issues']}건, "
            f"채널 {c['channels']}곳")


def point_candidates(items, briefing, limit_topics=4):
    """관전 포인트 후보: 묶음 next_session 일정 + 방송에서 많이 나온 주제 (2-2절). AI는 이 중에서만 고른다."""
    out = []   # [{"text": 후보 글, "time": 시각 또는 ""}]
    for x in ((briefing or {}).get("next_session") or {}).get("items") or []:
        t, w = str(x.get("time") or "").strip(), str(x.get("what") or "").strip()
        if w:
            out.append({"text": f"{t} {w}" if t else w, "time": t})
    # 방송에서 많이 나온 주제: topic은 자유 문구라 잘 겹치지 않으므로, 겹치는 것이 없으면 큰 분류로 센다
    topics, cats = Counter(), Counter()
    for s in items:
        issues = s.get("issues") or []
        for t in {str(it.get("topic") or "").strip() for it in issues} - {""}:
            topics[t] += 1
        for c in {it.get("category") for it in issues} - {None, "OTHER"}:
            cats[c] += 1
    top = [(t, n) for t, n in topics.most_common(limit_topics) if n >= 2]
    if len(top) < 2:
        from summarize import CATEGORIES
        top += [(CATEGORIES.get(c, c).split(" (")[0], n) for c, n in cats.most_common(limit_topics - len(top)) if n >= 2]
    out += [{"text": f"방송에서 많이 다룬 주제: {t} (영상 {n}개)", "time": ""} for t, n in top]
    return out


def material_text(items):
    """방송 요약을 번호 붙인 글로. 자막 원문 인용(picks.quote)은 넣지 않는다 (2-3절 4)."""
    blocks = []
    for n, s in enumerate(items, 1):
        at = s.get("createdAt")
        at = at.astimezone(config.KST).strftime("%m-%d %H:%M") if hasattr(at, "astimezone") else ""
        lines = [f"[영상 {n}] {s.get('channelName', '')} · {s.get('titleKo') or s.get('title', '')} · 받은 시각 {at}",
                 f"요약: {s.get('summary', '')}"]
        for it in s.get("issues") or []:
            lines.append(f"- 이슈 [{it.get('category')} · {it.get('scope') or '?'} · {it.get('stance')} · "
                         f"{it.get('country') or '나라 없음'}] {it.get('topic', '')}: {it.get('note', '')}")
            lines += [f"    · {x}" for x in it.get("lines") or []]
        for p in s.get("picks") or []:
            lines.append(f"- 종목 의견: {p.get('name')}({p.get('ticker') or ''}) {p.get('action')} — "
                         f"{p.get('speaker') or '발언자 모름'}: {p.get('reason', '')}")
        for x in s.get("numbers") or []:
            lines.append(f"- 숫자: {x.get('what')} {x.get('value')} {x.get('period') or ''}".rstrip())
        if s.get("speakers"):
            lines.append(f"- 발언자: {', '.join(s['speakers'])}")
        blocks.append("\n".join(lines))
    out, used = [], 0
    for b in blocks:
        if used + len(b) > MAX_MATERIAL_CHARS:
            out.append(f"(뒤 영상 {len(blocks) - len(out)}개는 분량 때문에 뺌)")
            break
        out.append(b)
        used += len(b)
    return "\n\n".join(out)


# ---------- 프롬프트 ----------

PROMPT = """당신은 주식 투자 블로그 '알파스코프'의 시황 해설을 씁니다. 재료는 아래 방송 요약과 블로그 데이터뿐입니다.

이번 글: {when}. {what}
이번 호출에서 맡은 쪽: **{side_ko} 쪽 시각** — 재료 가운데 {side_ko} 쪽 근거를 모아 그 시각을 가장 설득력 있게 씁니다.
반대쪽 시각은 다른 호출이 따로 씁니다. 이 호출에서 반대쪽을 깎아내리거나 균형을 맞추려 하지 않습니다.
{side_ko} 쪽 근거로 센 방송 이슈는 [영상 번호]가 {side_videos}인 영상에 있습니다 (이 밖의 재료도 써도 됨).

JSON 객체 하나로만 답합니다:
{{
  "title_topic": "글 제목 뒤쪽. 그날 일어난 일을 중립으로 한 구절 (20자 안팎). 예: '금리 인하 기대 꺾인 하루'. 방향을 편들지 않는다",
  "summary": "검색 설명용 한 문장, 한글 45자 안. 그날 일어난 일만 중립으로 (오름 · 내림 어느 쪽 시각도 편들지 않음)",
  "facts": ["오늘 시장에서 있었던 일, 공통 사실 3~4줄. 한 줄 한 문장. 숫자는 글 전체에서 두세 개만. 해석 없이 사실만"],
  "heading": "{side_ko} 쪽 시각의 제목. 이유와 방향을 함께 말한다 (예: 'AI 투자, 실적이 받쳐 준다', '금리 부담에 비싸진 주가'). 25자 안",
  "body": "{side_ko} 쪽 시각 한 단락, 4~7문장. 근거와 해석",
  "points": [{{"pick": 아래 관전 포인트 후보 번호, "text": "그 후보를 독자 말로 다듬은 한 줄, 30자 안. 시각은 쓰지 않는다(따로 붙임). 예측 · 판단 없이 사실만"}}],
            (후보에서만 0~3개. 후보가 없으면 빈 배열),
  "tickers": ["본문 · 사실에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [본문 · 사실의 근거로 쓴 영상 번호 (숫자). 방송에서 가져온 숫자가 있으면 그 영상은 꼭 넣는다]
}}

글쓰기 규칙:
1. 재료에 없는 숫자 · 사건 · 종목은 쓰지 않는다.{numbers_rule}
2. 전망 · 평가는 누가 말했는지 붙인다 ('톰 리(펀드스트랫)는 …라고 봤다'). 우리 판단처럼 쓰지 않는다.
   말한 사람 없이 '~로 보인다 · ~할 전망이다 · 긍정적인 환경이다 · 매력적인 기회다' 같은 판단으로 문장을 끝내지 않는다.
   이 시각은 '어떤 근거가 나왔고 누가 어떻게 봤는지'를 모은 것이다.
3. 다음 말은 쓰지 않는다: 매수 · 매도(순매수 · 순매도 포함) · 과매수 · 과매도 · 추천 · 목표 진입 · 강세론 · 약세론.
   수급은 '외국인이 사들였다 · 팔았다'처럼 쓴다. 계산 신호는 '오름 쪽 · 내림 쪽 · 평소의 n배'처럼 사실의 말로 쓴다.
4. 자막 문장을 그대로 옮기지 않는다. 바꿔 써서 요약한다.
5. 독자 말을 쓴다. 분류 코드(RATES_FED 등) · scope · stance · bullish · bearish · edition · D+20 · A/B · 유보 같은 내부 말은 쓰지 않는다.
6. 종목 이름은 한글(엔비디아 · 삼성전자), 숫자는 '2.04%' · '13억 달러' 식.
7. 분량: 공통 사실 3~4줄, 본문 한 단락 4~7문장. 문장은 모두 '-다'로 끝낸다 ('-습니다' 쓰지 않음).
8. '테스트 중' 같은 표시를 넣지 않는다. 설명이나 머리말 없이 JSON만 출력한다.

관전 포인트 후보 ({points_title}):
{candidates}

블로그 데이터 (묶음 파일):
{briefing}

방송 요약 ({n}개, {span}):
{material}
"""


def build_prompt(day, slot, d, items, briefing, candidates, side_videos):
    start, end = window(day, slot)
    what = ("미국 마감 해설 + 오늘 한국장 관전 포인트 (한국 개장 전에 읽힘)" if slot == "am"
            else "한국 마감 해설 + 오늘 밤 미국장 관전 포인트 (미국 개장 전에 읽힘)")
    if briefing:
        numbers_rule = " 지수 · 업종 · 종목 등락 숫자는 블로그 데이터를 먼저 쓴다."
        if slot == "pm":
            numbers_rule += " 한국 지수(코스피 · 코스닥) 종가는 블로그 데이터에 없으므로 방송에 나온 숫자만 쓴다."
        b = json.dumps({k: v for k, v in briefing.items() if k not in ("links",)}, ensure_ascii=False)
    else:
        numbers_rule = " 오늘은 블로그 데이터가 없다. 숫자는 방송에 나온 것만 쓴다."
        b = "(없음)"
    idx = {s["id"]: n for n, s in enumerate(items, 1)}
    vids = ", ".join(str(idx[v]) for v in sorted(side_videos, key=lambda v: idx[v])) or "(없음, 데이터 근거만)"
    return PROMPT.format(
        when=f"{day} {'아침' if slot == 'am' else '저녁'} 편", what=what, side_ko=DIR_KO[d], side_videos=vids,
        numbers_rule=numbers_rule, points_title=points_title(day, slot),
        candidates="\n".join(f"{n}. {c['text']}" for n, c in enumerate(candidates, 1)) or "(없음 → points는 빈 배열)",
        briefing=b, n=len(items), span=f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST 받음",
        material=material_text(items))


# ---------- 결과 검사 ----------

def _clean(x):
    return " ".join(str(x or "").split()).strip()


def _cut_bytes(s, n):
    b = s.encode("utf-8")
    return s if len(b) <= n else b[:n].decode("utf-8", "ignore").rstrip() + "…"


def pick_points(raw, candidates):
    """AI가 고른 후보 번호 → 관전 포인트 줄. 후보에 없는 것은 버리고, 시각은 후보의 것을 앞에 붙인다 (40자 안)."""
    out, seen = [], set()
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        try:
            n = int(p.get("pick"))
        except (TypeError, ValueError):
            continue
        text = _clean(p.get("text"))
        if not (1 <= n <= len(candidates)) or n in seen or not text:
            continue
        seen.add(n)
        t = candidates[n - 1]["time"]
        line = f"{t} — {text}" if t else text
        if len(line) <= 45:
            out.append(line)
    return out[:3]


def check(raw, items, known_tickers, candidates=()):
    """모델 답 → 다듬은 dict. 틀이 어긋나거나 금지 표현이 있으면 ValueError (예비 모델로 다시)."""
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체가 아님")
    out = {
        "title_topic": _clean(raw.get("title_topic")).strip("'\"")[:40],
        "summary": _cut_bytes(_clean(raw.get("summary")), 150),
        "facts": [_clean(x) for x in raw.get("facts") or [] if _clean(x)][:4],
        "heading": _clean(raw.get("heading")).strip("'\"")[:40],
        "body": _clean(raw.get("body")),
        "points": pick_points(raw.get("points"), list(candidates)),
    }
    for k in ("title_topic", "summary", "heading", "body"):
        if not out[k]:
            raise ValueError(f"{k} 비어 있음")
    if not out["facts"]:
        raise ValueError("facts 비어 있음")
    if not 80 <= len(out["body"]) <= 1200:
        raise ValueError(f"본문 길이 {len(out['body'])}자")
    text =" ".join([out["title_topic"], out["summary"], out["heading"], out["body"], *out["facts"], *out["points"]])
    m = FORBIDDEN.search(text)
    if m:
        raise ValueError(f"금지 표현 '{m.group()}'")
    out["tickers"] = [t for t in dict.fromkeys(str(x).strip().upper() for x in raw.get("tickers") or [])
                      if t in known_tickers and mentioned(t, known_tickers[t], text)]
    srcs = []
    for x in raw.get("sources") or []:
        try:
            n = int(str(x).strip("[] 영상"))
        except ValueError:
            continue
        if 1 <= n <= len(items) and items[n - 1]["id"] not in srcs:
            srcs.append(items[n - 1]["id"])
    out["sources"] = srcs
    return out


def known_tickers(items, briefing):
    """재료에 나온 티커 → 알려진 종목 이름들 (picks · 묶음 movers에서). 이름을 모르면 빈 집합."""
    out = {}

    def add(t, name=""):
        t = str(t or "").strip().upper()
        if t:
            out.setdefault(t, set())
            if name:
                out[t].add(" ".join(str(name).split()))
    for s in items:
        for t in s.get("tickers") or []:
            add(t)
        for it in s.get("issues") or []:
            for t in it.get("tickers") or []:
                add(t)
        for p in s.get("picks") or []:
            add(p.get("ticker"), p.get("name"))
    for m in ((briefing or {}).get("close") or {}).get("movers") or []:
        add(m.get("ticker"), m.get("name"))
    return out


def mentioned(t, names, text):
    """티커나 그 종목 이름이 글에 실제로 나왔는가 (AI가 본문에 없는 종목까지 tickers에 넣는다, 11-16 시험)."""
    if re.search(rf"(?<![A-Z]){re.escape(t)}(?![A-Z])", text):
        return True
    return any(n and n in text for n in names)


def write_view(prompt, items, tickers, candidates, log=print):
    """한 시각을 쓴다. 첫 모델이 실패하거나 틀이 어긋나면 예비 모델로 한 번 더. 둘 다 안 되면 Skip."""
    last = None
    for model in MODELS:
        config.GEMINI_TEXT_MODEL, config.GEMINI_TEXT_FALLBACK = model, "off"
        try:
            raw = llm.generate_json(prompt, log=log, what=f"시황 해설 {model}")
            return check(raw, items, tickers, candidates), f"gemini:{model}"
        except llm.LLMError as e:
            last = f"호출 실패 ({e})"
        except ValueError as e:
            last = f"틀 어긋남 ({e})"
        log(f"  {model}: {last}")
    raise Skip("금지 표현" if last and "금지 표현" in last else "호출 실패")


# ---------- 한 편 ----------

def compose(day, slot, items, briefing, log=print):
    """재료 → columns 문서 칸 (status 제외). 쓰지 않을 편이면 Skip."""
    market = SLOTS[slot]["market"]
    if briefing and briefing.get("session") == "closed":
        raise Skip("휴장")
    if not items:
        raise Skip("영상 없음")
    counts, pool, videos = count_evidence(items, briefing, market)
    log(f"근거: 오름 {counts['bull']} · 내림 {counts['bear']} (건수에 든 이슈 {pool}건)")
    dirs = [d for d in ("bull", "bear") if enough(counts[d])]
    base = {"counts": counts, "briefing": f"{day}-{slot}" if briefing else None}
    if not dirs:
        raise Skip("재료 부족", base)
    cands = point_candidates(items, briefing)
    tickers = known_tickers(items, briefing)
    views, first, models, sources, used = [], None, [], [], []
    for d in dirs:   # 두 시각은 따로 호출. 같은 후보를 주고, 관전 포인트 · 제목 · 사실은 첫 호출 것만 쓴다
        r, model = write_view(build_prompt(day, slot, d, items, briefing, cands, videos[d]), items, tickers, cands, log)
        first = first or r
        models.append(model)
        views.append({"dir": d, "heading": r["heading"], "body": r["body"]})
        sources += [s for s in r["sources"] if s not in sources]
        used += [t for t in r["tickers"] if t not in used]
    lack = [d for d in ("bull", "bear") if d not in dirs]
    dt = datetime.strptime(day, "%Y-%m-%d")
    return {
        **base,
        "date": day, "slot": slot, "reason": "",
        "title": f"{dt.month}.{dt.day:02d} 시황 해설 — {first['title_topic']}",
        "summary": first["summary"],
        "facts": first["facts"],
        "views": views,
        "missing": {"dir": lack[0], "line": missing_line(lack[0], counts[lack[0]], pool)} if lack else None,
        "pointsTitle": points_title(day, slot),
        "points": first["points"],
        "tickers": used,
        "sources": sources,
        "model": " · ".join(dict.fromkeys(models)),
    }


def save(key, doc, log=print):
    """columns/{key}. 이미 있으면 옛 내용을 columns_history/{key}-r{rev}로 옮기고 rev를 올린다."""
    import store
    from firebase_admin import firestore
    db = store.init()
    ref = db.collection("columns").document(key)
    old = ref.get()
    rev = 1
    if old.exists:
        o = old.to_dict() or {}
        rev = int(o.get("rev") or 1) + 1
        db.collection("columns_history").document(f"{key}-r{o.get('rev') or 1}").set(o)
    ref.set({**doc, "rev": rev, "createdAt": firestore.SERVER_TIMESTAMP})
    log(f"저장: columns/{key} rev {rev} status {doc['status']}" + (f" ({doc['reason']})" if doc.get("reason") else ""))


def exists(key):
    import store
    return store.init().collection("columns").document(key).get().exists


def run(day, slot, status, force=False, dry=False, log=print):
    key = f"{day}-{slot}"
    start, end = window(day, slot)
    now = datetime.now(config.KST)
    log(f"시황 해설 {key}: 재료 {start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST, 마감 {end:%H:%M}")
    if not force and not dry and exists(key):
        log("이미 있음 → 끝 (다시 쓰려면 --force)")
        return None
    doc = {"date": day, "slot": slot, "views": [], "missing": None, "points": [], "facts": [],
           "title": "", "summary": "", "tickers": [], "sources": [], "counts": None, "briefing": None,
           "pointsTitle": points_title(day, slot), "model": ""}
    try:
        if not force and now > end:
            raise Skip("마감 넘김")
        items = load_summaries(start, min(end, now) if not force else end)
        log(f"재료 영상 {len(items)}개")
        briefing = load_briefing(day, slot, log)
        doc.update(compose(day, slot, items, briefing, log))
        if not force and datetime.now(config.KST) > end:
            raise Skip("마감 넘김", {k: doc[k] for k in ("counts", "briefing")})
        doc["status"] = status
    except Skip as e:
        doc["status"], doc["reason"] = "skip", e.args[0]
        if len(e.args) > 1:
            doc.update(e.args[1])
        doc.update(views=[], missing=None, points=[], facts=[], title="", summary="", tickers=[], sources=[])
    if dry:
        print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        return doc
    save(key, doc, log)
    return doc


def main():
    ap = argparse.ArgumentParser(description="알파스코프 시황 해설 한 편 쓰기")
    ap.add_argument("--slot", choices=["am", "pm", "auto"], default="auto", help="auto: 낮 12시 전이면 am")
    ap.add_argument("--date", help="편 날짜 YYYY-MM-DD (기본 오늘 KST)")
    ap.add_argument("--status", choices=["draft-test", "ready"],
                    help="다 쓴 글의 status. 기본은 환경 변수 COMMENTARY_STATUS, 없으면 draft-test")
    ap.add_argument("--force", action="store_true", help="마감 · 이미 있음 무시 (지난 편 다시 쓰기, rev 올림)")
    ap.add_argument("--dry", action="store_true", help="저장하지 않고 결과만 출력")
    a = ap.parse_args()
    now = datetime.now(config.KST)
    slot = a.slot if a.slot != "auto" else ("am" if now.hour < 12 else "pm")
    day = a.date or now.strftime("%Y-%m-%d")
    import os
    status = a.status or (os.environ.get("COMMENTARY_STATUS") or "draft-test")
    if status not in ("draft-test", "ready"):
        sys.exit(f"COMMENTARY_STATUS 값이 이상함: {status}")
    run(day, slot, status, force=a.force, dry=a.dry)


if __name__ == "__main__":
    main()

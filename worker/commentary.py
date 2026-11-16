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
import hedge_check
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


def session_label(day, slot, briefing, plain=False):
    """사실 칸 첫 줄의 장 이름 (Q15). 묶음이 있으면 trade_date, 없으면 직전 평일 (am = 미국장, pm = 한국장).

    장 날짜와 글 날짜 사이에 주말이 끼면(월요일 아침 편 등) '지난 금요일 미국장과 주말 사이 소식'."""
    d = datetime.strptime(day, "%Y-%m-%d")
    td = None
    if briefing and briefing.get("trade_date"):
        try:
            td = datetime.strptime(str(briefing["trade_date"])[:10], "%Y-%m-%d")
        except ValueError:
            td = None
    if td is None:
        td = d - timedelta(days=1) if slot == "am" else d
        while td.weekday() >= 5:
            td -= timedelta(days=1)
    mk = "미국장" if slot == "am" else "한국장"
    wd = "월화수목금토일"[td.weekday()]
    gap = (d - td).days - (1 if slot == "am" else 0)   # 장 날짜 뒤 글 날짜까지 빈 날 수
    if gap >= 1 and not plain:
        return f"지난 {wd}요일({td.month}월 {td.day}일) {mk}과 주말 사이 소식"
    return f"{td.month}월 {td.day}일({wd}) {mk}"


def _pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.2f}%"


def index_line(label, briefing):
    """묶음 close.indices가 있으면 지수 등락 한 줄 (Q15). 숫자는 묶음 그대로라 AI를 거치지 않는다."""
    xs = [f"{x.get('name')} {_pct(x.get('chg_pct'))}" for x in ((briefing or {}).get("close") or {}).get("indices") or []
          if x.get("name") and _pct(x.get("chg_pct"))]
    return f"{label}: {' · '.join(xs)}" if xs else ""


def counted_issues(items, market):
    """근거 건수에 드는 이슈 (count_evidence와 같은 기준) → [(영상 번호, 채널, 이슈)]."""
    out = []
    for n, s in enumerate(items, 1):
        ch = s.get("channelName") or s.get("channelId") or "?"
        for it in s.get("issues") or []:
            scope, cat, country = it.get("scope"), it.get("category"), (it.get("country") or "").upper()
            if (scope == "market" or (scope == "sector" and cat not in NOT_EQUITY)) and not (country and country != market):
                out.append((n, ch, it))
    return out


def center_event(items, briefing, market):
    """그날의 중심 사건 (Q11): 건수에 드는 이슈 중 가장 많은 채널이 다룬 큰 분류. 같으면 이슈 수.

    topic은 자유 문구라 잘 겹치지 않아 큰 분류로 고르고, 그 안에서 많이 나온 topic을 같이 준다."""
    from summarize import CATEGORIES
    by = {}
    for n, ch, it in counted_issues(items, market):
        c = it.get("category")
        if not c or c == "OTHER":
            continue
        b = by.setdefault(c, {"channels": set(), "issues": 0, "videos": set(), "topics": Counter()})
        b["channels"].add(ch)
        b["issues"] += 1
        b["videos"].add(n)
        t = str(it.get("topic") or "").strip()
        if t:
            b["topics"][t] += 1
    if not by:
        return None
    cat, b = max(by.items(), key=lambda kv: (len(kv[1]["channels"]), kv[1]["issues"]))
    label = CATEGORIES.get(cat, cat).split(" (")[0]
    move = ""
    close = (briefing or {}).get("close") or {}
    for k, name in (("sectors", "업종"), ("movers", "종목")):
        xs = [x for x in close.get(k) or [] if _pct(x.get("chg_pct"))]
        if xs:
            x = max(xs, key=lambda x: abs(float(x["chg_pct"])))
            move += f" 묶음에서 가장 크게 움직인 {name}: {x.get('name')} {_pct(x['chg_pct'])}."
    return {
        "category": cat, "label": label, "channels": len(b["channels"]),
        "text": (f"{label} — 채널 {len(b['channels'])}곳 · 영상 {', '.join(map(str, sorted(b['videos'])))}에서 다룸. "
                 f"주로 나온 주제: {', '.join(t for t, _ in b['topics'].most_common(3)) or '(없음)'}.{move}"),
    }


def point_candidates(items, briefing, next_market, limit_topics=4):
    """관전 포인트 후보: 묶음 next_session 일정 + 방송에서 많이 나온 주제 (2-2절). AI는 이 중에서만 고른다.

    묶음의 장 시작 줄('… 첫 반응', Q16)은 must → 늘 첫 관전 포인트. 방송 주제는 다음 장 나라(또는 나라 없음) 이슈만."""
    out = []   # [{"text", "time", "must"}]
    for x in ((briefing or {}).get("next_session") or {}).get("items") or []:
        t, w = str(x.get("time") or "").strip(), str(x.get("what") or "").strip()
        if w:
            out.append({"text": f"{t} {w}" if t else w, "time": t, "what": w, "must": "첫 반응" in w})
    topics, cats = Counter(), Counter()
    for s in items:
        issues = [it for it in s.get("issues") or []
                  if it.get("scope") in ("market", "sector") and (it.get("country") or "").upper() in ("", next_market)]
        for t in {str(it.get("topic") or "").strip() for it in issues} - {""}:
            topics[t] += 1
        for c in {it.get("category") for it in issues} - {None, "OTHER"}:
            cats[c] += 1
    top = [(t, n) for t, n in topics.most_common(limit_topics) if n >= 2]
    if len(top) < 2:
        from summarize import CATEGORIES
        top += [(CATEGORIES.get(c, c).split(" (")[0], n) for c, n in cats.most_common(limit_topics - len(top)) if n >= 2]
    out += [{"text": f"방송에서 많이 다룬 주제: {t} (영상 {n}개)", "time": "", "what": "", "must": False} for t, n in top]
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


# ---------- 재료 묶음 (프롬프트 · 검사에 같이 씀) ----------

class Ctx:
    def __init__(self, day, slot, items, briefing):
        import summarize
        self.day, self.slot, self.items, self.briefing = day, slot, items, briefing
        self.market, self.next = SLOTS[slot]["market"], SLOTS[slot]["next"]
        self.material = material_text(items)
        self.briefing_json = (json.dumps({k: v for k, v in briefing.items() if k != "links"}, ensure_ascii=False)
                              if briefing else "")
        self.label = session_label(day, slot, briefing)
        self.index_line = index_line(session_label(day, slot, briefing, plain=True), briefing)
        self.center = center_event(items, briefing, self.market)
        self.candidates = point_candidates(items, briefing, self.next)
        self.tickers = known_tickers(items, briefing)
        # 숫자 대조: 방송 요약 + 묶음 숫자 (묶음 숫자는 % · 그냥 수 둘 다로) + 지수 이름 숫자
        self.numbers = summarize.source_numbers(self.material + "\n" + self.briefing_json)
        self.numbers += [(k, float(v)) for v in (500, 2000, 100, 200, 50, 30) for k in ("n",)]
        self.numbers += [(k, abs(v)) for v in _json_numbers(briefing) for k in ("n", "%")]
        # 비위 주장 대조: 채널별 재료 글 · 묶음 news 글
        self.channel_text = {}
        for s in items:
            ch = s.get("channelName") or s.get("channelId") or "?"
            self.channel_text[ch] = self.channel_text.get(ch, "") + json.dumps(
                {k: s.get(k) for k in ("summary", "issues", "picks", "titleKo", "title")}, ensure_ascii=False, default=str)
        self.news_text = json.dumps((briefing or {}).get("news") or [], ensure_ascii=False)


def _json_numbers(x):
    if isinstance(x, bool):
        return []
    if isinstance(x, (int, float)):
        return [float(x)]
    if isinstance(x, dict):
        return [v for y in x.values() for v in _json_numbers(y)]
    if isinstance(x, list):
        return [v for y in x for v in _json_numbers(y)]
    return []


# ---------- 프롬프트 ----------

RULES = """글쓰기 규칙:
1. 재료에 없는 숫자 · 사건 · 종목은 쓰지 않는다.{numbers_rule}
2. 전망 · 평가는 누가 말했는지 붙인다 ('톰 리(펀드스트랫)는 …라고 봤다'). 우리 판단처럼 쓰지 않는다.
   말한 사람 없이 '~로 보인다 · ~로 전망된다 · ~라는 평가다 · ~가 기대된다'로 문장을 끝내지 않는다.
   말한 사람이 있는 평가도 좋다 · 나쁘다 · 매력적이다 같은 값 매김은 옮기지 않고 숫자 + 사람으로 쓴다
   ('○○는 수익률 6%대를 높은 수준이라고 봤다').
3. 다음 말은 쓰지 않는다: 매수 · 매도(순매수 · 순매도 포함) · 과매수 · 과매도 · 추천 · 목표 진입 · 강세론 · 약세론 ·
   투자 기회 · 매력적 · 담을 만 · 사 둘 만 · 놓치면 안 · 유망.
   수급은 '외국인이 사들였다 · 팔았다'처럼 쓴다. 계산 신호는 '오름 쪽 · 내림 쪽 · 평소의 n배'처럼 사실의 말로 쓴다.
4. 실존 인물 · 회사의 비위 · 사고 주장(스캔들 · 의혹 · 수사 · 사임 · 소송 · 분식 등)은 서로 다른 채널 2곳 이상이 다뤘거나
   블로그 데이터 news에 있을 때만, '~라고 ○○(채널 · 매체)가 전했다'로 쓴다. 한 채널만 말한 것은 쓰지 않는다.
   제목에는 이런 말을 넣지 않는다.
5. 자막 문장을 그대로 옮기지 않는다. 바꿔 써서 요약한다.
6. 독자 말을 쓴다. 분류 코드(RATES_FED 등) · scope · stance · bullish · bearish · edition · D+20 · A/B · 유보 같은 내부 말은 쓰지 않는다.
7. 종목 이름은 한글(엔비디아 · 삼성전자), 숫자는 '2.04%' · '13억 달러' 식.
8. 문장은 모두 '-다'로 끝낸다 ('-습니다' 쓰지 않음). '테스트 중' 같은 표시를 넣지 않는다. 설명이나 머리말 없이 JSON만 출력한다."""

FRAME_PROMPT = """당신은 주식 투자 블로그 '알파스코프'의 시황 해설을 씁니다. 재료는 아래 방송 요약과 블로그 데이터뿐입니다.

이번 글: {when}. {what}
이번 호출은 **공통 틀**(글 제목 · 오늘 있었던 일 · 관전 포인트)만 씁니다. 오름 · 내림 시각은 다른 호출이 씁니다.
어느 쪽 시각도 편들지 않습니다.

오늘의 중심 사건: {center}
이 글이 다루는 장: {label}

JSON 객체 하나로만 답합니다:
{{
  "title_topic": "글 제목 뒤쪽. 중심 사건을 중립으로 한 구절 (20자 안팎). 예: '금리 인하 기대 꺾인 하루'. 방향을 편들지 않는다",
  "summary": "검색 설명용 한 문장, 한글 45자 안. 그날 일어난 일만 중립으로",
  "facts": [{facts_rule}],
  "points": [{{"pick": 아래 관전 포인트 후보 번호, "text": "그 후보를 독자 말로 다듬은 한 줄, 30자 안"}}],
  "tickers": ["사실 칸에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [사실 칸의 근거로 쓴 영상 번호 (숫자). 방송에서 가져온 숫자가 있으면 그 영상은 꼭 넣는다]
}}

사실 칸: 해석 없이 사실만, 한 줄 한 문장. 중심 사건에 관한 사실을 먼저 쓴다. 중심 사건과 이어지지 않는 소식은 넣지 않는다.
숫자는 사실 칸 전체에서 두세 개만.
관전 포인트: {points_rule} 다음 장에서 무엇을 볼지(일정 · 발표 · 숫자)가 드러나게 쓰고, 예측 · 판단은 쓰지 않는다.
시각은 쓰지 않는다(따로 붙임). 다음 장과 이어지지 않는 후보는 고르지 않는다.

{rules}

관전 포인트 후보 ({points_title}):
{candidates}

블로그 데이터 (묶음 파일):
{briefing}

방송 요약 ({n}개, {span}):
{material}
"""

VIEW_PROMPT = """당신은 주식 투자 블로그 '알파스코프'의 시황 해설을 씁니다. 재료는 아래 방송 요약과 블로그 데이터뿐입니다.

이번 글: {when}. {what}
이번 호출에서 맡은 쪽: **{side} 쪽 시각** — 재료 가운데 {side} 쪽 근거를 모아 그 시각을 가장 설득력 있게 씁니다.
반대쪽 시각은 다른 호출이 따로 씁니다. 이 호출에서 반대쪽을 깎아내리거나 균형을 맞추려 하지 않습니다.

오늘의 중심 사건: {center}
질문: **이 중심 사건을 {side} 쪽에서 보면 어떤가?** 중심 사건에 먼저 답하고, 다른 재료는 그 근거로만 쓴다.
중심 사건과 이어지지 않는 재료(그날 시장 흐름과 관계없는 개별 소식)는 쓰지 않는다.
{side} 쪽 근거로 센 방송 이슈는 [영상 번호]가 {side_videos}인 영상에 있습니다.

공통 사실 (다른 호출이 이미 씀, 번호):
{facts}

JSON 객체 하나로만 답합니다:
{{
  "heading": "{side} 쪽 시각의 제목. 이유와 방향을 함께 말한다 (예: 'AI 투자, 실적이 받쳐 준다', '금리 부담에 비싸진 주가'). 25자 안",
  "claim": "주장 한 문장. 중심 사건을 {side} 쪽에서 본 것",
  "evidence": ["근거 2~3개, 한 문장씩. 근거마다 말한 사람이나 채널을 붙인다 (예: 'CNBC에서 ○○는 …라고 봤다')"],
  "risk": "이 시각이 틀리는 조건 한 문장, 사실로 쓴다 (예: '다음 주 엔비디아 실적에서 데이터센터 매출이 줄면 이 시각은 힘을 잃는다')",
  "facts_used": [위 공통 사실 가운데 이 시각의 근거와 이어지는 번호],
  "tickers": ["본문에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [근거로 쓴 영상 번호 (숫자). 방송에서 가져온 숫자가 있으면 그 영상은 꼭 넣는다]
}}

{rules}

블로그 데이터 (묶음 파일):
{briefing}

방송 요약 ({n}개, {span}):
{material}
"""


def _common(ctx):
    start, end = window(ctx.day, ctx.slot)
    what = ("미국 마감 해설 + 오늘 한국장 관전 포인트 (한국 개장 전에 읽힘)" if ctx.slot == "am"
            else "한국 마감 해설 + 오늘 밤 미국장 관전 포인트 (미국 개장 전에 읽힘)")
    if ctx.briefing:
        numbers_rule = " 지수 · 업종 · 종목 등락 숫자는 블로그 데이터를 먼저 쓴다."
        if ctx.slot == "pm":
            numbers_rule += " 한국 지수(코스피 · 코스닥) 종가는 블로그 데이터에 없으므로 방송에 나온 숫자만 쓴다."
    else:
        numbers_rule = " 오늘은 블로그 데이터가 없다. 숫자는 방송에 나온 것만 쓴다."
    return dict(when=f"{ctx.day} {'아침' if ctx.slot == 'am' else '저녁'} 편", what=what,
                center=ctx.center["text"] if ctx.center else "(정하지 못함 — 가장 많이 다룬 시장 흐름을 중심으로)",
                rules=RULES.format(numbers_rule=numbers_rule), briefing=ctx.briefing_json or "(없음)",
                n=len(ctx.items), span=f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST 받음", material=ctx.material)


def frame_prompt(ctx):
    if ctx.index_line:
        facts_rule = (f'"사실 2~3줄. 맨 앞 줄은 이미 있으니 쓰지 않는다: \'{ctx.index_line}\'. 그 뒤에 이어질 줄만"')
    else:
        facts_rule = f'"사실 3~4줄. 첫 줄은 \'{ctx.label}\'로 시작한다 (예: \'{ctx.label}에서 …\')"'
    must = [n for n, c in enumerate(ctx.candidates, 1) if c["must"]]
    points_rule = ("후보에서만 0~3개 고른다. 후보가 없으면 빈 배열." if not must else
                   f"후보 {must[0]}번(장 시작 줄)은 늘 첫 줄로 따로 들어가니 고르지 않는다. 나머지 후보에서 0~2개 고른다.")
    cands = "\n".join(f"{n}. {c['text']}{' (장 시작 줄, 고르지 않음)' if c['must'] else ''}"
                      for n, c in enumerate(ctx.candidates, 1)) or "(없음 → points는 빈 배열)"
    return FRAME_PROMPT.format(**_common(ctx), label=ctx.label, facts_rule=facts_rule, points_rule=points_rule,
                               points_title=points_title(ctx.day, ctx.slot), candidates=cands)


def view_prompt(ctx, d, side_videos, facts):
    idx = {s["id"]: n for n, s in enumerate(ctx.items, 1)}
    vids = ", ".join(str(idx[v]) for v in sorted(side_videos, key=lambda v: idx[v])) or "(없음, 데이터 근거만)"
    return VIEW_PROMPT.format(**_common(ctx), side=DIR_KO[d], side_videos=vids,
                              facts="\n".join(f"{n}. {f}" for n, f in enumerate(facts, 1)))


# ---------- 결과 검사 ----------

def _clean(x):
    return " ".join(str(x or "").split()).strip()


def _cut_bytes(s, n):
    b = s.encode("utf-8")
    return s if len(b) <= n else b[:n].decode("utf-8", "ignore").rstrip() + "…"


def _ints(xs):
    out = []
    for x in xs if isinstance(xs, list) else []:
        try:
            out.append(int(str(x).strip("[] 영상번")))
        except ValueError:
            continue
    return out


def pick_points(raw, candidates):
    """장 시작 줄(must)은 늘 첫 줄. 그다음 AI가 고른 후보 번호 → 관전 포인트 줄.

    후보에 없는 것은 버리고, 시각은 후보의 것을 앞에 붙인다 (45자 안, 장 시작 줄은 묶음 글 그대로라 길이를 보지 않음)."""
    out, seen = [], set()
    for n, c in enumerate(candidates, 1):
        if c["must"]:
            out.append(f"{c['time']} — {c['what']}" if c["time"] else c["what"])
            seen.add(n)
            break
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        n = (_ints([p.get("pick")]) or [0])[0]
        text = _clean(p.get("text"))
        if not (1 <= n <= len(candidates)) or n in seen or not text:
            continue
        seen.add(n)
        t = candidates[n - 1]["time"]
        line = f"{t} — {text}" if t else text
        if len(line) <= 45:
            out.append(line)
    return out[:3]


def frame_out(raw, ctx):
    """공통 틀 답 → dict. 틀이 어긋나면 ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체가 아님")
    facts = [_clean(x) for x in raw.get("facts") or [] if _clean(x)]
    out = {
        "title_topic": _clean(raw.get("title_topic")).strip("'\"")[:40],
        "summary": _cut_bytes(_clean(raw.get("summary")), 150),
        "facts": facts[:3] if ctx.index_line else facts[:4],
        "points": pick_points(raw.get("points"), ctx.candidates),
        "tickers": raw.get("tickers") or [], "sources": _ints(raw.get("sources")),
    }
    return frame_ok(out, ctx)


def frame_ok(out, ctx):
    for k in ("title_topic", "summary"):
        if not out[k]:
            raise ValueError(f"{k} 비어 있음")
    if not out["facts"] and not ctx.index_line:
        raise ValueError("facts 비어 있음")
    return out


def view_out(raw, ctx):
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체가 아님")
    out = {
        "heading": _clean(raw.get("heading")).strip("'\"")[:40],
        "claim": _clean(raw.get("claim")),
        "evidence": [_clean(x) for x in raw.get("evidence") or [] if _clean(x)][:3],
        "risk": _clean(raw.get("risk")),
        "facts_used": _ints(raw.get("facts_used")),
        "tickers": raw.get("tickers") or [], "sources": _ints(raw.get("sources")),
    }
    return view_ok(out, ctx)


def view_body(v):
    return " ".join(x for x in [v["claim"], *v["evidence"], v["risk"]] if x)


def view_ok(out, ctx):
    if not out["heading"]:
        raise ValueError("heading 비어 있음")
    if not out["claim"]:
        raise ValueError("claim 비어 있음")
    if not out["evidence"]:
        raise ValueError("evidence 비어 있음")
    n = len(view_body(out))
    if not 80 <= n <= 1200:
        raise ValueError(f"본문 길이 {n}자")
    return out


# 2-3절 3 · 5 + Q14: 독자에게 내보내지 않는 말 (블로그 발행기도 같은 검사를 한다)
FORBIDDEN = re.compile(r"매수|매도|과매수|과매도|추천|목표 진입|D\+\d+|edition|stance|\bA/B\b|유보|테스트 중|"
                       r"강세론|약세론|칼럼|bullish|bearish|"
                       r"투자\s*기회|매력적|담을\s*만|사\s*둘\s*만|놓치면\s*안|유망", re.I)
# Q13: 인물 · 회사의 비위 · 사고 주장. 제목에는 금지, 본문은 채널 2곳 이상 또는 묶음 news에 있을 때만
ALLEGATION = re.compile(r"스캔들|의혹|비리|비위|수사|사임|소송|분식|횡령|배임|기소|사기")
# 말한 사람 없는 판단으로 끝나는 문장 (11-16 시험: '견고하다는 평가다' · '~로 전망된다')
JUDGMENT_END = re.compile(r"(전망된다|전망이다|보인다|예상된다|평가다|평가된다|기대된다|관측된다|분석된다|풀이된다|"
                          r"여겨진다|판단된다|전망이 나온다)[.。]?$")
SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+")


def sentences(text):
    return [s for s in SENT_SPLIT.split(text or "") if s.strip()]


def bad_number(text, have):
    """문장의 숫자 중 재료(방송 요약 · 묶음)에 없는 것. 10 이하 정수(개수 · 순서)는 보지 않는다."""
    import summarize
    for kind, v in summarize._vals(text, hedge_check.KO_NUM, hedge_check.KO_UNIT, skip_dates=True):
        if kind == "n" and v <= 10 and v == int(v):
            continue
        if not any(k == kind and abs(v - s) <= max(abs(s), 1e-9) * 0.02 for k, s in have):
            return f"{v:g}{'%' if kind == '%' else ''}"
    return None


def sentence_problems(s, ctx, title):
    out = []
    m = FORBIDDEN.search(s)
    if m:
        out.append(f"쓰면 안 되는 말 '{m.group()}'")
    m = ALLEGATION.search(s)
    if m:
        w = m.group()
        if title:
            out.append(f"제목에 '{w}' 같은 말을 넣지 않는다")
        else:
            n = sum(1 for t in ctx.channel_text.values() if w in t)
            if n < 2 and w not in ctx.news_text:
                out.append(f"'{w}' 주장은 서로 다른 채널 2곳 이상이 다룬 것만 쓴다 (재료에서 {n}곳)")
    if not title and JUDGMENT_END.search(s.strip()):
        out.append("말한 사람 없는 판단으로 끝남")
    x = bad_number(s, ctx.numbers)
    if x:
        out.append(f"재료에 없는 숫자 {x}")
    return out


def units(kind, out):
    """검사할 칸: [(칸, 번호 또는 None, 글, 제목인가)]"""
    if kind == "frame":
        return ([("title_topic", None, out["title_topic"], True), ("summary", None, out["summary"], True)]
                + [("facts", i, f, False) for i, f in enumerate(out["facts"])])
    return ([("heading", None, out["heading"], True), ("claim", None, out["claim"], False)]
            + [("evidence", i, e, False) for i, e in enumerate(out["evidence"])] + [("risk", None, out["risk"], False)])


def find_problems(kind, out, ctx):
    """→ [(칸, 번호, 문장, 제목인가, [걸린 점])]"""
    found = []
    for key, i, text, title in units(kind, out):
        for s in [text] if title else sentences(text):
            p = sentence_problems(s, ctx, title)
            if p:
                found.append((key, i, s, title, p))
    return found


def drop_sentences(out, found):
    """걸린 문장만 뺀다 (고쳐 쓰게 해도 남은 것). 빈 줄이 된 사실 · 근거는 줄째 뺀다."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in out.items()}
    for key, i, s, _, _ in found:
        if i is None:
            out[key] = " ".join(x for x in sentences(out[key]) if x != s)
        else:
            out[key][i] = " ".join(x for x in sentences(out[key][i]) if x != s)
    for key in ("facts", "evidence"):
        if key in out:
            out[key] = [x for x in out[key] if x.strip()]
    return out


FIX_PROMPT = """아래 JSON은 시황 해설 원고입니다. 다음 문장에 걸린 점이 있습니다.
{problems}

고치는 법:
- 쓰면 안 되는 말: 같은 뜻의 사실 말로 바꾼다 ('외국인이 순매도했다' → '외국인이 주식을 팔았다', '매도 의견' → '낮춰 잡은 의견',
  '추천 종목' → '꼽은 종목', '매력적인 투자 기회라고 봤다' → '○○는 지금 수준을 낮다고 봤다'처럼 숫자 · 사람으로)
- 말한 사람 없는 판단: 원고 안에 그렇게 말한 사람 · 채널이 나와 있으면 '○○는 …라고 봤다'로 고치고, 없으면 그 문장을 뺀다
- 재료에 없는 숫자: 그 숫자를 빼고 쓴다
- 비위 · 사고 주장: 본문에서는 그 문장을 빼고, 제목에서는 그 말을 빼고 중립 구절로 쓴다
걸린 문장만 고치고 나머지 문장과 칸은 그대로 두고, 같은 틀의 JSON 하나로만 답합니다. '-다'체를 지킨다.

{draft}
"""


def ask(kind, prompt, ctx, log=print):
    """한 호출(공통 틀 · 한 시각). 걸린 문장은 그 문장만 고쳐 쓰게 한 번 더, 그래도 남으면 그 문장을 뺀다.

    호출 실패 · 틀 어긋남 · 제목에 걸린 점이 남으면 예비 모델로. 다 안 되면 Skip."""
    norm, ok = (frame_out, frame_ok) if kind == "frame" else (view_out, view_ok)
    last = None
    for model in MODELS:
        config.GEMINI_TEXT_MODEL, config.GEMINI_TEXT_FALLBACK = model, "off"
        try:
            raw = llm.generate_json(prompt, log=log, what=f"시황 해설 {kind} {model}")
            out = norm(raw, ctx)
            found = find_problems(kind, out, ctx)
            if found:
                log(f"  {model} {kind}: 걸린 문장 {len(found)}개 → 그 문장만 고쳐 쓰게 함")
                for _, _, s, _, p in found:
                    log(f"    - {' · '.join(p)}: {s}")
                probs = "\n".join(f'- "{s}" → {" · ".join(p)}' for _, _, s, _, p in found)
                raw = llm.generate_json(FIX_PROMPT.format(problems=probs, draft=json.dumps(raw, ensure_ascii=False)),
                                        log=log, what=f"시황 해설 고쳐 쓰기 {kind} {model}")
                out = norm(raw, ctx)
                found = find_problems(kind, out, ctx)
                if any(title for _, _, _, title, _ in found):
                    raise ValueError("제목에 걸린 점 남음: " + " · ".join(
                        x for _, _, _, title, p in found if title for x in p))
                if found:
                    for _, _, s, _, p in found:
                        log(f"    문장 뺌 ({' · '.join(p)}): {s}")
                    out = ok(drop_sentences(out, found), ctx)
            return out, f"gemini:{model}"
        except llm.LLMError as e:
            last = f"호출 실패 ({e})"
        except ValueError as e:
            last = f"틀 어긋남 ({e})"
        log(f"  {model} {kind}: {last}")
    raise Skip("금지 표현" if last and ("쓰면 안 되는 말" in last or "제목에" in last) else "호출 실패")


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


# ---------- 한 편 ----------

def compose(day, slot, items, briefing, log=print):
    """재료 → columns 문서 칸 (status 제외). 쓰지 않을 편이면 Skip.

    호출: 공통 틀(제목 · 사실 · 관전 포인트, 중립) 1번 → 근거가 넉넉한 시각마다 1번. 세 호출 모두 같은 중심 사건을 받는다 (Q11)."""
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
    ctx = Ctx(day, slot, items, briefing)
    log(f"장: {ctx.label} · 중심 사건: {ctx.center['text'] if ctx.center else '없음'}")
    base["center"] = {"category": ctx.center["category"], "label": ctx.center["label"],
                      "channels": ctx.center["channels"]} if ctx.center else None
    try:   # 한쪽만 실수로 나가지 않게 어느 호출이든 안 되면 그 편 전체 skip. 센 건수는 남긴다
        frame, m0 = ask("frame", frame_prompt(ctx), ctx, log)
        facts = ([ctx.index_line] if ctx.index_line else []) + frame["facts"]
        if not ctx.index_line and ctx.label not in facts[0]:
            facts[0] = f"{ctx.label}: {facts[0]}"   # Q15: 첫 줄은 어느 장 이야기인지로 시작
        views = []
        for d in dirs:
            v, m = ask("view", view_prompt(ctx, d, videos[d], facts), ctx, log)
            views.append((d, v, m))
    except Skip as e:
        raise Skip(e.args[0], base) from e
    # Q12: 사실 칸은 첫 줄(장 · 지수)과 어느 시각에서든 쓰인 줄만 남긴다
    used = {n for _, v, _ in views for n in v["facts_used"]}
    kept = [f for n, f in enumerate(facts, 1) if n == 1 or n in used]
    if len(kept) < len(facts):
        log(f"사실 칸: 두 시각 어디에도 안 쓰인 {len(facts) - len(kept)}줄 뺌")
    body = [{"dir": d, "heading": v["heading"], "body": view_body(v)} for d, v, _ in views]
    text = " ".join([frame["title_topic"], frame["summary"], *kept, *frame["points"],
                     *(x for b in body for x in (b["heading"], b["body"]))])
    tickers = [t for t in dict.fromkeys(str(x).strip().upper() for r in [frame] + [v for _, v, _ in views]
                                        for x in r["tickers"])
               if t in ctx.tickers and mentioned(t, ctx.tickers[t], text)]
    sources = []
    for r in [frame] + [v for _, v, _ in views]:
        for n in r["sources"]:
            if 1 <= n <= len(items) and items[n - 1]["id"] not in sources:
                sources.append(items[n - 1]["id"])
    lack = [d for d in ("bull", "bear") if d not in dirs]
    dt = datetime.strptime(day, "%Y-%m-%d")
    return {
        **base,
        "date": day, "slot": slot, "reason": "",
        "title": f"{dt.month}.{dt.day:02d} 시황 해설 — {frame['title_topic']}",
        "summary": frame["summary"],
        "facts": kept,
        "views": body,
        "missing": {"dir": lack[0], "line": missing_line(lack[0], counts[lack[0]], pool)} if lack else None,
        "pointsTitle": points_title(day, slot),
        "points": frame["points"],
        "tickers": tickers,
        "sources": sources,
        "model": " · ".join(dict.fromkeys([m0] + [m for _, _, m in views])),
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
           "pointsTitle": points_title(day, slot), "model": "", "center": None}
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

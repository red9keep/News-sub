"""알파스코프 시황 해설 (11-16, News-doc 요청-알파스코프-시황-해설.md).

하루 두 편. 그 편 시간대에 받은 방송 요약(summaries) + 블로그 묶음 파일(briefing)을 재료로
'이어 쓰는 시황 기사'(17 ~ 22절, format "story") 한 편을 쓰고, Firestore columns/{날짜}-{am|pm}에 칸으로 나눠 저장한다.
블로그는 status가 ready인 것만 발행한다. 옛 틀(facts · views, 오름 · 내림 시각 따로)은 11-19에 버림 (18절 Q19).

    am  아침 편: 미국 마감 해설 + 오늘 한국장 관전 포인트. 재료 = 전날 20:40 ~ 당일 08:30, 마감 08:30
    pm  저녁 편: 한국 마감 해설 + 오늘 밤 미국장 관전 포인트. 재료 = 당일 08:30 ~ 20:40, 마감 20:40

무엇이 중요한지(변수 후보 순위 · 글의 무게)는 AI가 아니라 여기서 먼저 정한다 (driver_candidates · day_shape).

    python worker/commentary.py --slot am                 # 예약 실행 (이미 있으면 그냥 끝냄)
    python worker/commentary.py --slot pm --date 2026-11-16 --dry   # 지난 편 시험, 저장 안 함 (--story도 같음)
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
import private

BRIEFING_URL = "https://raw.githubusercontent.com/red6keep-droid/alphascope/gh-pages/briefing/{key}.json"

# 편별 재료 시간대 · 마감 (KST). start는 (며칠 전, 시, 분)
SLOTS = {
    "am": {"start": (-1, 20, 40), "end": (0, 8, 30), "market": "US", "next": "KR"},
    "pm": {"start": (0, 8, 30), "end": (0, 20, 40), "market": "KR", "next": "US"},
}
MAX_MATERIAL_CHARS = 60_000


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
    if b.get("date") != day or b.get("slot") != slot:   # 14절 Q18: ahead 묶음은 generated가 이틀 전이라 date만 본다
        log(f"묶음 파일이 그날 것이 아님 (date {b.get('date')}, generated {b.get('generated')}) → 방송만으로")
        return None
    return b


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


def _move(v):
    """등락을 부호 없이 '1.82% 하락' · '0.40% 상승' · '보합' (11-19 블로그: 발행기가 방향 낱말을 ▲▼로 바꾼다)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{abs(v):.2f}% {'상승' if v > 0 else '하락'}" if round(v, 2) else "보합"


# 본문의 부호 붙은 등락 '−1.82%' · '+0.05%p' → '1.82% 하락' · '0.05%p 상승'. 바로 뒤에 방향 말이 있으면 부호만 뗀다
SIGNED = re.compile(r"(?<![\w\d.,])([+\-−–])\s?(\d[\d,]*(?:\.\d+)?)(%p|%)(\s*)(?=(하락|상승|내림|오름|내렸|올랐|내린|오른|떨어|올라|빠졌|빠진|급락|급등|반등|약세|강세|밀렸|밀린)?)")


def unsign(t):
    def f(m):
        sign, num, unit, sp, after = m.groups()
        if after:
            return f"{num}{unit}{sp}"
        return f"{num}{unit} {'상승' if sign == '+' else '하락'}{sp}"
    t = SIGNED.sub(f, t or "")
    # '하락' · '상승'은 받침이 있다: '1.82% 하락로' → '하락으로'
    return re.sub(r"(%p? (?:하락|상승))(로|가|는|를|와)(?![가-힣])",
                  lambda m: m.group(1) + {"로": "으로", "가": "이", "는": "은", "를": "을", "와": "과"}[m.group(2)], t)


def index_line(label, briefing):
    """묶음 close.indices가 있으면 지수 등락 한 줄 (Q15). 숫자는 묶음 그대로라 AI를 거치지 않는다."""
    xs = [f"{x.get('name')} {_move(x.get('chg_pct'))}" for x in ((briefing or {}).get("close") or {}).get("indices") or []
          if x.get("name") and _pct(x.get("chg_pct"))]
    return f"{label}: {' · '.join(xs)}" if xs else ""


def add_center(by, c, n, ch, it):
    b = by.setdefault(c, {"channels": set(), "moved": set(), "issues": 0, "videos": set(), "topics": Counter()})
    b["channels"].add(ch)
    if it.get("stance") in ("bullish", "bearish"):
        b["moved"].add(ch)
    b["issues"] += 1
    b["videos"].add(n)
    t = str(it.get("topic") or "").strip()
    if t:
        b["topics"][t] += 1


def center_event(items, briefing, market, log=None):
    """그날의 중심 사건 (Q11): 해설 시장(또는 나라 없음) 이슈 중 가장 많은 채널이 방향(오름 · 내림)을 붙여 다룬 큰 분류.

    같으면 그 분류를 다룬 채널 수 → 이슈 수. 방향 없는 이슈만 많은 분류(11-16 시험: 빅테크 CEO 스파이 위협)가
    그날 시장을 움직인 사건(반도체 급락)보다 앞서지 않게 방향 있는 채널을 먼저 본다.
    topic은 자유 문구라 잘 겹치지 않아 큰 분류로 고르고, 그 안에서 많이 나온 topic을 같이 준다."""
    from summarize import CATEGORIES
    by = {}
    for n, s in enumerate(items, 1):   # 종목 범위도 넣는다 (11-16: 반도체 급락이 엔비디아 종목 이슈로만 적힘)
        ch = s.get("channelName") or s.get("channelId") or "?"
        for it in s.get("issues") or []:
            c, country = it.get("category"), (it.get("country") or "").upper()
            if not c or c == "OTHER" or (country and country != market):
                continue
            add_center(by, c, n, ch, it)
    if not by:
        return None
    ranked = sorted(by.items(), key=lambda kv: (len(kv[1]["moved"]), len(kv[1]["channels"]), kv[1]["issues"]), reverse=True)
    if log:
        for c, b in ranked[:5]:
            # log의 둘째 인자부터는 본문(문장 · 주제 · 이름)이다. 공개 로그에는 글자 수만 찍힌다 (private.Report.log, 11-18)
            log(f"  중심 사건 후보 {c}: 방향 채널 {len(b['moved'])} · 채널 {len(b['channels'])} · 이슈 {b['issues']} —",
                ", ".join(t for t, _ in b["topics"].most_common(4)))
    cat, b = ranked[0]
    label = CATEGORIES.get(cat, cat).split(" (")[0]
    move = ""
    close = (briefing or {}).get("close") or {}
    for k, name in (("sectors", "업종"), ("movers", "종목")):
        xs = [x for x in close.get(k) or [] if _pct(x.get("chg_pct"))]
        if xs:
            x = max(xs, key=lambda x: abs(float(x["chg_pct"])))
            move += f" 묶음에서 가장 크게 움직인 {name}: {x.get('name')} {_pct(x['chg_pct'])}."
    return {
        "category": cat, "label": label, "channels": len(b["channels"]), "videos": sorted(b["videos"]),
        "text": (f"{label} — 채널 {len(b['channels'])}곳 · 영상 {', '.join(map(str, sorted(b['videos'])))}에서 다룸. "
                 f"주로 나온 주제: {', '.join(t for t, _ in b['topics'].most_common(3)) or '(없음)'}.{move}"),
    }


WEEKDAY = "월화수목금토일"


def rel_day(us_day, day):
    """미국 날짜 us_day를 한국 독자(그 편 날짜 day에 읽음) 말로: 오늘 밤 · 내일 밤 · 11월 19일(목) 밤."""
    try:
        u = datetime.strptime(us_day, "%Y-%m-%d").date()
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return ""
    n = (u - d).days
    return {0: "오늘 밤", 1: "내일 밤"}.get(n, f"{u.month}월 {u.day}일({WEEKDAY[u.weekday()]}) 밤")


def date_words(us_day, day, after_close=False):
    """그 일정에 써도 되는 날짜 말 (14절 1번: 본문 '다음 주 엔비디아 실적' ↔ 실제는 그날 밤)."""
    u = datetime.strptime(us_day, "%Y-%m-%d").date()
    d = datetime.strptime(day, "%Y-%m-%d").date()
    n = (u - d).days
    ok = set()
    if n == 0:
        ok |= {"오늘", "오늘 밤"} | ({"내일"} if after_close else set())
    elif n == 1:
        ok |= {"내일", "내일 밤"} | ({"모레"} if after_close else set())
    elif n == 2:
        ok |= {"모레"}
    wk = u.isocalendar()[1] - d.isocalendar()[1] + 52 * (u.isocalendar()[0] - d.isocalendar()[0])
    ok |= {0: {"이번 주"}, 1: {"다음 주"}}.get(wk, set())
    return ok


DATE_WORDS = ("다음 주", "이번 주", "오늘", "내일", "모레", "다음 달")
KIND_NAME = {"CLAIMS": ("신규 실업수당 청구", ["실업수당"]), "GDP": ("GDP", ["GDP"]), "PCE": ("PCE 물가", ["PCE"]),
             "OPEX": ("옵션 만기", ["옵션 만기"]), "CPI": ("소비자물가(CPI)", ["CPI", "소비자물가"]),
             "PPI": ("생산자물가(PPI)", ["PPI", "생산자물가"]), "FOMC": ("FOMC", ["FOMC", "연준 회의", "금리 결정"]),
             "JOBS": ("고용 보고서", ["고용 보고서", "비농업"]), "NFP": ("고용 보고서", ["고용 보고서", "비농업"]),
             "RETAIL": ("소매판매", ["소매판매"])}


def schedule(day, briefing, names):
    """묶음 calendar → [{name, keys, label, ok}] (파이썬이 날짜 말을 정해 재료로 준다 · 본문 날짜 말을 대조한다)."""
    out = []
    for x in ((briefing or {}).get("calendar") or {}).get("items") or []:
        k, us_day = str(x.get("kind") or "").upper(), x.get("day")
        if not us_day:
            continue
        if k == "EARNINGS":
            sym = str(x.get("symbol") or "").upper()
            nm = sorted(names.get(sym) or [], key=len)
            name, keys = f"{nm[0] if nm else sym} 실적", [sym, *nm]
        elif k in KIND_NAME:
            name, keys = KIND_NAME[k]
        else:
            continue
        try:
            ok = date_words(us_day, day, bool(x.get("after_close")))
        except ValueError:
            continue
        u = datetime.strptime(us_day, "%Y-%m-%d")
        when = f"{name}: 미국 {u.month}월 {u.day}일({WEEKDAY[u.weekday()]}){' 장 마감 뒤' if x.get('after_close') else ''}"
        done = (u.date() - datetime.strptime(day, "%Y-%m-%d").date()).days < 0   # 11-18 시험: 지난밤 장 마감 뒤 엔비디아 실적
        if done:
            ok |= {"오늘", "오늘 새벽", "지난밤"}
            label = (f"{when} — 이미 발표됨 (한국 시각 이 글 전). 지수 마감 뒤라 지수 흐름에는 반영되지 않았다"
                     f", 써도 되는 날짜 말: {' · '.join(sorted(ok))}")
        else:
            label = when + f" — 한국 독자에게는 '{rel_day(us_day, day)}'" + (f", 써도 되는 날짜 말: {' · '.join(sorted(ok))}" if ok else "")
        out.append({"name": name, "keys": [k for k in keys if k], "label": label, "ok": ok, "done": done, "kind": k})
    return out


def point_candidates(items, briefing, next_market, limit_topics=4, day=None):
    """관전 포인트 후보: 묶음 next_session 일정 + 방송에서 많이 나온 주제 (2-2절). AI는 이 중에서만 고른다.

    묶음의 장 시작 줄('… 첫 반응', Q16)은 must → 늘 첫 관전 포인트. 방송 주제는 다음 장 나라(또는 나라 없음) 이슈만."""
    out = []   # [{"text", "time", "must"}]
    for x in ((briefing or {}).get("next_session") or {}).get("items") or []:
        t, w = str(x.get("time") or "").strip(), str(x.get("what") or "").strip()
        kind = x.get("kind")
        must = kind == "open" if kind else "첫 반응" in w   # 14절 Q17: 장 시작 줄은 kind로 알아본다
        if kind == "event" and x.get("us_day") and day:   # 14절 1번: '장 마감 뒤' → '오늘 밤 미국 장 마감 뒤'
            r = rel_day(x["us_day"], day)
            t = f"{r} 미국 {t}".strip() if r else t
        if w:
            out.append({"text": f"{t} {w}" if t else w, "time": t, "what": w, "must": must})
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


def material_text(items, only=None):
    """방송 요약을 번호 붙인 글로. 자막 원문 인용(picks.quote)은 넣지 않는다 (2-3절 4). only = 넣을 영상 번호 (번호는 그대로)"""
    blocks = []
    for n, s in enumerate(items, 1):
        if only is not None and n not in only:
            continue
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
    def __init__(self, day, slot, items, briefing, log=None):
        import summarize
        self.day, self.slot, self.items, self.briefing, self.log = day, slot, items, briefing, log
        self.market, self.next = SLOTS[slot]["market"], SLOTS[slot]["next"]
        self.material = material_text(items)
        self.briefing_json = (json.dumps({k: v for k, v in briefing.items() if k != "links"}, ensure_ascii=False)
                              if briefing else "")
        self.label = session_label(day, slot, briefing)
        self.index_line = index_line(session_label(day, slot, briefing, plain=True), briefing)
        self.center = center_event(items, briefing, self.market, log)
        self.candidates = point_candidates(items, briefing, self.next, day=day)
        self.tickers = known_tickers(items, briefing)
        self.schedule = schedule(day, briefing, self.tickers)
        m = re.search(r"주로 나온 주제: ([^.]*)", self.center["text"]) if self.center else None
        self.center_keys = keys_of((m.group(1) if m else "") + " " + (self.center["label"] if self.center else ""))
        for t in unlisted_tickers(items, briefing):   # 11-17 시험: 상장 전 앤트로픽 'ANTH'
            if t in self.tickers:
                del self.tickers[t]
                (log or print)(f"티커 뺌 (상장 전 · 비상장으로 보임): {t}")
        # 날짜 대조: 재료 · 묶음에 나온 'N일'과 재료 기간 앞뒤 날짜
        src = self.material + "\n" + self.briefing_json
        self.days = {int(x) for x in re.findall(r"(?<!\d)(\d{1,2})일", src)}
        self.days |= {int(x) for x in re.findall(r"\d{4}-\d{2}-(\d{2})", src)}
        start, end = window(day, slot)
        self.days |= {(start + timedelta(days=i)).day for i in range(-3, (end - start).days + 2)}
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


def _common(ctx):
    start, end = window(ctx.day, ctx.slot)
    what = ("미국 마감 해설 + 오늘 한국장 관전 포인트 (한국 개장 전에 읽힘)" if ctx.slot == "am"
            else "한국 마감 해설 + 오늘 밤 미국장 관전 포인트 (미국 개장 전에 읽힘)")
    if ctx.briefing:
        numbers_rule = " 지수 · 업종 · 종목 등락 숫자는 블로그 데이터를 먼저 쓴다."
        if ctx.briefing.get("close", {}).get("figures"):   # 18절 Q20: pm 외국인 순매수 · 환율 · 금리
            numbers_rule += (" 외국인 순매수 · 환율 · 금리는 블로그 데이터 close.figures 값을 쓴다 (v가 음수면 순매도)."
                             " 그 값의 day가 장 날짜(trade_date)와 다르면 그 날짜를 밝혀 쓴다 ('11월 16일 외국인은 …').")
    else:
        numbers_rule = " 오늘은 블로그 데이터가 없다. 숫자는 방송에 나온 것만 쓴다."
    return dict(when=f"{ctx.day} {'아침' if ctx.slot == 'am' else '저녁'} 편", what=what,
                center=ctx.center["text"] if ctx.center else "(정하지 못함 — 가장 많이 다룬 시장 흐름을 중심으로)",
                numbers_rule=numbers_rule,
                briefing=ctx.briefing_json or "(없음)",
                schedule="\n".join(f"- {e['label']}" for e in ctx.schedule) or "(없음)",
                n=len(ctx.items), span=f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST 받음", material=ctx.material)


# ---------- 결과 검사 ----------

def _clean(x):
    return " ".join(str(x or "").split()).strip()


# 원고에 남은 재료 번호 '[영상 39]' · '(영상 25, 26)' (11-17 시험: 근거 끝에 붙고 마침표가 빠짐)
VIDEO_REF = re.compile(r"\s*(\[\s*(영상\s*)?[\d,\s·~-]+\]|[(（]\s*영상\s*[\d,\s·~-]+[)）])")  # '[39]'도 (11-17 두 번째 시험)


def _sent(x):
    """본문에 들어갈 한 문장: 재료 번호를 빼고, 끝에 마침표가 없으면 붙인다."""
    x = _clean(VIDEO_REF.sub("", str(x or "")))
    return x + "." if x and x[-1] not in ".!?。…" else x


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


# 2-3절 3 · 5 + Q14: 독자에게 내보내지 않는 말 (블로그 발행기도 같은 검사를 한다)
FORBIDDEN = re.compile(r"매수|매도|과매수|과매도|추천|목표 진입|D\+\d+|edition|stance|\bA/B\b|유보|테스트 중|"
                       r"강세론|약세론|칼럼|bullish|bearish|"
                       r"투자\s*기회|매력적|담을\s*만|사\s*둘\s*만|놓치면\s*안|유망", re.I)
# Q13: 인물 · 회사의 비위 · 사고 주장. 제목에는 금지, 본문은 채널 2곳 이상 또는 묶음 news에 있을 때만
ALLEGATION = re.compile(r"스캔들|의혹|비리|비위|수사|사임|소송|분식|횡령|배임|(?<!경)기소|사기|소환|조사에 착수|조사하")
ATTRIBUTED = re.compile(r"(전했다|보도했다|밝혔다|말했다|지적했다|주장했다|발표했다|봤다|"
                        r"전했습니다|보도했습니다|밝혔습니다|말했습니다|지적했습니다|주장했습니다|발표했습니다|봤습니다)[.。]?$")
# 말한 사람 없는 판단으로 끝나는 문장 (11-16 시험: '견고하다는 평가다' · '~로 전망된다')
JUDGMENT_END = re.compile(r"(전망된다|전망이다|보인다|예상된다|평가다|평가된다|기대된다|관측된다|분석된다|풀이된다|"
                          r"여겨진다|판단된다|전망이 나온다|전해졌다|알려졌다|나타났다고 한다|"
                          r"전망됩니다|전망입니다|보입니다|예상됩니다|평가입니다|평가됩니다|기대됩니다|관측됩니다|분석됩니다|"
                          r"풀이됩니다|여겨집니다|판단됩니다|전망이 나옵니다|전해졌습니다|알려졌습니다|"
                          # 19절 Q24 (11-17 시험: '…옮겨갔음을 보여준다' · '경계감이 형성되어 있다' · '지속되는 상태다')
                          r"보여준다|보여줍니다|형성되어 있다|형성되어 있습니다|형성돼 있다|형성돼 있습니다|상태다|상태입니다)[.。]?$")
# 말한 사람이 흐린 문장 (11-16 저녁 시험: '전문가들은 … 평가했다')
# 11-17 시험: '외신이 전했다' · '매경TV 뉴스에서 증권가는' · '은행권은'
VAGUE = re.compile(r"(^|\s)(외신|증권가|은행권|월가|시장 관계자|전문가들|전문가들은|시장에서는|시장 참여자들|업계에서는|일각에서는|투자자들은|분석가들은|애널리스트들은)")
SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+")
# 이름 반쪽 (11-17 시험: '블룸버그에서 마크는'). 재료에는 'X Y'로만 나오는데 문장에는 X만
HALF_NAME = re.compile(r"에서\s+([가-힣]{2,4})(?:은|는)\s")
MEDIA = {"블룸버그", "로이터", "폭스", "야후", "월스트리트저널", "파이낸셜타임스", "연합뉴스", "한국경제", "매일경제"}
TITLES = {"이사", "본부장", "대표", "위원", "연구원", "애널리스트", "센터장", "팀장", "장관", "의원", "회장", "부장", "전략가",
          "교수", "총재", "의장", "사장", "부사장", "상무", "전무", "실장", "기자", "앵커", "위원장", "매니저", "수석", "고문"}
# 틀리는 조건이 본문과 이어지는지 볼 때 세지 않는 말
GENERIC = {"시장", "시각", "힘을", "잃는다", "실적", "발표", "확인", "확인되어", "주가", "증시", "기술주", "하락", "상승", "전반",
           "경우", "이후", "가능성", "우려", "전망", "기대", "예상", "투자자", "투자", "이번", "다음", "현재", "추가", "크게", "계속"}
# 이름 뒤에 붙어도 이름의 일부가 아닌 말 (11-17: 재료 '조슈아 발언' → '조슈아 발언은'으로 고쳐 씀)
NOT_NAME = {"발언", "발언자", "의견", "분석", "전망", "주장", "설명", "인터뷰", "출연", "측", "씨", "등", "역시", "또한", "이번",
            "외국인", "기관", "개인", "지수", "주가", "시장", "증시",
            "기업공개", "상장", "실적", "발표", "규제", "업종", "종목", "금리", "잔고", "통계"}
SOFT = "[고쳐 보기만] "   # 고쳐 쓰게 하되, 그래도 남아도 문장은 빼지 않는다


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


PARTICLE = re.compile(r"(에서|으로|에게|까지|부터|이라는|라는|이다|의|은|는|이|가|을|를|과|와|에|로|도|만)$")
COMMON = {"관련", "대한", "통해", "따라", "있다", "했다", "전했다", "보도", "주장", "가능성", "우려", "논란", "대상", "최근"}


def backing(sent, word, texts):
    """비위 주장 sent를 뒷받침하는 글 수: word와 sent의 고유 낱말 2개 이상이 한 문장에 같이 나온 글.

    낱말만 세면 '의혹'이 다른 일로 두 채널에 나와도 통과한다 (11-16 시험: 엔비디아 칩 유출 의혹)."""
    keys = set()
    for t in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9&]+", sent):
        t = PARTICLE.sub("", t)
        if len(t) >= 2 and t not in COMMON and not ALLEGATION.fullmatch(t):
            keys.add(t)
    n = 0
    for text in texts:
        for x in re.split(r"(?<=[.!?。])\s+|\\n|\n|\", \"", text):
            if word in x and sum(1 for k in keys if k in x) >= 2:
                n += 1
                break
    return n


def half_name(s, ctx):
    """'○○에서 마크는'의 '마크'가 재료에는 '마크 뉴턴'처럼 더 긴 이름으로만 나오면 → (마크, 마크 뉴턴)."""
    src = ctx.material
    for m in HALF_NAME.finditer(s):
        x = m.group(1)
        before = s[:m.start()].split()[-1:] or [""]   # '블룸버그에서'의 '블룸버그' — 채널 · 매체일 때만 (11-17: '속에서 주가가')
        w = before[0]
        if not (re.search(r"[A-Za-z]", w) or w in MEDIA or any(w and (w in ch or ch.split()[0] in w) for ch in ctx.channel_text)):
            continue
        fulls = {f"{x} {y}" for y in re.findall(rf"(?<![가-힣]){re.escape(x)} ([가-힣]{{2,5}})", src)
                 if not any(PARTICLE.sub("", y).endswith(t) for t in TITLES)   # '베센트 재무장관'은 이름 아님
                 and PARTICLE.sub("", y) not in NOT_NAME and y not in NOT_NAME}
        if fulls and not any(f in s for f in fulls):
            return x, sorted(fulls)[0]
    return None


def bad_day(s, ctx):
    """재료 · 묶음 · 재료 기간에 없는 'N일' (11-17 시험: 관전 포인트 '26일 금통위'를 대조할 길이 없었음)."""
    for x in re.findall(r"(?<!\d)(\d{1,2})일", s):
        if int(x) not in ctx.days:
            return f"{x}일"
    return None


def bad_when(s, ctx):
    """일정 표에 있는 일정을 다른 날짜 말로 쓴 것 (14절 1번)."""
    hit = {}   # 이름 → 그 이름의 일정들 (11-18: 실업수당 청구가 11-19 · 11-25 두 번 → 한쪽만 보고 다른 날짜 말을 막았음)
    for e in ctx.schedule:
        if any(k in s for k in e["keys"]):
            hit.setdefault(e["name"], []).append(e)
    for name, es in hit.items():
        ok = set().union(*(e["ok"] for e in es))
        for w in DATE_WORDS:
            if w in s and w not in ok:
                return f"'{name}' 날짜 말 '{w}'가 일정과 다름 ({' / '.join(e['label'] for e in es)})"
    return None


def keys_of(text):
    out = set()
    for t in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9&]+", text):
        t = PARTICLE.sub("", t)
        if len(t) >= 2 and t not in COMMON and t not in GENERIC:
            out.add(t)
    return out


def sentence_problems(s, ctx, title, forbidden=None):
    out = []
    m = (forbidden or FORBIDDEN).search(s)
    if m:
        out.append(f"쓰면 안 되는 말 '{m.group()}'")
    m = ALLEGATION.search(s)
    if m:
        w = m.group()
        if title:
            out.append(f"제목에 '{w}' 같은 말을 넣지 않는다")
        else:
            n = backing(s, w, ctx.channel_text.values())
            if n < 2 and backing(s, w, [ctx.news_text]) == 0:
                out.append(f"'{w}' 주장은 서로 다른 채널 2곳 이상이 다룬 것만 쓴다 (재료에서 {n}곳)")
            elif not ATTRIBUTED.search(s.strip()):
                out.append(f"'{w}' 주장은 '~라고 ○○(채널 · 매체)가 전했다'로 쓴다")
    if not title and JUDGMENT_END.search(s.strip()):
        out.append("말한 사람 없는 판단으로 끝남")
    if not title and VAGUE.search(s):
        out.append("말한 사람이 흐림 ('전문가들' 대신 이름 · 채널)")
    x = bad_number(s, ctx.numbers)
    if x:
        out.append(f"재료에 없는 숫자 {x}")
    x = bad_day(s, ctx)
    if x:
        out.append(f"재료에 없는 날짜 {x}")
    x = bad_when(s, ctx)
    if x:
        out.append(x)
    if not title:
        h = half_name(s, ctx)
        if h:
            out.append(f"이름이 반쪽 ('{h[0]}' → 재료에 나온 '{h[1]}'처럼 전체 이름으로)")
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


UNLISTED_MARK = re.compile(r"^(.+?)\s*\((비상장|상장\s*전|상장\s*예정)\)$")


def unlisted_tickers(items, briefing):
    """재료에 '(비상장)' 표시가 붙은 티커 → 표시를 뗀 티커 (그 자체 · 뗀 것 둘 다).

    요약 지시문은 비상장사를 '회사명(비상장)'으로 적게 한다. 11-17 시험: 재료 'ANTH(비상장)'를 AI가 'ANTH'로 옮겨 씀.
    (IPO 이야기에만 나온 티커를 빼는 방식은 MSFT · ARKK처럼 상장 종목까지 빼서 버림)"""
    movers = {str(m.get("ticker") or "").upper() for m in ((briefing or {}).get("close") or {}).get("movers") or []}
    out = set()
    for s in items:
        ts = list(s.get("tickers") or [])
        for it in s.get("issues") or []:
            ts += it.get("tickers") or []
        for p in s.get("picks") or []:
            ts.append(p.get("ticker"))
            m = UNLISTED_MARK.match(" ".join(str(p.get("name") or "").split()))
            if m and p.get("ticker"):
                ts.append(f"{p.get('ticker')}(비상장)")
        for t in ts:
            m = UNLISTED_MARK.match(str(t or "").strip().upper())
            if m:
                out |= {m.group(0), m.group(1).strip()}
    return out - movers


def mentioned(t, names, text):
    """티커나 그 종목 이름이 글에 실제로 나왔는가 (AI가 본문에 없는 종목까지 tickers에 넣는다, 11-16 시험)."""
    if re.search(rf"(?<![A-Z]){re.escape(t)}(?![A-Z])", text):
        return True
    return any(n and n in text for n in names)


# ---------- 한 편 ----------

# 14절 2번: 한 글 안 같은 지표 두 값 (11-16 저녁: 신용잔고 '20조 원' · '25조 7,729억 원')
INDICATORS = {"신용잔고": ("신용잔고", "신용 잔고", "신용거래융자", "신용거래 융자", "신용융자", "신용 융자"), "마이너스 통장": ("마이너스 통장", "마이너스통장"),
              "예탁금": ("예탁금",), "PBR": ("PBR",), "PER": ("PER",), "VIX": ("VIX",), "환율": ("환율",),
              "국채 금리": ("국채 금리", "국채 10년", "10년물"), "유가": ("유가", "WTI", "브렌트")}
AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(조)(?:\s*(\d[\d,]*)\s*억)?|(\d[\d,]*(?:\.\d+)?)\s*(억|%|배|포인트|선|달러|원)")
SCALE = {"조": 1e12, "억": 1e8}


def indicator_values(text):
    """글 → [(지표, 단위 종류, 값, 원문)] — 숫자 바로 앞 25자 안에 가장 가까이 나온 지표."""
    out = []
    for m in AMOUNT.finditer(text):
        if m.group(2):
            v = float(m.group(1).replace(",", "")) * 1e12 + float((m.group(3) or "0").replace(",", "")) * 1e8
            kind = "amt"
        else:
            v, u = float(m.group(4).replace(",", "")), m.group(5)
            kind = "amt" if u in ("억", "달러", "원") else u
            v *= SCALE.get(u, 1)
        before = text[max(0, m.start() - 25):m.start()]
        best = None
        for name, words in INDICATORS.items():
            for w in words:
                i = before.rfind(w)
                if i >= 0 and (best is None or i > best[1]):
                    best = (name, i)
        if best:
            out.append((best[0], kind, v, m.group(0)))
    return out


def resolve_conflicts(facts, views, ctx, log, protect=True):
    """같은 지표가 다른 값으로 나오면 재료(묶음 · 채널)에서 더 많이 받친 값만 남기고, 다른 값이 든 문장을 뺀다.

    같으면 사실 칸에 있는 값을 남긴다. 장 줄(사실 첫 줄) · 주장(본문 첫 문장) · 틀리는 조건(끝 문장)은 빼지 않고 로그만."""
    places = []   # (지표, 종류, 값, 원문, ('facts', i) | ('view', j, k))
    for i, f in enumerate(facts):
        places += [(*x, ("facts", i)) for x in indicator_values(f)]
    for j, v in enumerate(views):
        for k, sent in enumerate(sentences(v["body"])):
            places += [(*x, ("view", j, k)) for x in indicator_values(sent)]
    src = [ctx.briefing_json] * 3 + list(ctx.channel_text.values())   # 묶음은 세 채널 몫
    drop = set()
    for name in {p[0] for p in places}:
        for kind in {p[1] for p in places if p[0] == name}:
            ps = [p for p in places if p[0] == name and p[1] == kind]
            vals = []
            for p in ps:
                if not any(abs(p[2] - v) <= abs(v) * 0.05 for v in vals):
                    vals.append(p[2])
            if len(vals) < 2:
                continue

            def support(v):
                return sum(1 for t in src if any(x[0] == name and x[1] == kind and abs(x[2] - v) <= abs(v) * 0.02
                                                 for x in indicator_values(t)))
            in_facts = {p[2] for p in ps if p[4][0] == "facts"}
            def detail(v):   # 같으면 더 자세한 숫자 ('25조 7,729억' > '20조'). 11-17: 사실 칸이 없는 이어 쓰는 글에서 어림값이 남음
                return max(len(re.sub(r"[^1-9]", "", p[3])) for p in ps if abs(p[2] - v) <= abs(v) * 0.05)
            keep = max(vals, key=lambda v: (support(v), any(abs(v - f) <= abs(v) * 0.05 for f in in_facts), detail(v)))
            for p in ps:
                if abs(p[2] - keep) > abs(keep) * 0.05:
                    where = p[4]
                    fixed = protect and ((where[0] == "facts" and where[1] == 0) or (
                        where[0] == "view" and where[2] in (0, len(sentences(views[where[1]]["body"])) - 1)))
                    log("같은 지표 두 값" + (" → 장 줄 · 주장 · 조건 문장이라 둠" if fixed else " → 그 문장 뺌") + ":",
                        f"{name} '{p[3]}' (남길 값 쪽 재료 {support(keep)}곳 · 이 값 {support(p[2])}곳)")
                    if not fixed:
                        drop.add(where)
    facts = [f for i, f in enumerate(facts) if ("facts", i) not in drop]
    for j, v in enumerate(views):
        ss = sentences(v["body"])
        v["body"] = " ".join(x for k, x in enumerate(ss) if ("view", j, k) not in drop)
    return facts, views


# 14절 7번: 같은 사람 두 표기 (만딥 싱 · 만디프 싱) → 처음 나온 표기로
NAME_PAIR = re.compile(r"(?<![가-힣])([가-힣]{1,5}) ([가-힣]{1,4}?)(?=(?:은|는|이|가|의)\s|\(|,)")


def unify_names(texts, log):
    from difflib import SequenceMatcher
    first, reps = {}, {}
    for t in texts:
        for a, b in NAME_PAIR.findall(t):
            if b in TITLES or a in TITLES or b in NOT_NAME or a in NOT_NAME or PARTICLE.search(a):
                continue   # '코스피가 외국인' · '코스피는 외국인'을 같은 사람 두 표기로 본 일 (11-17)
            for a0 in first.get(b, []):
                if a0 != a and a0[0] == a[0] and SequenceMatcher(None, a0, a).ratio() >= 0.4:
                    reps[f"{a} {b}"] = f"{a0} {b}"
                    break
            else:
                if a not in first.get(b, []):
                    first.setdefault(b, []).append(a)
    for x, y in reps.items():
        log("이름 표기 하나로:", f"'{x}' → '{y}'")
    return reps


# ---------- 이어 쓰는 글 (11-17 사용자: 칸 채우기 글은 맥락이 없다 → 흐름이 있는 시황으로) ----------
# 지금은 시험 모드(--story, 저장 안 함). 블로그와 새 칸 형식을 맞춘 뒤 compose를 이 틀로 바꾼다 (요청 문서 17절).
# 호출: 글 전체 1번 → 걸린 문장이 있으면 그 문장만 고쳐 쓰게 1번 → 그래도 남으면 그 문장을 문단에서 뺀다.

STORY_MODEL = "gemini-4-hydrogen"   # 11-18 사용자 결정: 3.6 flash보다 글이 낫고 503 기다림 없음 · 받아쓰기(3.6 flash)와 한도가 갈림
                                    # flash-lite로는 넘어가지 않는다 (11-17: 문단이 얇고, 고쳐 쓰다 출처를 지어냄)
STORY_WAIT_MIN = 30    # 호출이 안 되면(한도 · 혼잡) 이만큼까지 기다리며 다시 해 본다 (마감 안에서만, 21절). 그래도 안 되면 그 편 skip
STORY_PAUSE_SEC = 300  # 두 키가 다 안 됐을 때 다음 시도까지 쉬는 시간
STORY_REWRITES = 2     # 틀이 어긋나면(제목에 걸린 점 등) 처음부터 다시 쓰는 횟수
# 편당 요청 상한 (키마다, 재시도 · 고쳐 쓰기 · 다시 쓰기 · 기다린 뒤 다시 모두 포함). Hydrogen 무료 하루 20회 (11-18 AI Studio)
# → 두 편 12회 + 받아쓰기 예비(4-hydrogen) · 손 시험 몫 8회. 보통 편은 글 1 + 고쳐 쓰기 0 ~ 2 = 1 ~ 3회
STORY_BUDGET = 6
STORY_ATTEMPTS = 2     # 호출 하나에 보내는 요청 수 (503 · 깨진 JSON이면 한 번 더). 429는 기다리지 않고 바로 다음 키


class OutOfBudget(llm.LLMError):
    """모든 키가 이 편 요청 상한을 다 씀. 기다려도 소용없다."""


def story_keys():
    """[{name, key, used}] — 기본 키, 그다음 예비 키(GEMINI_API_KEY_BACKUP, 있으면). used = 이 편에서 보낸 요청 수."""
    if config.LLM_PROVIDER == "nvidia":
        return [{"name": "NVIDIA 키", "key": None, "used": 0}]
    keys = [{"name": "기본 키", "key": config.GEMINI_API_KEY, "used": 0}]
    if config.GEMINI_API_KEY_BACKUP and config.GEMINI_API_KEY_BACKUP != config.GEMINI_API_KEY:
        keys.append({"name": "예비 키", "key": config.GEMINI_API_KEY_BACKUP, "used": 0})
    return keys


def story_call(prompt, what, keys, log):
    """기본 키로 안 되면 예비 키로. 둘 다 안 되면 LLMError (둘 다 상한을 다 썼으면 OutOfBudget).
    된 키를 맨 앞으로 옮겨 다음 호출도 그 키부터 쓴다."""
    import transcribe
    last = None
    for i, k in enumerate(list(keys)):
        left = STORY_BUDGET - k["used"]
        if left <= 0:
            log(f"  {k['name']}: 이 편 요청 {STORY_BUDGET}번을 다 씀" + (" → 다음 키로" if i + 1 < len(keys) else ""))
            continue
        if k["key"] is not None:
            config.GEMINI_API_KEY, transcribe._client = k["key"], None
        before = llm.requests_made
        try:
            r = llm.generate_json(prompt, log=log, what=f"{what} ({k['name']})",
                                  attempts=min(STORY_ATTEMPTS, left), wait_429=False)
        except llm.LLMError as e:
            last = e
            log(f"  {k['name']}: 호출 실패 ({e})" + (" → 다음 키로" if i + 1 < len(keys) else ""))
            continue
        finally:
            k["used"] += llm.requests_made - before
        if i:
            keys.insert(0, keys.pop(i))
        return r
    if all(k["used"] >= STORY_BUDGET for k in keys):
        raise OutOfBudget(f"모든 키가 이 편 요청 상한 {STORY_BUDGET}번을 다 씀")
    raise last

STORY_RULES = """글쓰기 규칙:
1. 재료에 없는 숫자 · 사건 · 종목은 쓰지 않는다.{numbers_rule}
   같은 지표(신용잔고 · 환율 등)의 값이 재료마다 다르면 더 많은 재료가 받치는 값 하나만 쓴다.
   지수가 어느 선을 지켰는지 · 깨졌는지는 재료에 적힌 대로 쓰고, 제목과 본문이 서로 어긋나지 않게 한다 ('턱걸이 마감'을 '붕괴'로 쓰지 않는다).
2. 글은 이어지게 쓴다. 문단 안에서 '무슨 일이 있었나 → 왜 그랬나 → 그래서 무엇이 달라지나'가 읽혀야 한다.
   원인 설명('…영향으로', '…때문에')은 재료(방송 · 블로그 데이터 news)에 그렇게 나온 것만 쓴다. 재료에 없는 원인을 지어내지 않는다.
3. 앞으로의 전망 · 평가는 누가 말했는지 붙인다 ('톰 리(펀드스트랫)는 …라고 봤습니다'). 사람 · 채널 이름은 문단마다 한두 번이면 된다.
   발언을 줄줄이 늘어놓지 말고, 시장 흐름을 설명하는 근거로 문장 속에 녹인다.
   말한 사람 없이 '~로 보입니다 · ~할 전망입니다 · ~로 예상됩니다 · ~라는 평가입니다'로 문장을 끝내지 않는다.
   다음 장에 미칠 영향은 예측이 아니라 사실로 쓴다 ('…가 한국 반도체주의 변수입니다', '갈림길은 …입니다').
   '전문가들 · 시장에서는 · 월가는 · 증권가는'처럼 흐린 주어를 쓰지 않는다.
4. 독자에게 사라 · 팔라 · 기다리라고 권하지 않는다. '…해야 합니다 · …할 필요가 있습니다'로 끝내지 않는다.
   추천 · 유망 · 매력적 · 투자 기회 같은 말은 쓰지 않는다. 수급은 '매도 · 매수 · 순매도 · 순매수 · 매도세'로 쓴다 ('팔자 · 사자'는 쓰지 않는다).
5. 실존 인물 · 회사의 비위 · 사고 주장(스캔들 · 의혹 · 수사 · 사임 · 소송 등)은 서로 다른 채널 2곳 이상이 다뤘거나 블로그 데이터 news에
   있을 때만 쓰고, 그 문장은 '~라고 ○○(채널 · 매체)가 전했습니다'로 끝낸다. 제목 · 소제목에는 넣지 않는다.
6. 자막 문장을 그대로 옮기지 않는다. 사람은 글에서 처음 나올 때 '이름 성(소속 직함)'으로 쓴다 ('톰 리(펀드스트랫 리서치 대표)').
   재료에 이름이 한 토막뿐이면('이사벨') 그 이름은 쓰지 않고 소속 · 직함으로만 쓴다 ('BNP 파리바 전략가는').
7. 쉬운 말로 쓴다. '투심 · 하방 압력 · 유동성 재편 · 차익 실현세 · 견인 · 이중고 · 극대화' 같은 상투어 대신
   '투자 심리 · 내리는 힘 · 돈이 옮겨 간다 · 이익을 챙기려는 매도 · 이끌었습니다'처럼 풀어 쓴다.
   분류 코드 · scope · stance · bullish 같은 내부 말은 쓰지 않는다. 종목 이름은 한글, 숫자는 '2.04%' 식.
   등락은 부호(+ · − · -)를 붙이지 않고 방향 말로 쓴다 ('1.82% 하락', '0.40% 상승'). 블로그 데이터의 음수 등락도 이렇게 바꿔 쓴다.
8. 문장은 모두 '-습니다'체로 끝낸다 ('마감했습니다', '이끌었습니다', '변수입니다'). '-다'체('마감했다')는 쓰지 않는다.
   제목 · 소제목 · 관전 포인트 · 표는 명사로 끝나는 구절이다. 설명이나 머리말 없이 JSON만 출력한다."""

STORY_PROMPT = """당신은 주식 투자 블로그 '알파스코프'의 시황 기자입니다. 재료는 아래 방송 요약과 블로그 데이터뿐입니다.
문체: 모든 문장을 '-습니다'체로 끝냅니다 ('마감했습니다', '이끌었습니다', '변수입니다'). '-다'체('마감했다')는 쓰지 않습니다.

이번 글: {when}. {what}
이 글이 다루는 장: {label}
오늘의 중심 사건: {center}

독자는 이 글 하나로 '장이 왜 그렇게 움직였고, 다음 장에서 무엇을 봐야 하는지'를 알고 싶어 합니다.
발언 모음이 아니라 한 편의 시황 기사로 씁니다. 먼저 아래 '변수 후보'에서 변수를 고르고, 그 변수들이 어떻게 이어지는지 정한 뒤 씁니다.

JSON 객체 하나로만 답합니다:
{{
  "title": "그날 주제 (30자 안팎). 무슨 일로 장이 어떻게 됐는지. 날짜 · '시황' 같은 앞머리와 줄표(—)는 쓰지 않는다 (블로그가 붙임). 예: '앤트로픽 IPO와 엔비디아 악재에 기술주 급락, 나스닥 2.47% 하락'",
  "summary": "검색 설명용 한 문장, 45자 안",
  "overview": "한눈에 보기. {overview_len} 한 문단. 지수가 어떻게 끝났고, 무엇이 장을 움직였고, 그 흐름이 다음 장에 어떻게 이어지는지",
  "market": "지수 · 업종 흐름. 3~4문장 한 문단. {market_rule}",
  "drivers": [
    {{"cand": 변수 후보 번호 (숫자),
      "heading": "변수 이름 (15자 안. 예: '실적 발표를 앞둔 경계')",
      "body": "한 문단 (길이는 아래 drivers 규칙). 무슨 일이 있었나 → 왜 시장에 중요한가 → 이 변수를 낙관하는 쪽과 걱정하는 쪽이 각각 무엇을 근거로 드는가(말한 사람을 붙여서. 한쪽만 있으면 한쪽만) → 마지막 문장은 전망이 아니라 사실로 끝낸다: 이 변수와 바로 이어지는 일정이 일정 표나 재료에 있으면 그것을, 없으면 지금 상태를 쓴다. 관계가 먼 일정을 억지로 붙이지 않는다"}}
  ],
  "sectors": [{{"name": "업종 이름 (블로그 데이터 close.sectors에 적힌 그대로)", "check": "다음 장에서 볼 것 한 구절 (20자 안)"}}],
  "checks": ["다음 장에서 확인할 것 1~2줄. 아래 후보와 일정 표에서 고른다. 각 줄은 '무엇 — 왜 보는지'를 명사로 끝나는 한 구절로 (예: '엔비디아 실적 — 데이터센터 매출이 예상을 넘는지'). 40자 안"],
  "one_line": "오늘의 핵심 한 줄. 권유나 예측이 아니라 오늘 장에서 일어난 일의 핵심을 사실 한 문장으로 ('-습니다'체). '…상태입니다 · …보여줍니다'로 끝내지 않는다",
  "banner": {{"titles": [["첫 줄", "둘째 줄"], ["첫 줄", "둘째 줄"]], "keys": ["무엇 + 방향 낱말", "…"]}},
  "tickers": ["본문에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [근거로 쓴 영상 번호 (숫자)]
}}

drivers: 변수는 아래 '변수 후보'에서만 고르고 후보 번호를 cand에 적는다. 번호가 앞선 후보부터 쓰고, 1번 후보는 꼭 다룬다.
한 후보에 여러 주제가 묶여 있으면 그 가운데 장과 가장 가까운 이야기를 중심으로 쓴다. 변수끼리 내용이 겹치지 않게 한다.
{drivers_rule}
sectors: {sectors_rule}
checks: {checks_rule}
banner (글 맨 위 그림에 얹는 짧은 글자. 그날 주제(title)를 짧게 줄인 말이고 새 주장을 넣지 않는다):
- titles: 제목 후보 2~3개, 좋은 것부터. 후보마다 [첫 줄, 둘째 줄]. 첫 줄 = 무슨 일, 둘째 줄 = 그래서 지금.
  띄어쓰기를 빼고 줄마다 한글 10자 이내 (예: ["기술주 내림 뒤", "엔비디아 실적 기다림"], ["기술주 급락", "실적 대기"]).
- keys: 시장 요약 2~3개, 띄어쓰기를 빼고 다 합쳐 한글 20자 이내. '무엇 + 방향 낱말' 꼴로, 방향 낱말
  (강세 · 약세 · 오름 · 내림 · 상승 · 하락 · 반등 · 관망 · 급락 · 급등 · 보합)을 끝에 둔다 (예: "미국 기술 업종 약세").
- 숫자 · % · 금액을 넣지 않는다. 줄표(—)를 쓰지 않는다. 비위 · 사고 낱말과 권하는 말은 제목과 같이 쓰지 않는다.

{rules}

변수 후보 (파이썬이 점수 순으로 매김: 다룬 채널 · 영상 수 + 그날 실제로 움직인 종목 · 업종과의 연결):
{driver_cands}

다음 장에서 확인할 것 후보 ({points_title}):
{candidates}

일정 표 (파이썬이 계산함. 일정의 날짜 말은 이 표대로만 쓰고, 표에 없는 일정에는 오늘 · 내일 · 이번 주 · 다음 주 같은 날짜 말을 붙이지 않는다):
{schedule}

블로그 데이터 (묶음 파일):
{briefing}

방송 요약 ({n}개, {span}):
{material}
"""

STORY_FIX_PROMPT = """아래 JSON은 시황 기사 원고입니다. 다음 문장에 걸린 점이 있습니다.

{problems}

걸린 문장만 고쳐 씁니다. 문단의 앞뒤 문장과 자연스럽게 이어지게 고치고, 나머지 문장과 칸은 그대로 둡니다.
- 말한 사람 없는 판단: 재료에서 그렇게 말한 사람 · 채널을 붙이거나, 예측을 빼고 사실로 고친다 ('…가 변수입니다', '갈림길은 …입니다')
- 비위 · 사고 주장: 문장을 '~라고 ○○(채널 · 매체)가 전했습니다'로 끝낸다. ○○는 재료에 나온 채널 · 매체 이름이다 ('외신 · 업계'는 안 됨).
  재료에서 한 곳만 말했으면 그 내용을 빼고 문장을 다시 쓴다
- 말한 사람이 흐림: '외신 · 증권가 · 투자자들은' 대신 재료의 이름 · 채널을 쓰거나, 주어를 사실(지수 · 종목 · 기관)로 바꾼다
- 재료에 없는 숫자 · 날짜, 원본(자막)에서 확인되지 않는 숫자: 그 숫자 · 날짜를 뺀다. 다른 숫자로 바꿔 넣지 않는다.
  숫자를 빼면 알맹이가 없어지는 문장('외국인이 물량을 내놓으며 수급을 흔들었습니다')은 문장째 빼고 앞뒤 문장을 잇는다
- 소속 · 직함이 재료와 다름: 재료에 적힌 소속 · 직함으로 고치거나 괄호 · 직함을 뺀다
- 이름 한 토막: 재료에 성까지 있으면 '이름 성(소속 직함)'으로, 없으면 이름을 빼고 '소속 직함'으로 쓴다 ('BNP 파리바 전략가는')
- 핵심 한 줄에 비위 · 사고 낱말: 그 낱말 없이 장에서 일어난 일로 다시 쓴다
- 재료에 없는 이름 · 말한 곳: 재료에 적힌 이름 · 채널로 고친다. 재료에서 찾을 수 없으면 그 문장을 빼고 앞뒤를 잇는다
- 권하는 말투: 명사로 끝나는 구절이나 사실 문장으로 고친다
- 상투어: 같은 뜻의 쉬운 말로 풀어 쓴다
- '-습니다'체가 아님: 그 문장을 '-습니다'체로 고친다 ('내렸다' → '내렸습니다'). 걸리지 않은 문장에도 '-다'체가 있으면 모두 고친다
같은 틀의 JSON 하나로만 답합니다. 문장은 '-습니다'체를 지킨다.

{draft}
"""

ADVICE_END = re.compile(r"(해야 한다|할 필요가 있다|필요가 있다|주시해야 한다|유효하다|바람직하다|"
                        r"해야 합니다|필요가 있습니다|유효합니다|바람직합니다)[.。]?$")
STORY_FORBIDDEN = re.compile(r"과매수|과매도|추천|목표 진입|D\+\d+|edition|stance|\bA/B\b|유보|테스트 중|"
                             r"강세론|약세론|칼럼|bullish|bearish|"
                             r"투자\s*기회|매력적|담을\s*만|사\s*둘\s*만|놓치면\s*안|유망", re.I)
PLAIN = {"순팔자": "순매도", "순사자": "순매수", "팔자세": "매도세", "사자세": "매수세", "팔자": "매도",
         "디위스킹": "위험 줄이기", "디리스킹": "위험 줄이기"}
CLICHE = re.compile(r"하방 압력|하방 압박|유동성 재편|차익 실현세|이중고|극대화|면치 못했다|직격탄|옥석 가리기|상존")
JOSA = {"과": "와", "이": "가", "을": "를", "은": "는"}   # '투심' → '투자 심리'로 바꾸면 받침이 없어짐 ('투심과' → '투자 심리와')


def plain(t):
    for a, b in PLAIN.items():
        t = t.replace(a, b)
    return re.sub(r"투심(과|이|을|은)?(?=[\s,.]|$)", lambda m: "투자 심리" + JOSA.get(m.group(1), ""), t).replace("투심", "투자 심리")


POLITE_END = re.compile(r"니다[.。]?$")
STORY_PARAS = ("overview", "market", "one_line")


# 변수 후보 순위 · 그날 글의 무게 (11-17 사용자: 무엇이 중요한지는 AI가 아니라 파이썬이 먼저 정한다)
MACRO = {"RATES_FED", "INFLATION", "GROWTH_JOBS", "TARIFF_TRADE", "GEOPOLITICS", "FISCAL_POLITICS", "MARKET_STRUCTURE",
         "FX_GLOBAL"}


# 닻으로 삼기에는 너무 넓은 낱말
ANCHOR_STOP = {"AI", "미국", "중국", "한국", "글로벌", "리스크", "영향", "강화", "확대", "변화", "사태", "문제", "이슈", "관련주",
               "기업", "산업", "업종", "종목", "정부", "경제", "정책", "예정", "임박", "지속", "심화", "둔화", "부각"}


def driver_candidates(items, briefing, market, log=None, limit=6):
    """장을 움직인 변수 후보를 점수 순으로. AI는 이 안에서만 변수를 고른다.

    방송 이슈의 주제(topic, 자유 문구)를 겹치는 낱말로 묶고, 묶음마다
    점수 = 다룬 채널 수 ×3 + 시장 · 업종 이야기로 다룬 채널 수 ×2 + 방향(오름 · 내림)을 붙인 채널 수 + 영상 수(10까지)
           + 시장 연결(그날 크게 움직인 종목 · 업종 · 지수).
    '많이 말한 것'만이 아니라 '시장에서 실제로 움직인 것과 이어진 것'이 앞에 오게 한다."""
    close = (briefing or {}).get("close") or {}
    movers = [m for m in close.get("movers") or [] if _pct(m.get("chg_pct"))]
    sectors = [x for x in close.get("sectors") or [] if _pct(x.get("chg_pct"))]
    idx = max((abs(float(x["chg_pct"])) for x in close.get("indices") or [] if _pct(x.get("chg_pct"))), default=0)
    # 주제(topic)는 자유 문구라 글자로는 잘 안 겹친다 → 여러 영상에 나온 낱말(엔비디아 · 앤트로픽 · 연준)을 닻으로 삼아 묶는다
    rows = []   # (영상 번호, 채널, 이슈, 낱말들)
    for n, v in enumerate(items, 1):
        ch = v.get("channelName") or v.get("channelId") or "?"
        for it in v.get("issues") or []:
            cat, country = it.get("category"), (it.get("country") or "").upper()
            ks = keys_of(str(it.get("topic") or "")) - ANCHOR_STOP
            if cat and cat != "OTHER" and not (country and country != market) and ks:
                rows.append((n, ch, it, ks))
    df = Counter(k for n, k in {(n, k) for n, _, _, ks in rows for k in ks})   # 낱말 → 나온 영상 수
    anchors = {k for k, c in df.items() if c >= 2}
    group = {k: k for k in anchors}   # 늘 같이 나오는 닻(연준 · 금리)은 하나로

    def root(k):
        while group[k] != k:
            k = group[k]
        return k
    count = Counter(k for _, _, _, ks in rows for k in ks & anchors)
    both = Counter((a, b) for _, _, _, ks in rows for a in ks & anchors for b in ks & anchors if a < b)
    for (a, b), c in both.items():
        if c / min(count[a], count[b]) >= 0.6:
            group[root(b)] = root(a)
    by = {}
    for n, ch, it, ks in rows:
        mine = ks & anchors
        if not mine:
            continue
        k = root(max(mine, key=lambda k: (df[k], k)))
        c = by.setdefault(k, {"keys": Counter(), "topics": Counter(), "channels": set(), "moved": set(), "videos": set(),
                              "tickers": Counter(), "cats": Counter(), "issues": 0, "wide": set()})
        c["issues"] += 1
        if it.get("scope") in ("market", "sector"):   # 종목 하나가 아니라 시장 · 업종 이야기로 다룬 채널
            c["wide"].add(ch)
        c["keys"].update(ks)
        c["topics"][str(it.get("topic")).strip()] += 1
        c["channels"].add(ch)
        c["videos"].add(n)
        c["cats"][it.get("category")] += 1
        c["tickers"].update({str(t).strip().upper() for t in it.get("tickers") or []})
        if it.get("stance") in ("bullish", "bearish"):
            c["moved"].add(ch)
    clusters = list(by.values())
    out = []
    for c in clusters:
        if len(c["channels"]) < 2 and len(c["videos"]) < 2:
            continue
        # 시장 연결은 그 묶음의 중심 낱말 · 중심 종목일 때만 (11-17: 한두 번 스친 '엔비디아'로 앤트로픽 · 비트코인 묶음까지 연결됨)
        keys, bonus, link = {k for k, _ in c["keys"].most_common(4)}, 0.0, []
        main = {t for t, k in c["tickers"].items() if k * 3 >= c["issues"]}
        hit = [m for m in movers if str(m.get("ticker") or "").upper() in main or m.get("name") in keys]
        if hit:
            m = max(hit, key=lambda m: abs(float(m["chg_pct"])))
            bonus += min(abs(float(m["chg_pct"])), 4)
            link.append(f"{m.get('name')} {_pct(m['chg_pct'])}")
        hit = [x for x in sectors if x.get("name") in keys]
        if hit:
            x = max(hit, key=lambda x: abs(float(x["chg_pct"])))
            bonus += min(abs(float(x["chg_pct"])), 3)
            link.append(f"{x.get('name')} 업종 {_pct(x['chg_pct'])}")
        if not link and idx >= 1 and c["cats"].most_common(1)[0][0] in MACRO:
            bonus += 2
            link.append("지수 전체가 1% 넘게 움직인 날의 거시 변수")
        # 11-17 한국장 시험: 종목 추천(철강 톱픽 · 가스공사 실적)이 '코스피 급락'과 같은 점수 → 시장 · 업종 이야기에 점수를 더 준다
        if not c["moved"] and not link:   # 11-17 한국장: '국가자격시험 부정행위 전수조사'가 후보 3번
            if log:
                log("  변수 후보에서 뺌 (방향 · 시장 연결 없음):", c['topics'].most_common(1)[0][0])
            continue
        score = 3 * len(c["channels"]) + 2 * len(c["wide"]) + len(c["moved"]) + min(len(c["videos"]), 10) + bonus
        tops = [t for t, _ in c["topics"].most_common(3)]
        out.append({"score": round(score, 1), "topic": tops[0], "more": tops[1:], "channels": len(c["channels"]),
                    "videos": sorted(c["videos"]), "link": " · ".join(link)})
    out.sort(key=lambda c: (c["score"], len(c["videos"])), reverse=True)
    out = out[:limit]
    for n, c in enumerate(out, 1):
        c["text"] = (f"{n}. {c['topic']}" + (f" (같이 나온 주제: {', '.join(c['more'])})" if c["more"] else "")
                     + f" — 채널 {c['channels']}곳 · 영상 {len(c['videos'])}개 (영상 {', '.join(map(str, c['videos'][:12]))})"
                     + (f" · 시장 연결: {c['link']}" if c["link"] else " · 시장 연결: 없음"))
        if log:
            log(f"  변수 후보 [점수 {c['score']}]", c['text'])
    return out


def day_shape(briefing, cands, log=None):
    """그날 글의 무게: 큰 사건 하나가 압도한 날 · 조용한 날 · 보통 날. 업종 표는 업종 사이 차이가 클 때만."""
    close = (briefing or {}).get("close") or {}
    idx = [abs(float(x["chg_pct"])) for x in close.get("indices") or [] if _pct(x.get("chg_pct"))]
    sec = [float(x["chg_pct"]) for x in close.get("sectors") or [] if _pct(x.get("chg_pct"))]
    big, spread = (max(idx) if idx else None), (max(sec) - min(sec) if sec else 0)
    lead = cands[0]["score"] / cands[1]["score"] if len(cands) > 1 and cands[1]["score"] else (9 if cands else 0)
    if lead >= 1.6 and (big is None or big >= 1):
        mode = "focus"
        rule = ("오늘은 후보 1번이 다른 후보를 크게 앞선 날이다. drivers 첫 문단은 후보 1번을 6~8문장으로 깊게 쓴다 "
                "(배경 → 무슨 일 → 왜 중요한가 → 양쪽 시각 → 다음에 확인될 사실). 꼭 필요하면 다른 후보 하나를 3~4문장으로 덧붙인다. drivers는 1~2개.")
        lo, hi, ov = 1, 2, "3~4문장"
    elif big is not None and big < 0.4 and spread < 1.5:
        mode = "quiet"
        rule = ("오늘은 지수 · 업종이 거의 움직이지 않은 조용한 날이다. 억지로 큰 사건처럼 쓰지 않는다. "
                "drivers는 1~2개, 문단마다 3~4문장으로 짧게 쓴다.")
        lo, hi, ov = 1, 2, "2~3문장"
    else:
        mode = "normal"
        rule = "drivers는 2~3개, 문단마다 4~6문장. 점수 차이가 큰 후보를 억지로 채우지 않는다 (2개면 충분하면 2개)."
        lo, hi, ov = 2, 3, "3~4문장"
    table = spread >= 2.0
    if log:
        log(f"글의 무게: {mode} (지수 최대 {big if big is not None else '모름'}% · 업종 차이 {spread:.2f}%p · "
            f"1번/2번 후보 점수 {lead:.2f}배) · 업종 표 {'넣음' if table else '안 넣음'}")
    return {"mode": mode, "rule": rule, "min": lo, "max": hi, "overview": ov, "table": table}


def story_out(raw, ctx):
    """AI 답을 다듬는다. 업종 등락은 묶음 숫자로 붙이고, 장 시작 줄과 겹치는 확인 줄은 뺀다."""
    from difflib import SequenceMatcher
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체 아님")
    out = {k: plain(_clean(raw.get(k))) for k in ("title", "summary", *STORY_PARAS)}
    out["title"] = undash(re.sub(r"^\d{1,2}\.\d{1,2}\s*(시황(\s*해설)?)?\s*[—–-]\s*", "", out["title"]))   # 19절 Q22: 주제만
    ds = [{"heading": undash(plain(_clean(d.get("heading")))), "body": plain(_clean(d.get("body"))),
           "cand": (_ints([d.get("cand")]) or [0])[0]}
          for d in raw.get("drivers") or [] if isinstance(d, dict) and _clean(d.get("body"))]
    n = len(ctx.driver_cands)
    ok = [d for d in ds if 1 <= d["cand"] <= n]
    if ok:   # 후보 밖에서 고른 변수는 빼고, 같은 후보는 하나만, 순위대로 놓는다 (글마다 변수 · 순서가 흔들리지 않게)
        seen, ds = set(), []
        for d in sorted(ok, key=lambda d: d["cand"]):
            if d["cand"] not in seen:
                seen.add(d["cand"])
                ds.append(d)
    out["drivers"] = ds[:ctx.shape["max"]]
    out["min_drivers"] = ctx.shape["min"]
    # 18절 Q19: 등락은 글이 아니라 숫자 칸 chg_pct (묶음 close.sectors 값 그대로). 블로그가 색 · 부호를 그린다
    moves = {x.get("name"): x.get("chg_pct") for x in ((ctx.briefing or {}).get("close") or {}).get("sectors") or []
             if _pct(x.get("chg_pct"))}
    out["sectors"] = [{"name": _clean(x.get("name")), "chg_pct": float(moves[_clean(x.get("name"))]),
                       "check": plain(_clean(x.get("check")))}
                      for x in raw.get("sectors") or [] if isinstance(x, dict) and _clean(x.get("name")) in moves][:5]
    if not ctx.shape["table"]:
        out["sectors"] = []
    must = [c for c in ctx.candidates if c["must"]]
    first = [f"{c['time']} — {c['what']}" if c.get("time") and c.get("what") else c["text"] for c in must]
    checks = []
    for x in (plain(_clean(x)).rstrip(".") for x in raw.get("checks") or []):
        if x and "첫 반응" not in x and not any(SequenceMatcher(None, x, f).ratio() >= 0.5 for f in first + checks):
            checks.append(x)
    out["first"], out["checks"] = first, checks[:2]
    out["tickers"] = [str(x) for x in raw.get("tickers") or []]
    out["sources"] = _ints(raw.get("sources") or [])
    out["banner"] = raw.get("banner") if isinstance(raw.get("banner"), dict) else {}
    return out


def undash(t):
    """줄표 ' — '는 쓰지 않는다 (19절 블로그 디자인 규칙 — 쉼표로)."""
    return re.sub(r"\s*[—–]\s*", ", ", t or "").strip(", ")


def hangul_len(t):
    """띄어쓰기 · 가운뎃점을 뺀 글자 수 (22절 Q27 배너 길이)."""
    return len(re.sub(r"[\s·]", "", t or ""))


BANNER_BAD = re.compile(r"[\d%]|억|달러|조\s*원")


def banner_out(raw, log=print):
    """22절 Q27 banner 칸. 숫자 · 금지 낱말 · 비위 낱말 · 권하는 말이 든 후보는 버린다 (고쳐 쓰기 호출은 하지 않음).
    길이를 넘은 후보는 버리지 않고 뒤로 (블로그가 줄여 그린다). 다 버려지면 빈 칸 → 블로그는 글 제목으로 그린다."""
    def bad(t):
        m = BANNER_BAD.search(t) or STORY_FORBIDDEN.search(t) or ALLEGATION.search(t) or ADVICE_END.search(t)
        return m.group() if m else None
    titles = []
    for c in (raw or {}).get("titles") or []:
        lines = [undash(plain(_clean(x))) for x in (c if isinstance(c, list) else [c])][:2]
        lines = [x for x in lines if x]
        why = next((b for b in map(bad, lines) if b), None) if lines else "빈 후보"
        if why:
            log("  배너 제목 후보 뺌:", f"{' / '.join(lines)} ({why})")
            continue
        titles.append(lines)
    titles.sort(key=lambda c: any(hangul_len(x) > 10 for x in c))   # 길이 안에 드는 후보를 앞으로 (안에서는 원래 순서)
    keys = []
    for x in (raw or {}).get("keys") or []:
        x = undash(plain(_clean(x)))
        why = bad(x) if x else "빈 칸"
        if why:
            log("  배너 요약 뺌:", f"{x} ({why})")
            continue
        keys.append(x)
    keys = keys[:3]
    while len(keys) > 2 and sum(map(hangul_len, keys)) > 20:
        keys.pop()
    for c in titles:
        if any(hangul_len(x) > 10 for x in c):
            log("  배너 제목 후보가 줄마다 10자를 넘음 (블로그가 줄여 그림):", " / ".join(c))
    if sum(map(hangul_len, keys)) > 20:
        log("  배너 요약이 20자를 넘음 (블로그가 줄여 그림):", " · ".join(keys))
    return {"titles": titles[:3], "keys": keys}


def story_ok(out):
    if not out["title"] or len(sentences(out["overview"])) < 2 or not out["one_line"]:
        raise ValueError("제목 · 한눈에 보기 · 핵심 한 줄이 모자람")
    out["drivers"] = [d for d in out["drivers"] if d["heading"] and len(sentences(d["body"])) >= 3]
    if len(out["drivers"]) < out.get("min_drivers", 2):
        raise ValueError(f"변수 문단이 {out.get('min_drivers', 2)}개가 안 됨")
    return out


# ---------- 발언자 대조 (11-17: '케빈 워시(연준 이사)' · '마크(블룸버그)' · flash-lite가 지어낸 '한국거래소는 … 분석했습니다') ----------
AFFIL = "[소속] "   # 고쳐 쓰게 하고, 그래도 남으면 문장을 빼지 않고 파이썬이 그 소속 · 직함만 지운다
PAREN_SPEAKER = re.compile(r"(?<![가-힣A-Za-z])([가-힣A-Za-z]+(?: [가-힣A-Za-z]+){0,2})\(([^()]{2,25})\)(?=(?:은|는|이|가)\s)")
TITLE_SPEAKER = re.compile(r"((?:[가-힣A-Za-z&]+ ){1,5})([가-힣]*(?:%s))(?=(?:은|는)\s)" % "|".join(sorted(TITLES, key=len, reverse=True)))
SPEECH_END = re.compile(r"((분석|평가|전망|지적|주장|설명|강조|경고|언급|예상|진단|해석|촉구|낙관)했습니다|"
                        r"봤습니다|보았습니다|밝혔습니다|말했습니다|전했습니다|내다봤습니다)[.。]?$")
LEAD_WORDS = {"반면", "이에", "대해", "또한", "한편", "그러나", "다만", "특히", "이와", "관련해", "앞서", "이날", "여기에"}


def _blocks(ctx):
    if not hasattr(ctx, "_blocks"):
        ctx._blocks = [b for b in (ctx.material + "\n\n" + ctx.briefing_json).split("\n\n") if b.strip()]
    return ctx._blocks


def _blocks_with(ctx, words):
    return [b for b in _blocks(ctx) if all(w in b for w in words)]


def speaker_problems(s, ctx):
    """문장에 나온 말한 사람 · 소속 · 직함이 재료에 그렇게 있는지. → ([걸린 점], {지울 문자열: 바꿀 문자열})"""
    out, strip = [], {}
    src = ctx.material
    for m in PAREN_SPEAKER.finditer(s):
        name, aff = m.group(1), m.group(2).strip()
        if re.search(r"[\d%]", aff) or not re.search(r"[가-힣A-Za-z]", aff):
            continue   # '삼성전자(005930.KS)가' · '에너지(−1.91%)와'
        toks = name.split()
        if len(toks) == 1:
            fulls = {f"{name} {y}" for y in re.findall(rf"(?<![가-힣]){re.escape(name)} ([가-힣]{{2,5}})", src)
                     if not any(PARTICLE.sub("", y).endswith(t) for t in TITLES) and PARTICLE.sub("", y) not in NOT_NAME}
            if fulls and name in src and aff.replace(" ", "") not in name:
                people = sorted(f for f in fulls if any(aff.replace(" ", "") in b.replace(" ", "") for b in _blocks_with(ctx, [f])))
                if people:
                    out.append(f"이름이 반쪽 ('{name}' → 재료에 나온 '{people[0]}'처럼 전체 이름으로)")
                    continue
            # 11-19 블로그: '이사벨(BNP 파리바)'처럼 외국 사람 이름 한 토막 → 성까지, 없으면 이름을 빼고 소속 · 직함만
            if not fulls and re.fullmatch(r"[가-힣]{2,5}", name) and foreign_name(name, ctx):
                out.append(f"{AFFIL}'{name}'은 이름 한 토막 (재료에 성까지 있으면 '이름 성({aff})'으로, 없으면 이름을 빼고 '{aff}'처럼 소속 · 직함만)")
                p = s[m.end():m.end() + 1]
                strip[m.group(0) + p] = aff + josa(aff, p)
                continue
        if not any(t in src for t in toks if len(t) >= 2) and name not in src:
            out.append(f"재료에 없는 이름 '{name}' (재료에 적힌 이름으로 쓰거나 그 문장을 뺀다)")
            continue
        blocks = _blocks_with(ctx, [name]) or _blocks_with(ctx, [toks[-1]])
        flat = aff.replace(" ", "")
        if blocks and not any(flat in b.replace(" ", "") or (keys_of(aff) and all(k in b for k in keys_of(aff))) for b in blocks):
            out.append(f"{AFFIL}'{name}'의 소속 · 직함 '{aff}'가 재료와 다름 (재료에 적힌 대로 쓰거나 괄호를 뺀다)")
            strip[m.group(0)] = name
    for m in TITLE_SPEAKER.finditer(s):
        toks = [t for t in m.group(1).split() if t not in LEAD_WORDS]
        title = m.group(2)
        base = next((t for t in TITLES if title.endswith(t)), title)
        if not toks or all(len(t) < 2 for t in toks):
            continue
        blocks = _blocks_with(ctx, toks[:2]) or _blocks_with(ctx, toks[:1])
        if not blocks:
            if not any(t in src for t in toks if len(t) >= 2):
                out.append(f"재료에 없는 이름 '{' '.join(toks)}' (재료에 적힌 이름으로 쓰거나 그 문장을 뺀다)")
            continue
        if not any(base in b for b in blocks):
            out.append(f"{AFFIL}'{' '.join(toks)}'의 직함 '{title}'이 재료에 없음 (재료에 적힌 대로 쓰거나 직함을 뺀다)")
            strip[f"{toks[-1]} {title}"] = toks[-1]
    if not out and SPEECH_END.search(s.strip()) and not PAREN_SPEAKER.search(s) and not TITLE_SPEAKER.search(s):
        m = re.match(r"^(.{2,40}?)(?:은|는)\s", s.strip())   # 기관 · 매체가 말한 것으로 쓴 문장
        via = re.search(r"고 ((?:[^\s]+ )?[^\s]+?)(?:이|가) [가-힣]+니다[.。]?$", s.strip())   # '…라고 CNBC가 전했습니다'면 말한 곳은 CNBC
        if m or via:
            toks = [PARTICLE.sub("", t) for t in (via.group(1) if via else m.group(1)).split() if t not in LEAD_WORDS]
            who = next((t for t in toks if len(t) >= 2 and t in src), toks[-1] if toks else "")
            m = via or m
            if len(who) >= 2 and who not in src:
                out.append(f"재료에서 이 말을 한 곳을 찾지 못함 ('{who}'는 재료에 없음. 재료의 이름 · 채널로 쓰거나 그 문장을 뺀다)")
            elif len(who) >= 2:
                ks = keys_of(s[m.end():])
                if ks and not any(len([k for k in ks if k in b]) >= 2 for b in _blocks_with(ctx, [who])):
                    out.append(f"{SOFT}'{who}'가 이 말을 했는지 재료에서 확인되지 않음")
    return out, strip


def foreign_name(name, ctx):
    """그 이름이 나온 영상이 모두 외국 방송(원래 제목에 한글이 없음)이면 외국 사람 → 한 토막 이름은 성이 빠진 것."""
    hit = [it for n, it in enumerate(ctx.items, 1) if name in _video_block(ctx, n)]
    return bool(hit) and all(not re.search(r"[가-힣]", str(it.get("title") or "")) for it in hit)


def _video_block(ctx, n):
    if not hasattr(ctx, "_vblocks"):
        ctx._vblocks = {i: material_text(ctx.items, {i}) for i in range(1, len(ctx.items) + 1)}
    return ctx._vblocks.get(n, "")


def josa(word, p):
    """word 뒤에 붙일 조사 p (은/는 · 이/가)를 받침에 맞춘다. 마지막 글자가 한글이 아니면 그대로."""
    c = (word or " ")[-1]
    if not ("가" <= c <= "힣") or p not in ("은", "는", "이", "가"):
        return p
    has = (ord(c) - 0xAC00) % 28 != 0
    return {"은": "은", "는": "은", "이": "이", "가": "이"}[p] if has else {"은": "는", "는": "는", "이": "가", "가": "가"}[p]


def raw_numbers(ctx):
    """원본 대조용 (11-19 블로그: 묶음에 없는 숫자는 수집기가 원본과 대조). 영상마다 요약 숫자 · 자막 숫자, 묶음 숫자."""
    if hasattr(ctx, "_raw"):
        return ctx._raw
    import store
    import summarize
    store.init()
    vids, subs = {}, {}
    for n, it in enumerate(ctx.items, 1):
        vids[n] = summarize.source_numbers(_video_block(ctx, n))
        try:
            t = store.get_transcript_text(it["id"])
        except Exception:   # 읽기 실패는 대조 못 함으로 둔다
            t = None
        subs[n] = summarize.source_numbers(t) if t else None
    brief = summarize.source_numbers(ctx.briefing_json) + [(k, abs(v)) for v in _json_numbers(ctx.briefing) for k in ("n", "%")]
    ctx._raw = {"videos": vids, "subs": subs, "brief": brief,
                "missing": sum(1 for v in subs.values() if v is None)}
    if ctx.log:
        ctx.log(f"원본 대조: 영상 {len(subs)}개 중 자막 없음 {ctx._raw['missing']}개")
    return ctx._raw


def raw_number_problem(s, ctx):
    """묶음에 없는 숫자가 그 숫자를 요약에 담은 영상의 자막(원본)에도 있는지. 없으면 그 숫자."""
    import summarize
    raw = raw_numbers(ctx)

    def has(v, kind, xs):
        return any(k == kind and abs(v - x) <= max(abs(x), 1e-9) * 0.02 for k, x in xs)
    for kind, v in summarize._vals(s, hedge_check.KO_NUM, hedge_check.KO_UNIT, skip_dates=True):
        if (kind == "n" and v <= 10 and v == int(v)) or has(v, kind, raw["brief"]):
            continue   # 작은 정수는 안 봄 · 묶음 숫자는 블로그가 대조
        vids = [n for n, xs in raw["videos"].items() if has(v, kind, xs)]
        if vids and not any(raw["subs"][n] is None or has(v, kind, raw["subs"][n]) for n in vids):
            return f"{v:g}{'%' if kind == '%' else ''}"
    return None


def story_replace(out, old, new):
    for k in ("summary", *STORY_PARAS):
        out[k] = out[k].replace(old, new)
    for d in out["drivers"]:
        d["body"] = d["body"].replace(old, new)


def story_problems(out, ctx):
    """[(문장, 제목인가, [걸린 점])]. 제목 · 소제목은 title 규칙으로 본다."""
    units = [(out["title"], True)] + [(d["heading"], True) for d in out["drivers"]] + [(out["summary"], False)]
    one = out["one_line"]   # 18절 Q19: 핵심 한 줄도 제목 칸처럼 비위 낱말을 넣지 않는다
    units += [(x, False) for k in STORY_PARAS for x in sentences(out[k])]
    units += [(x, False) for d in out["drivers"] for x in sentences(d["body"])]
    prose = {x for x, title in units if not title}   # 문장으로 쓰는 칸만 '-습니다'체를 본다 (관전 포인트 · 표는 구절)
    units += [(x, False) for x in out["checks"]] + [(x["check"], False) for x in out["sectors"]]
    found = []
    for s, title in units:
        p = sentence_problems(s, ctx, title, STORY_FORBIDDEN)
        if not any(x.startswith("재료에 없는 숫자") for x in p):
            x = raw_number_problem(s, ctx)
            if x:
                p.append(f"원본(자막)에서 확인되지 않는 숫자 {x} (요약에만 있음)")
        if s == one and ALLEGATION.search(s):
            p.append(f"핵심 한 줄에 '{ALLEGATION.search(s).group()}' 같은 말을 넣지 않는다")
        if s in prose and not POLITE_END.search(s.strip()):
            p.append("'-습니다'체가 아님 ('내렸다'를 '내렸습니다'로)")
        if not title and ADVICE_END.search(s.strip()):
            p.append("독자에게 권하는 말투 ('…해야 한다')")
        if s in prose:
            sp, strip = speaker_problems(s, ctx)
            p += sp
            for a, b in strip.items():
                ctx.strips.setdefault(s, {})[a] = b
        m = CLICHE.search(s)
        if m and not p:
            p.append(f"{SOFT}상투어 '{m.group()}'를 쉬운 말로")

        if p:
            found.append((s, title, p))
    return found


def near_numbers(s, ctx):
    """로그용: 숫자가 걸린 문장이면 재료에서 가장 가까운 숫자."""
    import summarize
    x = bad_number(s, ctx.numbers)
    if not x:
        return ""
    kind = "%" if x.endswith("%") else "n"
    return f" (재료의 가까운 숫자: {summarize._near(kind, float(x.rstrip('%')), ctx.numbers)})"


def story_drop(out, found):
    bad = {s for s, _, _ in found}
    for k in STORY_PARAS:
        out[k] = " ".join(x for x in sentences(out[k]) if x not in bad)
    for d in out["drivers"]:
        d["body"] = " ".join(x for x in sentences(d["body"]) if x not in bad)
    out["checks"] = [x for x in out["checks"] if x not in bad]
    out["sectors"] = [x for x in out["sectors"] if x["check"] not in bad]
    if out["summary"] in bad:
        out["summary"] = sentences(out["overview"])[0] if out["overview"] else ""
    return out


def story_compose(day, slot, items, briefing, model=None, log=print, deadline=None):
    """재료 → columns 문서 칸 (status 제외). 쓰지 않을 편이면 Skip.

    호출: 글 전체 1번 → 걸린 문장만 고쳐 쓰기 최대 2번 → 그래도 남으면 그 문장을 뺀다. 틀이 어긋나면 처음부터 (STORY_REWRITES).
    호출이 안 되면 STORY_PAUSE_SEC 쉬고 다시, 마감(deadline)과 STORY_WAIT_MIN 안에서만. 키마다 편당 요청 STORY_BUDGET번까지."""
    if briefing and briefing.get("session") == "closed":
        raise Skip("휴장")
    if not items:
        raise Skip("영상 없음")
    ctx = Ctx(day, slot, items, briefing, log)
    log(f"장: {ctx.label} · 중심 사건:", ctx.center['text'] if ctx.center else '없음')
    ctx.strips = {}
    ctx.driver_cands = driver_candidates(items, briefing, ctx.market, log)
    ctx.shape = day_shape(briefing, ctx.driver_cands, log)
    common = _common(ctx)
    common["rules"] = STORY_RULES.format(numbers_rule=common["numbers_rule"])
    sectors_rule = ("3~5개, 블로그 데이터에 있는 업종만. 등락 숫자는 따로 붙이니 쓰지 않는다." if ctx.shape["table"] else
                    "오늘은 업종 사이 차이가 작아 표를 넣지 않는다. 빈 배열.")
    cands = "\n".join(f"- {c['text']}" for c in ctx.candidates if not c["must"]) or "(없음)"
    has_first = any(c["must"] for c in ctx.candidates)
    market_rule = ("지수 등락 한 줄은 이미 따로 있으니 지수 숫자는 다시 쓰지 않는다. 블로그 데이터의 업종 · 종목 등락 숫자를 넣어 "
                   "어디가 많이 움직였고 어디가 버텼는지, 그 차이가 무엇을 말하는지" if ctx.index_line else
                   "방송에 나온 지수 · 수급 · 업종 숫자로 어디가 많이 움직였고 어디가 버텼는지")
    checks_rule = ("장 시작 줄(지난밤 장 뒤 첫 반응)은 따로 들어가니 쓰지 않는다. " if has_first else "") + \
        "다음 장과 이어지지 않는 일정은 고르지 않는다. 고를 것이 없으면 빈 배열."
    done = [e["name"] for e in ctx.schedule if e.get("done") and e.get("kind") == "EARNINGS"]
    drivers_rule = ctx.shape["rule"]
    if done:   # 11-18 시험: 장 마감 뒤 나온 엔비디아 실적이 글 끝에 묻힘
        names = " · ".join(done)
        drivers_rule += (f" 일정 표에서 '이미 발표됨'인 {names}은 지수 마감 뒤에 나와 지수 흐름에는 반영되지 않았지만 다음 장의 큰 재료다."
                         " 재료에 그 결과가 있으면 overview 둘째 문장에서 결과(재료에 적힌 숫자 · 표현만)와 시간 외 주가 반응(재료에 있을 때만)을 쓰고,"
                         " 그 회사와 이어지는 변수 문단도 그 결과로 시작한다. 재료에 결과가 없으면 '발표됐다'는 사실만 쓰고 결과를 짐작하지 않는다.")
        checks_rule += f" {names} 발표 뒤 첫 반응을 한 줄 넣는다 (예: '{done[0]} 뒤 반도체주 — 실적 결과가 주가로 이어지는지')."
    prompt = STORY_PROMPT.format(**common, label=ctx.label, points_title=points_title(day, slot), candidates=cands,
                                 market_rule=market_rule, checks_rule=checks_rule, sectors_rule=sectors_rule,
                                 drivers_rule=drivers_rule, overview_len=ctx.shape["overview"],
                                 driver_cands="\n".join(c["text"] for c in ctx.driver_cands) or "(없음 → 재료에서 직접 고른다, cand는 0)")
    import time
    m = model or STORY_MODEL
    if m.startswith("nvidia:"):
        config.LLM_PROVIDER, config.NVIDIA_MODEL = "nvidia", m.split(":", 1)[1]
    config.GEMINI_TEXT_MODEL, config.GEMINI_TEXT_FALLBACK = m, "off"
    keys, main_key = story_keys(), config.GEMINI_API_KEY
    started, rewrites = time.monotonic(), 0

    def late(extra=0):
        return deadline is not None and datetime.now(config.KST) + timedelta(seconds=extra) > deadline
    while True:
        if late():
            config.GEMINI_API_KEY = main_key
            raise Skip("마감 넘김")
        try:
            raw = story_call(prompt, f"이어 쓰는 글 {m}", keys, log)
            out = story_ok(story_out(raw, ctx))
            found = story_problems(out, ctx)
            for turn in (1, 2):   # 문단 첫 문장이 빠지면 글이 끊기므로 두 번까지 고쳐 쓰게 한다 (11-17 시험)
                if not found:
                    break
                log(f"  {m}: 걸린 문장 {len(found)}개 → 그 문장만 고쳐 쓰게 함 ({turn}번째)")
                for x, _, pr in found:
                    log(f"    - {' · '.join(pr)}:", f"{near_numbers(x, ctx)} {x}")
                probs = "\n".join(f'- "{x}" → {" · ".join(pr)}' for x, _, pr in found)
                try:
                    raw2 = story_call(STORY_FIX_PROMPT.format(problems=probs, draft=json.dumps(raw, ensure_ascii=False)),
                                      f"이어 쓰는 글 고쳐 쓰기 {m}", keys, log)
                    out2 = story_ok(story_out(raw2, ctx))
                except ValueError as e:   # 고쳐 쓴 글의 틀이 어긋남 → 처음 쓴 글에서 걸린 문장만 뺀다
                    log(f"  {m}: 고쳐 쓴 글이 틀에 안 맞음 ({e}) → 처음 쓴 글에서 걸린 문장만 뺌")
                    break
                raw, out = raw2, out2
                found = story_problems(out, ctx)
            for x, _, pr in found:
                if all(y.startswith(SOFT) for y in pr):
                    log(f"    그대로 둠 ({' · '.join(pr)}):", x)
            found = [f for f in found if not all(y.startswith(SOFT) for y in f[2])]
            for x, _, pr in found:
                if all(y.startswith((AFFIL, SOFT)) for y in pr):
                    new = x
                    for a, b in (ctx.strips.get(x) or {}).items():
                        new = new.replace(a, b)
                    log(f"    소속 · 직함만 지움 ({' · '.join(pr)}):", x)
                    story_replace(out, x, new)
            found = [f for f in found if not all(y.startswith((AFFIL, SOFT)) for y in f[2])]
            if found:
                if any(title for _, title, _ in found):
                    raise ValueError("제목 · 소제목에 걸린 점 남음: " + " · ".join(x for x, t, _ in found if t))
                for x, _, pr in found:
                    log(f"    문장 뺌 ({' · '.join(pr)}):", x)
                out = story_ok(story_drop(out, found))
            break
        except llm.LLMError as e:   # 두 키 모두 안 됨 (한도 · 혼잡) → 쉬었다가 다시. 건너뛰지 않고 기다린다
            waited = (time.monotonic() - started) / 60
            if isinstance(e, OutOfBudget) or waited + STORY_PAUSE_SEC / 60 > STORY_WAIT_MIN or late(STORY_PAUSE_SEC):
                config.GEMINI_API_KEY = main_key
                why = ("요청 상한" if isinstance(e, OutOfBudget) else "마감 전에 다시 할 시간 없음" if late(STORY_PAUSE_SEC)
                       else f"{waited:.0f}분 기다림")
                log(f"  {m}: 호출이 안 됨 ({e}) · {why} → 그 편 건너뜀")
                raise Skip("호출 실패") from e
            log(f"  {m}: 모든 키로 호출 실패 ({e}) → {STORY_PAUSE_SEC // 60}분 쉬고 다시 (지금까지 {waited:.0f}분)")
            time.sleep(STORY_PAUSE_SEC)
        except ValueError as e:
            rewrites += 1
            # 오류 글에 걸린 제목 · 소제목이 들어 있을 수 있어 뒤 인자로 뺀다
            log(f"  {m}: 틀 어긋남 → " + ("처음부터 다시 씀" if rewrites < STORY_REWRITES else "그 편 건너뜀") + ":", e)
            if rewrites >= STORY_REWRITES:
                config.GEMINI_API_KEY = main_key
                raise Skip("틀 어긋남") from e
    config.GEMINI_API_KEY = main_key
    log("요청 수: " + " · ".join(f"{k['name']} {k['used']}번" for k in keys) + f" (키마다 상한 {STORY_BUDGET})")
    # 같은 지표 두 값 · 이름 두 표기 (14절 2번 · 7번)
    paras = [{"body": out["overview"]}, {"body": out["market"]}, *out["drivers"]]
    _, paras = resolve_conflicts([], paras, ctx, log, protect=False)
    out["overview"], out["market"] = paras[0]["body"], paras[1]["body"]
    texts = [out["title"], out["summary"], out["overview"], out["market"], out["one_line"], *out["checks"],
             *(x for d in out["drivers"] for x in (d["heading"], d["body"]))]
    reps = unify_names(texts, log)

    def fix(t):   # 이름 한 표기 · 부호 없는 등락 ('−1.82%' → '1.82% 하락', 11-19 블로그)
        for x, y in reps.items():
            t = t.replace(x, y)
        return unsign(t)
    text = fix(" ".join(texts + out["first"]))
    tickers = [t for t in dict.fromkeys(x.strip().upper() for x in out["tickers"])
               if t in ctx.tickers and mentioned(t, ctx.tickers[t], text)]
    sources = list(dict.fromkeys(items[n - 1]["id"] for n in out["sources"] if 1 <= n <= len(items)))
    banner = banner_out(out["banner"], log)
    return {
        "date": day, "slot": slot, "reason": "", "format": "story", "briefing": f"{day}-{slot}" if briefing else None,
        "center": {"category": ctx.center["category"], "label": ctx.center["label"],
                   "channels": ctx.center["channels"]} if ctx.center else None,
        "title": undash(fix(out["title"])),   # 19절 Q22: 그날 주제만. 날짜 · 편 이름은 블로그가 붙인다
        "summary": fix(out["summary"]),
        "overview": fix(out["overview"]),
        "indexLine": ctx.index_line,   # 묶음 숫자, 부호 없이 '1.12% 하락' (index_line)
        "market": fix(out["market"]),
        "shape": ctx.shape["mode"],
        "drivers": [{"heading": fix(d["heading"]), "body": fix(d["body"]),
                     "topic": ctx.driver_cands[d["cand"] - 1]["topic"] if 1 <= d["cand"] <= len(ctx.driver_cands) else ""}
                    for d in out["drivers"]],
        "sectors": out["sectors"],
        "pointsTitle": points_title(day, slot),
        "points": [fix(x) for x in out["first"] + out["checks"]],
        "oneLine": fix(out["one_line"]),
        "banner": banner,
        "tickers": tickers, "sources": sources, "model": (m if m.startswith("nvidia:") else f"gemini:{m}") + (" (예비 키)" if keys[0]["name"] == "예비 키" else ""),
        "calls": {k["name"]: k["used"] for k in keys},
    }


def story_markdown(doc):
    """시험 결과로 보는 글 (블로그 화면 순서: 제목 줄 → 배너 → 머리글 → 본문)."""
    dt = datetime.strptime(doc["date"], "%Y-%m-%d")
    head = f"{dt.month}.{dt.day:02d} {'미국' if doc['slot'] == 'am' else '한국'} 증시시황"
    b = doc.get("banner") or {}
    out = [f"# {head} : {doc['title']}", "", f"_검색 설명: {doc['summary']}_", "",
           "배너 제목 후보: " + (" | ".join(" / ".join(c) for c in b.get("titles") or []) or "(없음)"),
           "배너 요약: " + (" · ".join(b.get("keys") or []) or "(없음)"), "",
           "## 한눈에 보기", "", doc["overview"], "", "## 지수 · 업종 흐름", ""]
    if doc["indexLine"]:
        out += [f"- {doc['indexLine']}", ""]
    out += [doc["market"], "", "## 장을 움직인 변수", ""]
    for n, d in enumerate(doc["drivers"], 1):
        out += [f"**{n}. {d['heading']}**", "", d["body"], ""]
    if doc["sectors"]:
        out += ["## 업종별로 볼 것", "", "| 업종 | 등락 (chg_pct) | 다음 장에서 볼 것 |", "|---|---|---|"]
        out += [f"| {x['name']} | {x['chg_pct']:+.2f} | {x['check']} |" for x in doc["sectors"]]
        out.append("")
    out += [f"## {doc['pointsTitle']}", ""] + [f"- {x}" for x in doc["points"]]
    out += ["", f"**핵심 한 줄**: {doc['oneLine']}", "", f"종목: {' · '.join(doc['tickers'])}",
            f"모델: {doc['model']} · 요청 수: {doc.get('calls')} · 글의 무게: {doc.get('shape')}"]
    return "\n".join(out)


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


def run(day, slot, status, force=False, dry=False, log=print, out=print, model=None):
    """한 편 쓰기. dry면 저장하지 않고 글을 out으로 (시험 결과). force · dry면 마감을 보지 않는다."""
    key = f"{day}-{slot}"
    start, end = window(day, slot)
    now = datetime.now(config.KST)
    log(f"시황 해설 {key}: 재료 {start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST, 마감 {end:%H:%M}")
    if not force and not dry and exists(key):
        log("이미 있음 → 끝 (다시 쓰려면 --force)")
        return None
    free = force or dry
    doc = {"date": day, "slot": slot, "format": "story", "reason": "", "title": "", "summary": "", "overview": "",
           "indexLine": "", "market": "", "drivers": [], "sectors": [], "pointsTitle": points_title(day, slot),
           "points": [], "oneLine": "", "banner": {}, "tickers": [], "sources": [], "briefing": None,
           "center": None, "model": ""}
    try:
        if not free and now > end:
            raise Skip("마감 넘김")
        items = load_summaries(start, end if free else min(end, now))
        briefing = load_briefing(day, slot, log)
        log(f"재료 영상 {len(items)}개, 묶음 {'있음' if briefing else '없음'}")
        doc["briefing"] = f"{day}-{slot}" if briefing else None
        doc.update(story_compose(day, slot, items, briefing, model, log, deadline=None if free else end))
        if not free and datetime.now(config.KST) > end:
            raise Skip("마감 넘김")
        doc["status"] = status
    except Skip as e:
        log(f"건너뜀: {e.args[0]}")
        doc.update(status="skip", reason=e.args[0], title="", summary="", overview="", market="", drivers=[],
                   sectors=[], points=[], oneLine="", banner={}, tickers=[], sources=[])
    if dry:
        if doc["status"] != "skip":
            out(f"\n===== 시황 글 =====\n{story_markdown(doc)}\n===== 끝 =====\n")
        out(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
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
    ap.add_argument("--dry", action="store_true", help="columns에 저장하지 않고 결과만 낸다 (Actions에서는 시험 결과에)")
    ap.add_argument("--story", action="store_true", help="--dry와 같음 (11-17 시험 모드 이름, 워크플로 입력 호환)")
    ap.add_argument("--model", help=f"쓸 모델 (기본 {STORY_MODEL}, NVIDIA는 nvidia:z-ai/glm-5.3)")
    a = ap.parse_args()
    now = datetime.now(config.KST)
    slot = a.slot if a.slot != "auto" else ("am" if now.hour < 12 else "pm")
    day = a.date or now.strftime("%Y-%m-%d")
    import os
    status = a.status or (os.environ.get("COMMENTARY_STATUS") or "draft-test")
    if status not in ("draft-test", "ready"):
        sys.exit(f"COMMENTARY_STATUS 값이 이상함: {status}")
    dry = a.dry or a.story
    # 저장소가 공개라 글 · 걸린 문장은 실행 로그에 찍지 않는다. 시험 결과(trials, 웹 설정 > 시험 결과)에 담는다 (11-18).
    # 집 PC에서는 전처럼 화면에도 찍힌다
    kind = "시험 (dry)" if dry else "쓰는 과정"
    rep = private.Report("commentary", f"시황 해설 {day}-{slot} {kind}")
    try:
        run(day, slot, status, force=a.force, dry=dry, log=rep.log, out=rep.line, model=a.model)
    finally:
        rep.save()


if __name__ == "__main__":
    main()

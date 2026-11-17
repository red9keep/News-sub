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
    if b.get("date") != day or b.get("slot") != slot:   # 14절 Q18: ahead 묶음은 generated가 이틀 전이라 date만 본다
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
            log(f"  중심 사건 후보 {c}: 방향 채널 {len(b['moved'])} · 채널 {len(b['channels'])} · 이슈 {b['issues']} — "
                + ", ".join(t for t, _ in b["topics"].most_common(4)))
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
        label = (f"{name}: 미국 {u.month}월 {u.day}일({WEEKDAY[u.weekday()]}){' 장 마감 뒤' if x.get('after_close') else ''}"
                 f" — 한국 독자에게는 '{rel_day(us_day, day)}'" + (f", 써도 되는 날짜 말: {' · '.join(sorted(ok))}" if ok else ""))
        out.append({"name": name, "keys": [k for k in keys if k], "label": label, "ok": ok})
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
        self.day, self.slot, self.items, self.briefing = day, slot, items, briefing
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

RULES = """글쓰기 규칙:
1. 재료에 없는 숫자 · 사건 · 종목은 쓰지 않는다.{numbers_rule}
2. 전망 · 평가는 누가 말했는지 붙인다 ('톰 리(펀드스트랫)는 …라고 봤다'). 우리 판단처럼 쓰지 않는다.
   말한 사람 없이 '~로 보인다 · ~로 전망된다 · ~라는 평가다 · ~가 기대된다'로 문장을 끝내지 않는다.
   말한 사람이 있는 평가도 좋다 · 나쁘다 · 매력적이다 · '긍정적으로 평가했다' 같은 값 매김은 옮기지 않고 무엇을 근거로 어떻게 봤는지를 숫자 + 사람으로 쓴다
   ('○○는 수익률 6%대를 높은 수준이라고 봤다').
3. 다음 말은 쓰지 않는다: 매수 · 매도(순매수 · 순매도 포함) · 과매수 · 과매도 · 추천 · 목표 진입 · 강세론 · 약세론 ·
   투자 기회 · 매력적 · 담을 만 · 사 둘 만 · 놓치면 안 · 유망.
   수급은 '외국인이 사들였다 · 팔았다'처럼 쓴다. 계산 신호는 '오름 쪽 · 내림 쪽 · 평소의 n배'처럼 사실의 말로 쓴다.
4. 실존 인물 · 회사의 비위 · 사고 주장(스캔들 · 의혹 · 수사 · 사임 · 소송 · 분식 등)은 서로 다른 채널 2곳 이상이 다뤘거나
   블로그 데이터 news에 있을 때만, '~라고 ○○(채널 · 매체)가 전했다'로 쓴다. 한 채널만 말한 것은 쓰지 않는다.
   제목에는 이런 말을 넣지 않는다.
5. 자막 문장을 그대로 옮기지 않는다. 바꿔 써서 요약한다. 사람 이름은 재료에 적힌 전체 이름(성과 이름)으로 쓴다.
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

일정 표 (파이썬이 계산함. 일정의 날짜 말은 이 표대로만 쓰고, 표에 없는 일정에는 오늘 · 내일 · 이번 주 · 다음 주 같은 날짜 말을 붙이지 않는다):
{schedule}

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
  "claim": "주장 한 문장. 중심 사건을 {side} 쪽에서 본 것. 우리 판단이 아니라 시각임이 드러나게 끝낸다 (예: '…라고 보는 쪽이다', '…라는 시각이다')",
  "evidence": ["근거 2~3개, 한 문장씩. 근거마다 말한 사람이나 채널을 붙인다 (예: 'CNBC에서 ○○는 …라고 봤다')"],
  "risk": "이 시각이 틀리는 조건 한 문장, 사실로 쓴다. 위 근거로 든 사건 · 숫자가 뒤집히는 조건으로 늘 '…면 이 시각은 힘을 잃는다.'로 끝낸다. {side} 쪽 주장과 반대로 일이 풀리는 조건이어야 한다 (예: '{risk_example}')",
  "facts_used": [위 공통 사실 가운데 이 시각의 근거와 이어지는 번호],
  "tickers": ["본문에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [근거로 쓴 영상 번호 (숫자). 방송에서 가져온 숫자가 있으면 그 영상은 꼭 넣는다]
}}

{rules}

일정 표 (파이썬이 계산함. 일정의 날짜 말은 이 표대로만 쓰고, 표에 없는 일정에는 오늘 · 내일 · 이번 주 · 다음 주 같은 날짜 말을 붙이지 않는다):
{schedule}

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
                rules=RULES.format(numbers_rule=numbers_rule), numbers_rule=numbers_rule,
                briefing=ctx.briefing_json or "(없음)",
                schedule="\n".join(f"- {e['label']}" for e in ctx.schedule) or "(없음)",
                n=len(ctx.items), span=f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST 받음", material=ctx.material)


def frame_prompt(ctx):
    if ctx.index_line:
        facts_rule = (f'"사실 2~3줄. 맨 앞 줄은 이미 있으니 쓰지 않는다: \'{ctx.index_line}\'. 그 뒤에 이어질 줄만"')
    else:
        facts_rule = '"사실 3~4줄. 장 날짜 · 장 이름은 쓰지 않는다 (첫 줄 앞에 따로 붙임)"'
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
    common = _common(ctx)
    if ctx.center:   # Q11: 중심 사건과 이어지지 않는 재료는 쓰지 않는다 → 시각 호출에는 중심 사건을 다룬 영상만 준다
        only = set(ctx.center["videos"])
        common["material"] = material_text(ctx.items, only)
        common["n"] = f"전체 {len(ctx.items)}개 중 중심 사건을 다룬 {len(only)}"
        vids = ", ".join(str(n) for n in sorted(only & {idx[v] for v in side_videos})) or "(중심 사건 영상에는 없음)"
    risk_example = ("엔비디아 실적에서 데이터센터 매출이 줄면 이 시각은 힘을 잃는다." if d == "bull" else
                    "엔비디아 실적에서 데이터센터 매출이 예상을 웃돌면 이 시각은 힘을 잃는다.")
    return VIEW_PROMPT.format(**common, side=DIR_KO[d], side_videos=vids, risk_example=risk_example,
                              facts="\n".join(f"{n}. {f}" for n, f in enumerate(facts, 1)))


# ---------- 결과 검사 ----------

def _clean(x):
    return " ".join(str(x or "").split()).strip()


# 원고에 남은 재료 번호 '[영상 39]' · '(영상 25, 26)' (11-17 시험: 근거 끝에 붙고 마침표가 빠짐)
VIDEO_REF = re.compile(r"\s*(\[\s*(영상\s*)?[\d,\s·~-]+\]|[(（]\s*영상\s*[\d,\s·~-]+[)）])")  # '[39]'도 (11-17 두 번째 시험)


def _sent(x):
    """본문에 들어갈 한 문장: 재료 번호를 빼고, 끝에 마침표가 없으면 붙인다."""
    x = _clean(VIDEO_REF.sub("", str(x or "")))
    return x + "." if x and x[-1] not in ".!?。…" else x


def distinct(xs, limit=0.6):
    """말한 사람만 다르고 내용이 거의 같은 문장은 앞의 것만 남긴다."""
    from difflib import SequenceMatcher
    out = []
    for x in xs:
        body = re.sub(r"^.*?(은|는) ", "", x, count=1)
        if not any(SequenceMatcher(None, body, re.sub(r"^.*?(은|는) ", "", y, count=1)).ratio() >= limit for y in out):
            out.append(x)
    return out


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
        if t:   # '장 마감 뒤 — 장 마감 후 예정된 …' · '오늘 밤 미국 장 마감 뒤 — 오늘 밤 엔비디아 …' (11-17 시험)
            words = {t, t.replace("뒤", "후"), t.replace("후", "뒤"), "오늘 밤", "내일 밤", "오늘", "내일", "미국",
                     "장 마감 뒤", "장 마감 후", "장 시작 전"}
            again = True
            while again:
                again = False
                for w in sorted(words, key=len, reverse=True):
                    if w and text.startswith(w):
                        text = text[len(w):].lstrip(" ,·—-")
                        again = True
                        break
        line = f"{t} — {text}" if t else text
        if len(line) <= 45:
            out.append(line)
    return out[:3]


def frame_out(raw, ctx):
    """공통 틀 답 → dict. 틀이 어긋나면 ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체가 아님")
    facts = [_sent(x) for x in raw.get("facts") or [] if _sent(x)]
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
        "claim": _sent(raw.get("claim")),
        "evidence": distinct([_sent(x) for x in raw.get("evidence") or [] if _sent(x)])[:3],
        "risk": _sent(raw.get("risk")),
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
ALLEGATION = re.compile(r"스캔들|의혹|비리|비위|수사|사임|소송|분식|횡령|배임|(?<!경)기소|사기|소환|조사에 착수|조사하")
ATTRIBUTED = re.compile(r"(전했다|보도했다|밝혔다|말했다|지적했다|주장했다|발표했다|봤다|"
                        r"전했습니다|보도했습니다|밝혔습니다|말했습니다|지적했습니다|주장했습니다|발표했습니다|봤습니다)[.。]?$")
# 말한 사람 없는 판단으로 끝나는 문장 (11-16 시험: '견고하다는 평가다' · '~로 전망된다')
JUDGMENT_END = re.compile(r"(전망된다|전망이다|보인다|예상된다|평가다|평가된다|기대된다|관측된다|분석된다|풀이된다|"
                          r"여겨진다|판단된다|전망이 나온다|전해졌다|알려졌다|나타났다고 한다|"
                          r"전망됩니다|전망입니다|보입니다|예상됩니다|평가입니다|평가됩니다|기대됩니다|관측됩니다|분석됩니다|"
                          r"풀이됩니다|여겨집니다|판단됩니다|전망이 나옵니다|전해졌습니다|알려졌습니다)[.。]?$")
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


FACT_OPINION = re.compile(r"(분석했다|봤다|보았다|평가했다|전망했다|언급했다|주장했다|예상했다|진단했다|해석했다|"
                          r"경고했다|지적했다|내다봤다|강조했다|풀이했다|관측했다)[.。]?$")
CLAIM_END = re.compile(r"(시각이다|보는 쪽이다|쪽이다|시각도 있다)[.。]?$")
FACT_CAUSE = re.compile(r"여파로|때문에|영향으로|탓에|탓으로|에 따른|으로 인해|로 인해")
RISK_END = re.compile(r"힘을 잃는다[.。]?$")
OTHER_MARKET = {"US": re.compile(r"한국은행|금통위|코스피|코스닥|한국장"), "KR": None}


def bad_when(s, ctx):
    """일정 표에 있는 일정을 다른 날짜 말로 쓴 것 (14절 1번)."""
    for e in ctx.schedule:
        if any(k in s for k in e["keys"]):
            for w in DATE_WORDS:
                if w in s and w not in e["ok"]:
                    return f"'{e['name']}' 날짜 말 '{w}'가 일정과 다름 ({e['label']})"
    return None


def keys_of(text):
    out = set()
    for t in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9&]+", text):
        t = PARTICLE.sub("", t)
        if len(t) >= 2 and t not in COMMON and t not in GENERIC:
            out.add(t)
    return out


def risk_problem(v):
    """틀리는 조건이 제목 · 주장 · 근거와 겹치는 낱말이 하나도 없으면 (11-17 시험: 수급 이야기 끝에 엔비디아 실적)."""
    if not v.get("risk"):
        return None
    body = " ".join([v.get("heading", ""), v.get("claim", ""), *v.get("evidence", [])])
    if not any(k in body for k in keys_of(v["risk"])):
        return SOFT + "틀리는 조건이 주장 · 근거와 이어지지 않음"
    return None


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


def key_problems(key, s, ctx):
    """칸마다 따로 보는 것: 사실 칸 의견 · 원인(3번), 근거의 중심 사건(4번), 관전 포인트 나라(5번)."""
    out = []
    if key == "claim" and not CLAIM_END.search(s.strip()):   # 11-17: 주장이 '…라고 전했다'로 바뀌어 근거와 겹침
        out.append(SOFT + "주장은 '…라는 시각이다 · …라고 보는 쪽이다'로 끝낸다 (채널 · 사람 이야기는 근거 칸 몫)")
    if key == "facts":
        if FACT_OPINION.search(s.strip()):
            out.append("사실 칸에 의견 (말한 사람의 판단은 시각 쪽 몫)")
        m = FACT_CAUSE.search(s)
        if m:
            out.append(f"사실 칸에 원인 풀이 '{m.group()}'")
    elif key == "evidence" and ctx.center_keys and not any(k in s for k in ctx.center_keys):
        out.append(SOFT + "근거에 중심 사건 이야기가 없음")
    elif key == "points":
        r = OTHER_MARKET.get(ctx.next)
        m = r.search(s) if r else None
        if m:
            out.append(f"다음 장과 다른 나라 일정 '{m.group()}'")
    return out


def units(kind, out):
    """검사할 칸: [(칸, 번호 또는 None, 글, 제목인가)]"""
    if kind == "frame":
        return ([("title_topic", None, out["title_topic"], True), ("summary", None, out["summary"], True)]
                + [("facts", i, f, False) for i, f in enumerate(out["facts"])]
                + [("points", i, p, False) for i, p in enumerate(out["points"])])
    return ([("heading", None, out["heading"], True), ("claim", None, out["claim"], False)]
            + [("evidence", i, e, False) for i, e in enumerate(out["evidence"])] + [("risk", None, out["risk"], False)])


def find_problems(kind, out, ctx):
    """→ [(칸, 번호, 문장, 제목인가, [걸린 점])]"""
    found = []
    for key, i, text, title in units(kind, out):
        for s in [text] if title or key == "points" else sentences(text):
            p = sentence_problems(s, ctx, title)
            p = p + key_problems(key, s, ctx)
            if p:
                found.append((key, i, s, title, p))
    if kind == "view" and out.get("risk") and not RISK_END.search(out["risk"].strip()):   # 14절 6번
        hit = [f for f in found if f[0] == "risk"]
        msg = "틀리는 조건은 '…면 이 시각은 힘을 잃는다.'로 끝낸다"
        if hit:
            hit[0][4].append(msg)
        else:
            found.append(("risk", None, out["risk"], False, [msg]))
    if kind == "view":
        p = risk_problem(out)
        if p:
            hit = [f for f in found if f[0] == "risk"]
            if hit:
                hit[0][4].append(p)
            else:
                found.append(("risk", None, out["risk"], False, [p]))
    return found


def drop_sentences(out, found):
    """걸린 문장만 뺀다 (고쳐 쓰게 해도 남은 것). 빈 줄이 된 사실 · 근거는 줄째 뺀다."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in out.items()}
    for key, i, s, _, _ in found:
        if i is None:
            out[key] = " ".join(x for x in sentences(out[key]) if x != s)
        else:
            out[key][i] = "" if key == "points" else " ".join(x for x in sentences(out[key][i]) if x != s)
    for key in ("facts", "evidence", "points"):
        if key in out:
            out[key] = [x for x in out[key] if x.strip()]
    return out


FIX_PROMPT = """아래 JSON은 시황 해설 원고입니다. 다음 문장에 걸린 점이 있습니다.
{problems}

고치는 법:
- 쓰면 안 되는 말: 같은 뜻의 사실 말로 바꾼다 ('외국인이 순매도했다' → '외국인이 주식을 팔았다', '매도 의견' → '낮춰 잡은 의견',
  '추천 종목' → '꼽은 종목', '매력적인 투자 기회라고 봤다' → '○○는 지금 수준을 낮다고 봤다'처럼 숫자 · 사람으로)
- 말한 사람 없는 판단 · 말한 사람이 흐림: 원고 안에 그렇게 말한 사람 · 채널이 나와 있으면 '○○는 …라고 봤다'로 고치고, 없으면 그 문장을 뺀다
- 재료에 없는 숫자 · 날짜: 그 숫자 · 날짜를 빼고 쓴다
- 날짜 말이 일정과 다름: 걸린 점에 적힌 날짜 말로 고치거나 날짜 말을 뺀다
- 사실 칸에 의견: 그 줄을 뺀다 (사실 칸은 일어난 일만). 사실 칸에 원인 풀이: '~여파로 · ~때문에' 같은 원인 부분을 빼고 일어난 일만 쓴다
- 근거에 중심 사건 이야기가 없음: 중심 사건과 이어지는 근거로 바꾸거나 그 근거를 뺀다
- 다음 장과 다른 나라 일정: 그 관전 포인트를 뺀다
- 틀리는 조건 끝맺음: '…면 이 시각은 힘을 잃는다.'로 끝나게 고친다
- 주장 끝맺음: 채널 · 사람 이름 없이 '…라는 시각이다' · '…라고 보는 쪽이다'로 고친다. 비위 주장은 주장 문장에 넣지 않는다
- 이름이 반쪽: 재료에 나온 전체 이름(성과 이름)으로 고친다
- 틀리는 조건이 주장 · 근거와 이어지지 않음: 이 시각의 근거로 든 사건 · 숫자가 뒤집히면 힘을 잃는다는 한 문장으로 다시 쓴다
- 비위 · 사고 주장: 채널 수가 모자라면 본문에서 그 문장을 빼고, '전했다'가 없으면 원고 안에 나온 채널 · 매체를 붙여
  '~라고 ○○가 전했다'로 고친다 (원고에 출처가 없으면 그 문장을 뺀다). 제목에서는 그 말을 빼고 중립 구절로 쓴다
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
                for _, _, s, _, p in found:
                    if all(x.startswith(SOFT) for x in p):
                        log(f"    그대로 둠 ({' · '.join(p)}): {s}")
                found = [f for f in found if not all(x.startswith(SOFT) for x in f[4])]
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
                    log(f"같은 지표 두 값: {name} '{p[3]}' (남길 값 쪽 재료 {support(keep)}곳 · 이 값 {support(p[2])}곳)"
                        + (" → 장 줄 · 주장 · 조건 문장이라 둠" if fixed else " → 그 문장 뺌"))
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
        log(f"이름 표기 하나로: '{x}' → '{y}'")
    return reps


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
    ctx = Ctx(day, slot, items, briefing, log)
    log(f"장: {ctx.label} · 중심 사건: {ctx.center['text'] if ctx.center else '없음'}")
    for n, c in enumerate(ctx.candidates, 1):   # 관전 포인트가 어디서 왔는지 (묶음 일정 · 방송 주제)
        log(f"  관전 포인트 후보 {n}: {c['text']}")
    base["center"] = {"category": ctx.center["category"], "label": ctx.center["label"],
                      "channels": ctx.center["channels"]} if ctx.center else None
    try:   # 한쪽만 실수로 나가지 않게 어느 호출이든 안 되면 그 편 전체 skip. 센 건수는 남긴다
        frame, m0 = ask("frame", frame_prompt(ctx), ctx, log)
        same = ctx.index_line.rstrip(".")   # AI가 지수 줄을 그대로 다시 쓰는 일 (11-17 시험)
        facts = ([ctx.index_line] if ctx.index_line else []) + [f for f in frame["facts"] if not same or f.rstrip(".") != same]
        if not ctx.index_line and ctx.label not in facts[0]:
            facts[0] = f"{ctx.label}: {facts[0]}"   # Q15: 첫 줄은 어느 장 이야기인지로 시작 (파이썬이 붙임)
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
    kept, body = resolve_conflicts(kept, body, ctx, log)   # 14절 2번
    reps = unify_names([*kept, *(b["body"] for b in body)], log)   # 14절 7번
    for x, y in reps.items():
        kept = [f.replace(x, y) for f in kept]
        for b in body:
            b["heading"], b["body"] = b["heading"].replace(x, y), b["body"].replace(x, y)
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


# ---------- 이어 쓰는 글 (11-17 사용자: 칸 채우기 글은 맥락이 없다 → 흐름이 있는 시황으로) ----------
# 지금은 시험 모드(--story, 저장 안 함). 블로그와 새 칸 형식을 맞춘 뒤 compose를 이 틀로 바꾼다 (요청 문서 17절).
# 호출: 글 전체 1번 → 걸린 문장이 있으면 그 문장만 고쳐 쓰게 1번 → 그래도 남으면 그 문장을 문단에서 뺀다.

STORY_MODELS = ["gemini-3.6-flash", *MODELS]   # 11-17 시험: flash-lite는 문단이 얇고 한쪽 시각만 씀

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
6. 자막 문장을 그대로 옮기지 않는다. 사람 이름은 재료에 적힌 전체 이름으로 쓴다.
7. 쉬운 말로 쓴다. '투심 · 하방 압력 · 유동성 재편 · 차익 실현세 · 견인 · 이중고 · 극대화' 같은 상투어 대신
   '투자 심리 · 내리는 힘 · 돈이 옮겨 간다 · 이익을 챙기려는 매도 · 이끌었습니다'처럼 풀어 쓴다.
   분류 코드 · scope · stance · bullish 같은 내부 말은 쓰지 않는다. 종목 이름은 한글, 숫자는 '2.04%' 식.
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
  "title": "제목. 무슨 일로 장이 어떻게 됐는지가 드러나게 (30자 안팎). 예: '금리 인하 기대 꺾이며 기술주 약세, 나스닥 1.2% 내려'",
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
  "one_line": "오늘의 핵심 한 줄. 권유나 예측이 아니라 지금 시장이 어떤 상태인지 한 문장 ('-습니다'체)",
  "tickers": ["본문에 이름이 나온 종목의 티커 (재료에 적힌 그대로)"],
  "sources": [근거로 쓴 영상 번호 (숫자)]
}}

drivers: 변수는 아래 '변수 후보'에서만 고르고 후보 번호를 cand에 적는다. 번호가 앞선 후보부터 쓰고, 1번 후보는 꼭 다룬다.
한 후보에 여러 주제가 묶여 있으면 그 가운데 장과 가장 가까운 이야기를 중심으로 쓴다. 변수끼리 내용이 겹치지 않게 한다.
{drivers_rule}
sectors: {sectors_rule}
checks: {checks_rule}

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
- 재료에 없는 숫자 · 날짜: 그 숫자 · 날짜를 뺀다. 다른 숫자로 바꿔 넣지 않는다
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
            log(f"  변수 후보 {c['text']} [점수 {c['score']}]")
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
    ds = [{"heading": plain(_clean(d.get("heading"))), "body": plain(_clean(d.get("body"))),
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
    moves = {x.get("name"): _pct(x.get("chg_pct")) for x in ((ctx.briefing or {}).get("close") or {}).get("sectors") or []}
    out["sectors"] = [{"name": _clean(x.get("name")), "move": moves[_clean(x.get("name"))], "check": plain(_clean(x.get("check")))}
                      for x in raw.get("sectors") or [] if isinstance(x, dict) and moves.get(_clean(x.get("name")))][:5]
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
    return out


def story_ok(out):
    if not out["title"] or len(sentences(out["overview"])) < 2 or not out["one_line"]:
        raise ValueError("제목 · 한눈에 보기 · 핵심 한 줄이 모자람")
    out["drivers"] = [d for d in out["drivers"] if d["heading"] and len(sentences(d["body"])) >= 3]
    if len(out["drivers"]) < out.get("min_drivers", 2):
        raise ValueError(f"변수 문단이 {out.get('min_drivers', 2)}개가 안 됨")
    return out


def story_problems(out, ctx):
    """[(문장, 제목인가, [걸린 점])]. 제목 · 소제목은 title 규칙으로 본다."""
    units = [(out["title"], True)] + [(d["heading"], True) for d in out["drivers"]] + [(out["summary"], False)]
    units += [(x, False) for k in STORY_PARAS for x in sentences(out[k])]
    units += [(x, False) for d in out["drivers"] for x in sentences(d["body"])]
    prose = {x for x, title in units if not title}   # 문장으로 쓰는 칸만 '-습니다'체를 본다 (관전 포인트 · 표는 구절)
    units += [(x, False) for x in out["checks"]] + [(x["check"], False) for x in out["sectors"]]
    found = []
    for s, title in units:
        p = sentence_problems(s, ctx, title, STORY_FORBIDDEN)
        if s in prose and not POLITE_END.search(s.strip()):
            p.append("'-습니다'체가 아님 ('내렸다'를 '내렸습니다'로)")
        if not title and ADVICE_END.search(s.strip()):
            p.append("독자에게 권하는 말투 ('…해야 한다')")
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


def story_compose(day, slot, items, briefing, model=None, log=print):
    """재료 → 이어 쓰는 글 칸. 안 되면 Skip."""
    if briefing and briefing.get("session") == "closed":
        raise Skip("휴장")
    if not items:
        raise Skip("영상 없음")
    ctx = Ctx(day, slot, items, briefing, log)
    log(f"장: {ctx.label} · 중심 사건: {ctx.center['text'] if ctx.center else '없음'}")
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
    prompt = STORY_PROMPT.format(**common, label=ctx.label, points_title=points_title(day, slot), candidates=cands,
                                 market_rule=market_rule, checks_rule=checks_rule, sectors_rule=sectors_rule,
                                 drivers_rule=ctx.shape["rule"], overview_len=ctx.shape["overview"],
                                 driver_cands="\n".join(c["text"] for c in ctx.driver_cands) or "(없음 → 재료에서 직접 고른다, cand는 0)")
    last = None
    for m in dict.fromkeys(([model] if model else []) + STORY_MODELS):
        config.GEMINI_TEXT_MODEL, config.GEMINI_TEXT_FALLBACK = m, "off"
        try:
            raw = llm.generate_json(prompt, log=log, what=f"이어 쓰는 글 {m}")
            out = story_ok(story_out(raw, ctx))
            found = story_problems(out, ctx)
            for turn in (1, 2):   # 문단 첫 문장이 빠지면 글이 끊기므로 두 번까지 고쳐 쓰게 한다 (11-17 시험)
                if not found:
                    break
                log(f"  {m}: 걸린 문장 {len(found)}개 → 그 문장만 고쳐 쓰게 함 ({turn}번째)")
                for x, _, pr in found:
                    log(f"    - {' · '.join(pr)}{near_numbers(x, ctx)}: {x}")
                probs = "\n".join(f'- "{x}" → {" · ".join(pr)}' for x, _, pr in found)
                try:
                    raw2 = llm.generate_json(STORY_FIX_PROMPT.format(problems=probs, draft=json.dumps(raw, ensure_ascii=False)),
                                             log=log, what=f"이어 쓰는 글 고쳐 쓰기 {m}")
                    out2 = story_ok(story_out(raw2, ctx))
                except (llm.LLMError, ValueError) as e:
                    # 고쳐 쓰기만 안 된 것 (11-17: 3.6 flash 무료 한도 429). 얇은 모델로 처음부터 다시 쓰는 것보다 쓴 글에서 걸린 문장만 뺀다
                    log(f"  {m}: 고쳐 쓰기 안 됨 ({e}) → 처음 쓴 글에서 걸린 문장만 뺌")
                    break
                raw, out = raw2, out2
                found = story_problems(out, ctx)
            for x, _, pr in found:
                if all(y.startswith(SOFT) for y in pr):
                    log(f"    그대로 둠 ({' · '.join(pr)}): {x}")
            found = [f for f in found if not all(y.startswith(SOFT) for y in f[2])]
            if found:
                if any(title for _, title, _ in found):
                    raise ValueError("제목 · 소제목에 걸린 점 남음: " + " · ".join(x for x, t, _ in found if t))
                for x, _, pr in found:
                    log(f"    문장 뺌 ({' · '.join(pr)}): {x}")
                out = story_ok(story_drop(out, found))
        except llm.LLMError as e:
            last = f"호출 실패 ({e})"
        except ValueError as e:
            last = f"틀 어긋남 ({e})"
        else:
            break
        log(f"  {m}: {last}")
    else:
        raise Skip("호출 실패")
    # 같은 지표 두 값 · 이름 두 표기 (14절 2번 · 7번)
    paras = [{"body": out["overview"]}, {"body": out["market"]}, *out["drivers"]]
    _, paras = resolve_conflicts([], paras, ctx, log, protect=False)
    out["overview"], out["market"] = paras[0]["body"], paras[1]["body"]
    texts = [out["title"], out["summary"], out["overview"], out["market"], out["one_line"], *out["checks"],
             *(x for d in out["drivers"] for x in (d["heading"], d["body"]))]
    reps = unify_names(texts, log)

    def fix(t):
        for x, y in reps.items():
            t = t.replace(x, y)
        return t
    text = fix(" ".join(texts + out["first"]))
    tickers = [t for t in dict.fromkeys(x.strip().upper() for x in out["tickers"])
               if t in ctx.tickers and mentioned(t, ctx.tickers[t], text)]
    sources = list(dict.fromkeys(items[n - 1]["id"] for n in out["sources"] if 1 <= n <= len(items)))
    dt = datetime.strptime(day, "%Y-%m-%d")
    return {
        "date": day, "slot": slot, "reason": "", "format": "story", "briefing": f"{day}-{slot}" if briefing else None,
        "center": {"category": ctx.center["category"], "label": ctx.center["label"],
                   "channels": ctx.center["channels"]} if ctx.center else None,
        "title": f"{dt.month}.{dt.day:02d} 시황 — {fix(out['title'])}",
        "summary": fix(out["summary"]),
        "overview": fix(out["overview"]),
        "indexLine": ctx.index_line,
        "market": fix(out["market"]),
        "shape": ctx.shape["mode"],
        "drivers": [{"heading": fix(d["heading"]), "body": fix(d["body"]),
                     "topic": ctx.driver_cands[d["cand"] - 1]["topic"] if 1 <= d["cand"] <= len(ctx.driver_cands) else ""}
                    for d in out["drivers"]],
        "sectors": out["sectors"],
        "pointsTitle": points_title(day, slot),
        "points": out["first"] + [fix(x) for x in out["checks"]],
        "oneLine": fix(out["one_line"]),
        "tickers": tickers, "sources": sources, "model": f"gemini:{m}",
    }


def story_markdown(doc):
    out = [f"# {doc['title']}", "", f"_{doc['summary']}_", "", "## 한눈에 보기", "", doc["overview"], "",
           "## 지수 · 업종 흐름", ""]
    if doc["indexLine"]:
        out += [f"- {doc['indexLine']}", ""]
    out += [doc["market"], "", "## 장을 움직인 변수", ""]
    for n, d in enumerate(doc["drivers"], 1):
        out += [f"**{n}. {d['heading']}**", "", d["body"], ""]
    if doc["sectors"]:
        out += ["## 업종별로 볼 것", "", "| 업종 | 등락 | 다음 장에서 볼 것 |", "|---|---|---|"]
        out += [f"| {x['name']} | {x['move']} | {x['check']} |" for x in doc["sectors"]]
        out.append("")
    out += [f"## {doc['pointsTitle']}", ""] + [f"- {x}" for x in doc["points"]]
    out += ["", f"**핵심 한 줄** — {doc['oneLine']}", "", f"종목: {' · '.join(doc['tickers'])}", f"모델: {doc['model']}"]
    return "\n".join(out)


def story(day, slot, model=None, log=print):
    """이어 쓰는 글 시험. 저장하지 않고 글만 출력."""
    start, end = window(day, slot)
    items = load_summaries(start, end)
    briefing = load_briefing(day, slot, log)
    log(f"이어 쓰는 글 시험 {day}-{slot}: 재료 영상 {len(items)}개, 묶음 {'있음' if briefing else '없음'}")
    try:
        doc = story_compose(day, slot, items, briefing, model, log)
    except Skip as e:
        log(f"건너뜀: {e.args[0]}")
        return None
    print(f"\n===== 이어 쓰는 글 =====\n{story_markdown(doc)}\n===== 끝 =====\n")
    return doc


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
    ap.add_argument("--story", action="store_true", help="이어 쓰는 글 시험 (저장 안 함)")
    ap.add_argument("--model", help="--story에서 먼저 써 볼 Gemini 모델")
    a = ap.parse_args()
    now = datetime.now(config.KST)
    slot = a.slot if a.slot != "auto" else ("am" if now.hour < 12 else "pm")
    day = a.date or now.strftime("%Y-%m-%d")
    import os
    if a.story:
        story(day, slot, a.model)
        return
    status = a.status or (os.environ.get("COMMENTARY_STATUS") or "draft-test")
    if status not in ("draft-test", "ready"):
        sys.exit(f"COMMENTARY_STATUS 값이 이상함: {status}")
    run(day, slot, status, force=a.force, dry=a.dry)


if __name__ == "__main__":
    main()

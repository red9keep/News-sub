"""종목 의견(picks) 수익률 · 적중률 (11-13, 데이터-설계 ③ 일별 주가의 첫 단계).

하루 1번 (prices.yml, 미국 장 마감 뒤). 유튜브 · VPN 요청 없음. 주가는 Yahoo Finance(yfinance, 수정 종가).

- 기준가: 방송이 나온 뒤 처음 맞는 장 마감 종가 (장 마감 뒤에 올라온 방송이면 다음 거래일)
- 1주 · 1개월 · 3개월 = 기준일에서 5 · 20 · 60 거래일 뒤 종가
- 시장 대비(초과 수익률) = 종목 수익률 - 같은 기간 지수 수익률 (미국 S&P 500, 한국 코스피 · 코스닥)
- 적중: 매수(buy)는 초과 수익률 > 0, 매도(sell)는 < 0. 보유 · 관심(hold · watch)은 수익률만 (적중은 안 셈)
- 티커가 있는 미국 · 한국 종목만. 비상장 · 해외 · 티커 없는 것은 건너뛴다

쓰는 곳:
- picks/{id}.perf: 위 값 (블로그가 읽음). 3개월까지 다 채우면 done=True, 그 뒤로는 다시 계산하지 않는다
- meta/pickStats: 전체 · 채널별 · 발언자별 적중률과 최근 의견 수익률 (웹 추천 종목 탭이 문서 1개만 읽게)

    python worker/prices.py --dry-run      # 계산만 찍고 쓰지 않음
"""
import argparse
import math
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf
from firebase_admin import firestore

import private
import store

HORIZONS = (5, 20, 60)
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
CLOSE = {"US": (time(16, 0), ET), "KR": (time(15, 30), KST)}
BENCH = {"US": "^GSPC", "KS": "^KS11", "KQ": "^KQ11"}
BENCH_NAME = {"^GSPC": "S&P 500", "^KS11": "코스피", "^KQ11": "코스닥"}
SCORED = {"buy": 1, "sell": -1}     # 적중을 세는 의견과 방향
RECENT_N = 150                      # meta/pickStats.recent 최근 의견 수
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB"}


def candidates(p):
    """picks 문서 → (시장, [야후 심볼 후보]) 또는 None"""
    t = (p.get("ticker") or "").strip().upper()
    m = (p.get("market") or "").upper()
    # 코인은 같은 글자의 미국 종목 · ETF로 잘못 잡힌다 (11-13 시험: 이더리움 "ETH")
    if t in CRYPTO or re.search(r"코인|비트코인|이더리움|리플|솔라나", p.get("name") or ""):
        return None
    if re.fullmatch(r"\d{6}", t):
        return "KR", [f"{t}.KS", f"{t}.KQ"]
    if m in ("US", "") and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
        return "US", [t.replace(".", "-")]   # BRK.B → BRK-B (야후 표기)
    return None


def published(p):
    v = p.get("publishedAt")
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            pass
    d = p.get("date")
    return datetime.fromisoformat(d).replace(tzinfo=KST) if isinstance(d, str) and d else None


def effective_day(pub, market):
    """이 날짜 이후(포함) 첫 거래일 종가가 기준가. 장 마감 뒤 방송이면 다음 날부터"""
    close, tz = CLOSE[market]
    local = pub.astimezone(tz)
    return local.date() + timedelta(days=1) if local.time() >= close else local.date()


def download(symbols, start):
    """{심볼: [(날짜, 종가)…]} (날짜 오름차순). 없는 심볼은 빠진다"""
    out = {}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), 80):
        chunk = symbols[i:i + 80]
        df = yf.download(chunk, start=start.isoformat(), auto_adjust=True, progress=False,
                         group_by="ticker", threads=True)
        for s in chunk:
            try:
                col = df[s]["Close"] if len(chunk) > 1 else df["Close"]
                if hasattr(col, "columns"):   # 한 개만 받을 때 열이 겹으로 오는 판이 있다
                    col = col.iloc[:, 0]
            except KeyError:
                continue
            rows = [(ix.date(), float(v)) for ix, v in col.dropna().items() if v and not math.isnan(v)]
            if rows:
                out[s] = rows
    return out


def at_or_before(rows, d):
    """rows(오름차순)에서 날짜 d 이하 마지막 종가"""
    last = None
    for day, v in rows:
        if day > d:
            break
        last = v
    return last


def perf_of(p, sym, rows, bench_sym, bench_rows, eff):
    idx = next((i for i, (d, _) in enumerate(rows) if d >= eff), None)
    if idx is None:
        return None   # 아직 기준일 종가가 없음
    base_day, base = rows[idx]
    b_base = at_or_before(bench_rows, base_day)
    sign = SCORED.get(p.get("action"))
    out = {"symbol": sym, "bench": bench_sym, "baseDate": base_day.isoformat(), "basePrice": round(base, 4)}

    def ret(j):
        d, v = rows[j]
        r = v / base - 1
        bv = at_or_before(bench_rows, d)
        b = bv / b_base - 1 if bv and b_base else None
        return d, r, b

    for n in HORIZONS:
        if idx + n < len(rows):
            _, r, b = ret(idx + n)
            x = r - b if b is not None else None
            out[f"r{n}"], out[f"b{n}"] = round(r, 4), (round(b, 4) if b is not None else None)
            out[f"x{n}"] = round(x, 4) if x is not None else None
            out[f"hit{n}"] = (x * sign > 0) if (sign and x is not None) else None
    d, r, b = ret(len(rows) - 1)
    out.update(nowDate=d.isoformat(), days=len(rows) - 1 - idx, rNow=round(r, 4),
               xNow=round(r - b, 4) if b is not None else None)
    out["done"] = all(f"r{n}" in out for n in HORIZONS)
    return out


def stats(picks):
    """전체 · 채널별 · 발언자별: 기간마다 적중 수 / 센 수, 평균 초과 수익률(매도는 부호를 뒤집어 '의견 방향으로 번 것')"""
    def empty():
        return {f"{k}{n}": 0 for n in HORIZONS for k in ("n", "hit")} | {f"sum{n}": 0.0 for n in HORIZONS} | {"picks": 0}

    groups = {"overall": defaultdict(empty), "channels": defaultdict(empty), "speakers": defaultdict(empty)}
    for p in picks:
        perf, sign = p.get("perf") or {}, SCORED.get(p.get("action"))
        if not perf or not sign:
            continue
        keys = {"overall": "전체", "channels": p.get("channelName") or "?", "speakers": p.get("speaker") or "?"}
        for g, k in keys.items():
            s = groups[g][k]
            s["picks"] += 1
            for n in HORIZONS:
                if perf.get(f"hit{n}") is not None:
                    s[f"n{n}"] += 1
                    s[f"hit{n}"] += int(perf[f"hit{n}"])
                    s[f"sum{n}"] += perf[f"x{n}"] * sign

    def rows(g, min_picks=1):
        out = []
        for k, s in groups[g].items():
            if s["picks"] < min_picks:
                continue
            row = {"name": k, "picks": s["picks"]}
            for n in HORIZONS:
                row[f"n{n}"], row[f"hit{n}"] = s[f"n{n}"], s[f"hit{n}"]
                row[f"avg{n}"] = round(s[f"sum{n}"] / s[f"n{n}"], 4) if s[f"n{n}"] else None
            out.append(row)
        return sorted(out, key=lambda r: (-r["picks"], r["name"]))

    return {"overall": (rows("overall") or [None])[0], "channels": rows("channels"),
            "speakers": rows("speakers", min_picks=2)[:60]}


def recent(picks):
    keep = ("name", "ticker", "action", "speaker", "channelName", "date", "videoId", "atSec", "source", "targetPrice", "currency")
    have = [p for p in picks if p.get("perf")]
    have.sort(key=lambda p: (p["perf"]["baseDate"], p.get("date") or ""), reverse=True)
    out = []
    for p in have[:RECENT_N]:
        f = p["perf"]
        out.append({**{k: p.get(k) for k in keep},
                    **{k: f.get(k) for k in ("baseDate", "days", "rNow", "xNow", "x5", "x20", "x60",
                                             "hit5", "hit20", "hit60", "bench")}})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=0, help="계산할 의견 수 상한 (시험용)")
    a = ap.parse_args()

    db = store.init()
    docs = [(d.id, d.to_dict()) for d in db.collection("picks").stream()]
    todo, skipped = [], defaultdict(int)
    for pid, p in docs:
        if (p.get("perf") or {}).get("done"):
            continue
        c, pub = candidates(p), published(p)
        if not c:
            skipped["티커 없음 · 미국 · 한국 아님"] += 1
            continue
        if not pub:
            skipped["방송 시각 없음"] += 1
            continue
        todo.append((pid, p, c[0], c[1], effective_day(pub, c[0])))
    if a.max:
        todo = todo[:a.max]
    print(f"의견 {len(docs)}건 중 계산할 것 {len(todo)}건, 건너뜀 {dict(skipped)}, 끝난 것 "
          f"{sum(1 for _, p in docs if (p.get('perf') or {}).get('done'))}건")

    start = (min((t[4] for t in todo), default=date.today()) - timedelta(days=7))
    first = [t[3][0] for t in todo]
    prices = download(first + list(BENCH.values()), start)
    # 한국 6자리: .KS(코스피)에 없으면 .KQ(코스닥)
    retry = [t[3][1] for t in todo if len(t[3]) > 1 and t[3][0] not in prices]
    if retry:
        prices.update(download(retry, start))

    changed, missing = 0, set()
    rep = private.Report("prices", "종목 의견 수익률 미리 보기")   # 의견 한 건씩은 공개 로그에 찍지 않는다 (11-18)
    batch, in_batch = db.batch(), 0
    for pid, p, market, syms, eff in todo:
        sym = next((s for s in syms if s in prices), None)
        if not sym:
            missing.add(syms[0])
            continue
        bench_sym = BENCH["KQ"] if sym.endswith(".KQ") else BENCH["KS"] if sym.endswith(".KS") else BENCH["US"]
        perf = perf_of(p, sym, prices[sym], bench_sym, prices.get(bench_sym, []), eff)
        if not perf:
            continue
        old = {k: v for k, v in (p.get("perf") or {}).items() if k != "updatedAt"}
        p["perf"] = perf
        if old == perf:
            continue
        changed += 1
        if a.dry_run:
            if changed <= 30:
                rep.line(f"  {p.get('date')} {p.get('channelName', '')[:14]:14} {p.get('name', '')[:12]:12} {sym:10} "
                         f"{p.get('action'):5} 기준 {perf['baseDate']} 지금 {perf['rNow']:+.1%} (시장 대비 {perf['xNow'] or 0:+.1%}) "
                         + " ".join(f"{n}일 {perf[f'x{n}']:+.1%}{'○' if perf[f'hit{n}'] else '×' if perf[f'hit{n}'] is False else ''}"
                                    for n in HORIZONS if perf.get(f"x{n}") is not None))
            continue
        batch.update(db.collection("picks").document(pid), {"perf": {**perf, "updatedAt": firestore.SERVER_TIMESTAMP}})
        in_batch += 1
        if in_batch >= 400:
            batch.commit()
            batch, in_batch = db.batch(), 0
    if in_batch:
        batch.commit()
    rep.save()
    if missing:
        print(f"주가를 못 찾은 심볼 {len(missing)}개: {', '.join(sorted(missing)[:40])}")

    # 통계는 끝난 것까지 전부로 (todo에서 바뀐 perf는 위에서 p에 반영됨)
    all_picks = [p for _, p in docs]
    st = stats(all_picks)
    o = st["overall"] or {}
    print("전체 적중률: " + " · ".join(
        f"{n}거래일 {o.get(f'hit{n}', 0)}/{o.get(f'n{n}', 0)}" + (f" (평균 {o[f'avg{n}']:+.1%})" if o.get(f"avg{n}") is not None else "")
        for n in HORIZONS))
    for r in st["channels"][:15]:
        print(f"  {r['name'][:20]:20} 의견 {r['picks']:3}  " + " ".join(f"{n}일 {r[f'hit{n}']}/{r[f'n{n}']}" for n in HORIZONS))
    print(f"바뀐 의견 {changed}건" + (" (dry-run, 쓰지 않음)" if a.dry_run else ""))
    if not a.dry_run:
        db.collection("meta").document("pickStats").set({
            **st, "recent": recent(all_picks), "horizons": list(HORIZONS), "benchNames": BENCH_NAME,
            "updatedAt": firestore.SERVER_TIMESTAMP})


if __name__ == "__main__":
    main()

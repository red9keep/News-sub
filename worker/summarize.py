"""영상별 구조화 요약. 자막 원문을 Gemini에 넣어 정해진 틀(JSON)로 채운다.

요약은 사람이 읽는 글이 아니라 나중에 통계를 내기 위한 표다. 그래서 이슈 분류(category)는
아래 CATEGORIES 목록에서만 고르게 하고(큰 분류, 통계용), 세부 주제는 topic에 자유로운 명사구로 둔다.
이슈마다 방향 · 놀람 · 범위 · 국가 · 기간 속성을 같이 저장한다. 목록을 바꾸면 이전 데이터와
끊기므로 되도록 늘리기만 한다 (설명은 doc/데이터-설계.md).
방송에서 특정 종목을 사라 · 팔라고 명시적으로 말한 것은 picks(종목 의견)에 따로 모은다 (10-28).

    명령줄 시험: python worker/summarize.py --dir trial-out   (trial 자막 파일들을 요약해 summaries.json으로)
"""
import argparse
import glob
import json
import os
import re

import config
import hedge_check
import llm

CATEGORIES = {
    # 큰 분류 17개 (10-22 확정). 통계용이라 고정. 세부 주제는 issues[].topic에 자유롭게 적는다.
    # 바꾸면 이전 데이터와 끊기므로 되도록 늘리기만 한다 (설명 · 경계 규칙은 doc/데이터-설계.md)
    # 11-07 블로그(AlphaScope issue_codes) 이름에 맞춤: RATES_CB→RATES_FED, ENERGY_COMMODITY→OIL_ENERGY, FX→FX_GLOBAL
    "RATES_FED": "금리 · 중앙은행 · 통화정책 (연준 · 일본은행 · ECB 등, 어느 나라든)",
    "INFLATION": "물가 · 인플레이션",
    "GROWTH_JOBS": "경기 · 고용 · 소비",
    "TARIFF_TRADE": "관세 부과 · 무역 협상 · 무역 적자",
    "GEOPOLITICS": "지정학 · 전쟁 · 안보 · 외교 · 재난 · 팬데믹",
    "OIL_ENERGY": "유가 · 에너지 · 금 · 원자재",
    "AI_CAPEX": "AI 투자 · 데이터센터 · 반도체 수요",
    "EARNINGS": "기업 실적 · 가이던스 (빅테크 포함, 종목은 tickers로)",
    "CORP_EVENT": "기업 사건: 인수합병 · 상장(IPO) · 자사주 · 분할 · 경영진 교체",
    "CREDIT_BANKS": "은행 · 신용 · 부실 · 사모대출 · 신용 경색",
    "MARKET_STRUCTURE": "시장 구조: 급락 · 변동성 상품 · 옵션 만기 · 지수 편입 · 밈 주식 · 수급",
    "REGULATION": "규제 · 반독점 · 산업 정책 · 수출 통제",
    "FISCAL_POLITICS": "재정 · 정치 (셧다운 · 선거 · 감세 · 예산)",
    "FX_GLOBAL": "환율 · 통화 가치 · 해외 증시 급변 (일본 · 유럽 · 신흥국)",
    "CRYPTO": "암호화폐",
    "REAL_ESTATE": "부동산 · 주택",
    "OTHER": "그 밖",
}
STANCES = ("bullish", "bearish", "neutral")
HORIZONS = ("days", "weeks", "months")
SURPRISES = ("expected", "surprise", "unknown")
SCOPES = ("market", "sector", "stock")
ACTIONS = ("buy", "sell", "hold", "watch")   # 종목 의견: 매수 · 매도 · 보유 · 관심
PICK_SOURCES = ("own", "report")             # 발언자 본인 의견 · 증권사 리포트 소개
MARKETS = ("US", "KR", "JP", "CN", "HK", "TW", "EU", "ETC")
MAX_LINES, MAX_LINE_CHARS = 5, 70   # issues[].lines: 블로그 팝업 요약 줄 수 · 한 줄 글자 수
MAX_CHARS = 120_000  # 자막이 이보다 길면 앞부분만 (약 30k 토큰). 호출 간격 · 재시도는 llm.py

PROMPT = """아래는 유튜브 경제 · 금융 영상의 자막 원문입니다. 주식 투자 블로그의 글감으로 쓰려고
정해진 틀에 맞춰 정리합니다. 나중에 "어떤 이슈가 어떤 종목에 영향을 줬는지" 통계를 내므로
틀을 정확히 지켜야 합니다.

JSON 객체 하나로만 답합니다:
{
  "title_ko": "영상 제목의 한국어 번역. 한국 뉴스 기사 제목처럼 자연스럽게, 고유명사 · 숫자는 살린다. 이미 한국어면 그대로",
  "summary": "한국어 3~5문장. 영상의 핵심 주장과 근거. 숫자는 자막에 나온 그대로",
  "issues": [
    {
      "category": "아래 분류표의 코드 중 하나",
      "topic": "세부 주제를 짧은 명사구로 (한국어 2~6단어, 예: '인도산 2차 관세', '엔비디아 3분기 실적', '연준 12월 인하 기대')",
      "stance": "bullish | bearish | neutral  (아래 '방향' 규칙 참고)",
      "surprise": "expected | surprise | unknown  (예정된 발표 · 시장 예상대로면 expected, 예상과 달랐으면 surprise, 영상에서 알 수 없으면 unknown)",
      "scope": "market | sector | stock  (영향 범위: 시장 전체 · 업종 · 개별 종목)",
      "tickers": ["이 이슈와 직접 관련된 종목 · ETF 심볼"],
      "country": "이슈의 주된 국가 (ISO 2자리: US, CN, JP, KR, EU 등. 특정 국가가 아니면 비움)",
      "horizon": "days | weeks | months  (영향이 이어질 기간, 발언 기준)",
      "note": "이 이슈에 대한 영상의 주장 한 문장 (한국어)",
      "lines": ["이 이슈에서 영상이 말한 내용을 한 줄에 하나씩, 최대 5줄 (한국어, 아래 '요약 줄' 규칙 참고)"]
    }
  ],
  "tickers": ["영상에서 언급된 모든 종목 · ETF 심볼 (미국 상장 기준, 예: NVDA, SMH, SPY)"],
  "sectors": ["언급된 섹터 · 업종 (한국어, 예: 반도체, 은행, 에너지)"],
  "keywords": ["글 태그로 쓸 핵심 단어 3~8개. 한국어 명사 또는 널리 쓰는 고유명사 (예: 관세, 연준, 금리 인하, 엔비디아, IPO, 호르무즈). 종목 심볼 · 섹터는 넣지 않는다"],
  "numbers": [{"what": "무엇의 수치인지", "value": "값 (단위 포함)", "period": "기간 · 시점"}],
  "speakers": ["발언자 이름 (소속)"],
  "picks": [
    {
      "name": "종목 이름 (한국어 표기, 예: 엔비디아, 삼성전자, 반에크 반도체 ETF)",
      "ticker": "미국 상장이면 심볼 (예: NVDA, SMH). 한국 상장은 6자리 종목코드를 확실히 알 때만. 모르면 비움",
      "market": "상장 시장 US | KR | JP | CN | HK | TW | EU | ETC",
      "kind": "opinion | news  (말한 사람 본인이나 소개한 증권사의 투자 판단이면 opinion, 주가 · 실적 · 사건 소식을 전하거나 주가 움직임을 설명한 것이면 news)",
      "action": "buy | sell | hold | watch  (아래 '종목 의견' 규칙 참고)",
      "target_price": "목표주가 숫자만 (예: 250). 말하지 않았으면 null",
      "currency": "목표주가 통화 USD | KRW | JPY 등. 목표주가가 없으면 비움",
      "speaker": "의견을 낸 사람 '이름 (소속)'. 증권사 리포트를 소개한 것이면 그 증권사 · 애널리스트 (예: '모건스탠리')",
      "source": "own | report  (출연자 본인의 의견이면 own, 증권사 · 투자은행의 등급 · 목표주가를 소개한 것이면 report)",
      "reason": "의견의 이유 한 문장 (한국어)",
      "quote": "의견을 말한 자막 줄을 그대로 한 줄 (100자 이내)",
      "at": "그 자막 줄의 시각 [mm:ss] 또는 [h:mm:ss]에서 괄호를 뺀 값 (예: 12:34)"
    }
  ]
}

규칙:
- issues는 영상이 실제로 다룬 이슈만, 중요한 순서로 1~4개. 첫 번째가 그 영상의 주도 이슈다.
- 요약 줄(lines)은 그 이슈에서 영상이 말한 내용을 줄마다 하나씩, 최대 5줄 (블로그 팝업에 그대로 보인다).
  · 한 줄 = 한 가지 내용. **명사형**으로 끝낸다 (발표 · 인상 · 기록 · 예정 · 감소 …). "-다" "-요" "-습니다"로 끝내지 않는다.
    마침표 없음. "—"를 쓰지 않는다. 한 줄 70자 안.
  · 숫자 · 사건 · 누가 무엇을 했는지는 그대로 적는다. **전망 · 평가 · 추천은 누가 말했는지 앞에 붙인다**
    ("톰 리 발언 : 연말 랠리 예상"처럼. 발언자를 모르면 "진행자 발언 :"). 방송은 의견이 많아 기사와 달리 이 표시가 필요하다.
  · 발언자가 추정 · 예상 · 어림으로 말한 숫자는 **그 말투를 살린다**. like · around · or so · about · roughly → "약" · "안팎",
    looks like · expect · could · I think → "~로 봄" · "~ 예상" (예: "2분기 약 145%", "4분기는 25% 안팎으로 더 느려질 것으로 봄").
  · 아직 발표되지 않은 기간(앞으로의 분기 · 연도)의 숫자는 반드시 "~ 예상" · "~로 봄"으로 쓴다. "둔화" · "기록" · "증가" 같은 확정된 일의 말로 끝내지 않는다.
  · 비상장사 실적처럼 공식 발표 · 공시 출처가 없는 수치(발언자가 말한 추산)는 줄 끝에 "(발언자 추정)"을 붙인다.
    예) "댄 나일스 발언 : 앤트로픽 분기 매출 증가율 2분기 약 145%, 3분기 약 45% (발언자 추정)"
  · 70자를 넘으면 숫자를 줄이고 "약" · "예상" · "(발언자 추정)" 같은 말투 표시는 남긴다.
  · 자막에 없는 내용은 쓰지 않는다. 짧게 다룬 이슈면 1~3줄로 줄인다. 억지로 다섯 줄을 채우지 않는다.
  · note(한 문장)는 그대로 둔다. lines의 첫 줄이 note를 되풀이하지 않게.
  · 종목 이름은 한글(엔비디아 · 테슬라), 금액은 "13억 달러" · "2.04%" 식.
- 분류 경계: 관세 부과 · 무역 협상 · 무역 적자는 TARIFF_TRADE, 수출 통제(반도체 수출 제한 등)는 REGULATION. 대만 · 안보 · 외교는 GEOPOLITICS.
  "미중" "일본" 같은 나라는 분류가 아니라 country에 적는다. 금리는 어느 나라 중앙은행이든 RATES_FED + country.
  환율과 해외 증시 급변(일본 · 유럽 · 신흥국 증시)은 FX_GLOBAL + country.
  실적 발표는 EARNINGS, 인수합병 · 상장 · 자사주 같은 사건은 CORP_EVENT. 분류표에 맞는 것이 없으면 OTHER.
- 방향(stance)은 "시장 전체"가 아니라 **그 이슈에 적은 종목 · 섹터 · 자산의 가격 방향**이다.
  tickers가 있으면 그 종목들의 주가 방향, 없으면 이슈가 가리키는 자산의 방향
  (OIL_ENERGY면 유가 · 금값, RATES_FED면 금리 · 채권 가격이 아니라 "금리 인하 = 주식에 bullish"처럼
  주식 시장 관점으로, CRYPTO면 코인 가격, FX_GLOBAL이면 해당 통화 · 시장). 시장 전체 영향은 적지 않는다.
  판단할 수 없으면 neutral.
- 종목 심볼은 대문자. 미국에 상장되지 않은 회사(한국 · 일본 등 해외 상장, 또는 SpaceX · OpenAI 같은 비상장)는
  "회사명(KR)" "회사명(비상장)"처럼 적는다.
- 자막에 없는 숫자나 종목을 만들지 않는다. 모르면 비운다.
- numbers도 요약 줄과 같이 구분한다. 발언자의 추정 · 어림이면 what 끝에 "(발언자 추정)", 아직 발표되지 않은 기간의 예상이면
  "(발언자 예상)"을 붙이고 value에 "약" · "안팎"을 살린다 (예: what "앤트로픽 4분기 매출 순차 증가율 (발언자 예상)", value "약 25%").
  공식 발표 · 공시된 숫자는 붙이지 않는다.
- 종목 의견(picks)은 **누군가 특정 종목 · ETF에 대해 좋다 · 나쁘다는 자기 판단이나 평가를 밝힌 것**을 빠짐없이 넣는다.
  "사라"고 딱 잘라 말하지 않아도 판단이 드러나면 의견이다. 영상 전체를 끝까지 훑어 하나도 빠뜨리지 않는다.
  넣는 것: 매수 · 매도 권유, "좋게 본다 · 아직 싸다 · 더 오른다 · 최선호주 · top pick · I like it · 들고 있다 · 더 샀다 ·
  팔았다 · 비싸다 · 조심해라 · 피해라", 이슈의 수혜주 · 피해주로 이름을 꼽은 것, 시청자 질문 · 라이트닝 라운드 답,
  방송이 전한 증권사 · 투자은행의 등급 · 목표주가 변경 (뉴스 진행자가 읽어 줘도 report로 넣는다).
  넣지 않는 것 (없으면 빈 배열):
  · 주가가 왜 올랐다 · 내렸다는 설명, 실적 · 이익 경고 · 사건 소식을 진행자 · 기자가 전한 것 ("BMW가 이익 경고를 냈다", "테슬라가 하락 압력").
    기자 · 진행자 본인이 "나는 이 종목을 좋게 본다"처럼 판단을 밝혔을 때만 넣는다.
  · 회사 경영진 · 관계자가 자기 회사를 말한 것 (CEO의 실적 자랑은 의견이 아니다).
  · 특정 종목 · ETF가 아닌 것: "한국 증시" "빅테크" "방산주" 같은 시장 · 업종 전체 (ETF 이름을 말했으면 그 ETF로 넣는다).
  buy = 매수 · 비중 확대 · 추천 · 담아라 · 지금 사도 된다 · "I'd buy" · 긍정적으로 본다 · 저평가 · 최선호 · 더 샀다 ·
        증권사 매수 등급 · 등급 상향 · 목표주가 상향.
  sell = 매도 · 비중 축소 · 팔아라 · 팔았다 · 고평가 · 피해라 · 차익 실현 권고 · 증권사 매도 등급 · 등급 하향 · 목표주가 하향.
  hold = 보유 · 중립 · 기다려라 · 들고만 있어라. watch = 수혜주 · 관심 종목으로 꼽았지만 사라고까지는 하지 않음, 또는 좋게 보지만 조건을 단 것.
  수혜주 · 피해주로 꼽기만 했으면 buy · sell이 아니라 watch다.
  한 사람이 여러 종목을 꼽으면 종목마다 한 건, 같은 종목을 여러 사람이 말하면 사람마다 한 건.
  진행자가 질문만 하고 의견은 출연자가 냈으면 speaker는 출연자다. 이름을 알 수 없으면 '진행자' '출연자'처럼 역할을 적는다.
- 설명이나 머리말 없이 JSON만 출력한다.

분류표:
""" + "\n".join(f"- {code}: {name}" for code, name in CATEGORIES.items())


class SummarizeError(Exception):
    pass


# 줄 끝 말투 표시: 70자로 자를 때 앞 내용을 줄이고 이것은 남긴다 (11-12 "(발언자 추정"으로 잘린 줄이 나옴)
LINE_TAGS = re.compile(r"\s*\((?:발언자 추정|발언자 예상|예상)\)$")


def _trim(head, n):
    """n자 안으로 자른다. 단어 중간이면 앞 띄어쓰기에서 끊는다 ("둔화" → "둔" 방지, 11-13). 너무 짧아지면 그냥 자른다"""
    if len(head) <= n:
        return head.rstrip(" ,·")
    cut = head[:n]
    if head[n] != " ":
        sp = cut.rfind(" ")
        if sp >= n * 0.6:
            cut = cut[:sp]
    return cut.rstrip(" ,·")


def _cut(x):
    if len(x) <= MAX_LINE_CHARS:
        return x
    m = LINE_TAGS.search(x)
    tag = " " + m.group().strip() if m else ""
    head = x[:m.start()] if m else x
    return _trim(head, MAX_LINE_CHARS - len(tag)) + tag


def _normalize(raw):
    """모델 응답을 틀에 맞게 다듬는다. 분류 · 방향 · 놀람 · 범위 · 기간은 허용값만 남긴다."""
    if not isinstance(raw, dict):
        raise SummarizeError("Gemini 답이 정해진 틀(JSON 객체)이 아님")

    def tickers(xs):
        out = []
        for x in xs or []:
            x = str(x).strip().upper()
            if x and x not in out and len(x) <= 24:
                out.append(x)
        return out[:30]

    def keywords(xs):
        out = []
        for x in xs or []:
            x = " ".join(str(x).split()).strip(" #,")
            if x and x not in out and len(x) <= 30:
                out.append(x)
        return out[:10]

    def lines(xs, note):
        """요약 줄: 문자열만, 공백 정리, 마침표 · 줄표 제거, 70자 안, note와 같은 줄 · 중복 제거, 최대 5줄"""
        def clean(x):
            return " ".join(str(x).split()).replace("—", "·").strip(" .。")
        out, note = [], clean(note)
        for x in xs if isinstance(xs, list) else []:
            x = clean(x)
            if x and x != note and x not in out:
                out.append(_cut(x))
        return out[:MAX_LINES]

    issues = []
    for it in (raw.get("issues") or [])[:4]:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category", "")).strip().upper()
        country = str(it.get("country") or "").strip().upper()
        note = str(it.get("note", ""))[:300]
        issues.append({
            "category": cat if cat in CATEGORIES else "OTHER",
            "topic": str(it.get("topic") or "").strip()[:60],
            "stance": it.get("stance") if it.get("stance") in STANCES else "neutral",
            "surprise": it.get("surprise") if it.get("surprise") in SURPRISES else "unknown",
            "scope": it.get("scope") if it.get("scope") in SCOPES else "",
            "tickers": tickers(it.get("tickers")),
            "country": country if len(country) <= 3 else "",
            "horizon": it.get("horizon") if it.get("horizon") in HORIZONS else "weeks",
            "note": note,
            "lines": lines(it.get("lines"), note),   # 블로그 뉴스 팝업용 요약 줄 (11-05)
        })
    picks, seen = [], set()
    for p in (_pick(x) for x in (raw.get("picks") or [])[:50]):
        # 같은 사람이 한 영상에서 같은 종목 의견을 두 번 말하면 (앞 요약 · 뒤 설명) 처음 것만
        if p and (pick_key(p), p["speaker"], p["action"]) not in seen:
            seen.add((pick_key(p), p["speaker"], p["action"]))
            picks.append(p)
    summary = str(raw.get("summary", "")).strip()
    if not summary:
        raise SummarizeError("Gemini 답에 요약 문장이 없음")
    return {
        "titleKo": " ".join(str(raw.get("title_ko") or "").split())[:200],  # 웹 첫 화면 제목 (10-23)
        "summary": summary[:2000],
        "issues": issues,
        "categories": sorted({i["category"] for i in issues}),  # 조회용 (array-contains)
        "tickers": tickers(raw.get("tickers")),
        "sectors": [str(s)[:30] for s in (raw.get("sectors") or [])][:20],
        "keywords": keywords(raw.get("keywords")),
        "numbers": [{"what": str(n.get("what", ""))[:80], "value": str(n.get("value", ""))[:40],
                     "period": str(n.get("period", ""))[:40]}
                    for n in (raw.get("numbers") or [])[:20] if isinstance(n, dict)],
        "speakers": [str(s)[:60] for s in (raw.get("speakers") or [])][:10],
        "picks": picks,
    }


def _price(x):
    """목표주가 → 숫자. '1,250' '250달러' 같은 문자열도 받는다. 없거나 0 이하면 None"""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        v = float(x)
    else:
        m = re.search(r"\d[\d,]*(?:\.\d+)?", str(x or ""))
        v = float(m.group().replace(",", "")) if m else 0
    return (int(v) if v == int(v) else round(v, 2)) if v > 0 else None


def _seconds(at):
    """'12:34' · '1:02:03' → 초. 아니면 None"""
    m = re.fullmatch(r"\[?(\d+(?::\d{1,2}){1,2})\]?", str(at or "").strip())
    if not m:
        return None
    sec = 0
    for part in m.group(1).split(":"):
        sec = sec * 60 + int(part)
    return sec


def pick_key(p):
    """같은 종목을 묶는 값: 티커, 없으면 띄어쓰기를 뺀 종목 이름 ('스페이스 X' = '스페이스X')"""
    return p["ticker"] or "".join(p["name"].split())


def _pick(p):
    """종목 의견 한 건을 틀에 맞게. 종목 이름 · 의견이 없으면 버린다"""
    if not isinstance(p, dict):
        return None
    name = " ".join(str(p.get("name") or "").split())[:40]
    # "005930(KR)" 처럼 시장을 괄호로 붙여 오면 괄호째 뺀다
    ticker = re.sub(r"[^A-Z0-9.\-]", "", re.sub(r"\(.*?\)", "", str(p.get("ticker") or "")).upper())[:12]
    action = str(p.get("action") or "").strip().lower()
    if not (name or ticker) or action not in ACTIONS:
        return None
    # 넓힌 규칙에서 섞여 들어오는 것 (11-04 시험): 기자가 전한 소식 · 주가 설명, 종목이 아닌 시장 전체 ("한국 증시")
    if str(p.get("kind") or "").strip().lower() == "news":
        return None
    if not ticker and re.search(r"(증시|시장|지수|업종)$", name):
        return None
    market = str(p.get("market") or "").strip().upper()
    price = _price(p.get("target_price"))
    return {
        "name": name or ticker,
        "ticker": ticker,
        "market": market if market in MARKETS else "",
        "action": action,
        "targetPrice": price,
        "currency": str(p.get("currency") or "").strip().upper()[:4] if price else "",
        "speaker": " ".join(str(p.get("speaker") or "").split())[:60],
        "source": p.get("source") if p.get("source") in PICK_SOURCES else "own",
        "reason": str(p.get("reason") or "").strip()[:200],
        "quote": " ".join(str(p.get("quote") or "").split())[:150],
        "atSec": _seconds(p.get("at")),
    }


# ---------- 요약 줄 검사 (11-12, AI 없이 글자 대조) ----------
# AI가 지시문을 놓쳐도: 자막에 없는 숫자가 든 줄은 빼고, 원문의 '대략 · 예상'이 빠진 줄은 "약" · "(예상)"을 붙인다.
# 숫자 대조는 AlphaScope news_store._num_problem과 같은 방식 (억 · 조 ↔ billion · trillion 환산). 말투는 hedge_check.py (AlphaScope에서 복사)

EN_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "dozen": 12, "hundred": 100,
            "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "half": 0.5}   # 서수는 "2, 3티어" ↔ second and third tier (11-13)
EN_WORD_UNIT = re.compile(r"\b(a|an|" + "|".join(k for k in EN_WORDS if k not in ("dozen", "half")) + r")\s+(trillion|billion|million|thousand|percent)\b", re.I)
# 범위 "70 to 80%" · "$1.3 to $1.4 trillion" · "2~2.5%": 앞 숫자에 단위가 없으면 뒤 숫자 단위를 같이 쓴다 (11-13, 맞는 줄이 빠지던 것)
RANGE_GAP = re.compile(r"\s*(?:to|or|and|-|–|~|에서|내지|부터)\s*\$?\s*", re.I)
TIMESTAMP = re.compile(r"\[\d+(?::\d{1,2}){1,2}\]")   # 자막 시각 표시 [03:18] (숫자 대조에서는 뺀다)
FWD_TAG = " (예상)"


def _vals(text, rx, units, skip_dates=False, keep_bare=False):
    """숫자 → [(종류, 값)]. `49만 7천` · `1조 2천억`처럼 큰 단위 뒤 작은 단위가 이어지면 한 수로 합친다 (news_store._vals)

    범위의 앞 숫자는 뒤 숫자 단위를 따른다. keep_bare(자막 쪽)면 단위 없는 원래 값도 같이 남긴다.
    """
    text = text or ""
    ms = list(rx.finditer(text))
    out, prev = [], None
    for i, m in enumerate(ms):
        if skip_dates and len(m.groups()) > 2 and m.group(3):   # 날짜 · 순위 · 분기 숫자
            prev = None
            continue
        v = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if not unit and i + 1 < len(ms) and ms[i + 1].group(2) and RANGE_GAP.fullmatch(text[m.end():ms[i + 1].start()]):
            if keep_bare:
                out.append(("n", v))
            unit = ms[i + 1].group(2).lower()
        u = "%" if unit == "%p" else units.get(unit, 1) if unit else 1
        if u == "%":
            out.append(("%", v))
            prev = None
            continue
        if prev and out and out[-1][0] == "n" and not text[prev[0]:m.start()].strip() and u < prev[1]:
            out[-1] = ("n", out[-1][1] + v * u)
        else:
            out.append(("n", v * u))
        prev = (m.end(), u)
    return out


def _word_vals(src):
    """글자로 쓴 숫자: "ten" · "a trillion dollar" · "twenty percent" """
    out = [("n", float(v)) for w, v in EN_WORDS.items() if re.search(rf"\b{w}\b", src, re.I)]
    for m in EN_WORD_UNIT.finditer(src):
        v = EN_WORDS.get(m.group(1).lower(), 1)
        u = hedge_check.EN_UNIT[m.group(2).lower()]
        out.append(("%", float(v)) if u == "%" else ("n", v * u))
    return out


def source_numbers(src):
    """자막의 숫자 전부. 영어 자막(billion · percent)과 한국어 자막(억 · 조) 둘 다 읽는다"""
    src = TIMESTAMP.sub(" ", src or "")
    return (_vals(src, hedge_check.EN_NUM, hedge_check.EN_UNIT, keep_bare=True)
            + _vals(src, hedge_check.KO_NUM, hedge_check.KO_UNIT, keep_bare=True) + _word_vals(src))


def _near(kind, v, have, n=3):
    """줄을 뺄 때 로그용: 자막에서 같은 종류로 가장 가까운 숫자 몇 개"""
    near = sorted({s for k, s in have if k == kind and s}, key=lambda s: abs(s - v))[:n]
    return ", ".join(f"{s:g}{'%' if kind == '%' else ''}" for s in near) or "없음"


def num_problem(ko, have):
    """한국어 줄의 숫자가 자막 숫자(have)에 없으면 "그 숫자 (자막 가까운 숫자: …)", 괜찮으면 None (news_store._num_problem)"""
    for kind, v in _vals(ko, hedge_check.KO_NUM, hedge_check.KO_UNIT, skip_dates=True):
        if not any(k == kind and abs(v - s) <= max(abs(s), 1e-9) * 0.02 for k, s in have):
            return f"{v:g}{'%' if kind == '%' else ''}, 자막 가까운 숫자: {_near(kind, v, have)}"
    return None


def check_lines(data, src, log=print):
    """issues[].lines를 자막과 대조해 고친다 (data를 바로 고침). → (뺀 줄 수, 말투 바로잡은 줄 수)"""
    have, dropped, fixed = source_numbers(src), 0, 0
    for it in data.get("issues") or []:
        out = []
        for ln in it.get("lines") or []:
            bad = num_problem(ln, have)
            if bad:
                dropped += 1
                log(f"요약 줄 뺌 (자막에 없는 숫자 {bad}): {ln}")   # bad에 자막 가까운 숫자도 들어 있음
                continue
            new, why = hedge_check.hedge(ln, src)
            if why:
                if len(new) > MAX_LINE_CHARS:   # 70자 안: 말투 표시는 남기고 앞 내용을 줄인다
                    tag = FWD_TAG if new.endswith(FWD_TAG) else ""
                    new = _trim(new[:len(new) - len(tag)], MAX_LINE_CHARS - len(tag)) + tag
                fixed += 1
            out.append(new)
        it["lines"] = out
    if dropped or fixed:
        log(f"말투 바로잡음 {fixed}줄 · 숫자 안 맞아 뺀 줄 {dropped}줄")
    return dropped, fixed


def summarize(title, channel, text, log=print):
    """자막 원문 → 구조화 요약 dict. 실패하면 SummarizeError."""
    body = text[:MAX_CHARS]
    content = f"{PROMPT}\n\n제목: {title}\n채널: {channel}\n\n자막:\n{body}"
    try:
        data = _normalize(llm.generate_json(content, log=log, what="요약"))
    except llm.LLMError as e:
        raise SummarizeError(str(e)) from e
    check_lines(data, body, log=log)
    return data


TITLE_PROMPT = """아래 유튜브 경제 · 금융 영상 제목들을 한국어로 번역합니다.
한국 뉴스 기사 제목처럼 자연스럽게, 고유명사 · 숫자는 살립니다. 이미 한국어인 제목은 그대로 둡니다.
설명 없이 JSON 객체 하나로만 답합니다: {"번호": "번역", ...}

"""


def translate_titles(titles, log=print):
    """{영상ID: 제목} → {영상ID: 한국어 제목}. 요약 전에 titleKo가 없던 것을 채울 때 쓴다 (한 번 호출에 여러 개)."""
    ids = list(titles)
    content = TITLE_PROMPT + "\n".join(f"{n}. {titles[vid]}" for n, vid in enumerate(ids, 1))
    try:
        raw = llm.generate_json(content, log=log, what="제목 번역")
    except llm.LLMError as e:
        raise SummarizeError(str(e)) from e
    if not isinstance(raw, dict):
        raise SummarizeError("Gemini 답이 정해진 틀(JSON 객체)이 아님")
    out = {}
    for n, vid in enumerate(ids, 1):
        ko = " ".join(str(raw.get(str(n)) or "").split())[:200]
        if ko:
            out[vid] = ko
    return out


# ---------- 명령줄 시험 (trial 자막 파일) ----------

ACTION_KO = {"buy": "매수", "sell": "매도", "hold": "보유", "watch": "관심"}


def pick_line(p):
    """종목 의견 한 줄 (시험 출력용)"""
    price = f" 목표 {p['targetPrice']:,} {p['currency']}" if p["targetPrice"] else ""
    at = f" [{p['atSec'] // 60}:{p['atSec'] % 60:02d}]" if p["atSec"] is not None else ""
    src = " (리포트)" if p["source"] == "report" else ""
    ticker = f" {p['ticker']}" if p["ticker"] else ""
    return f"* {ACTION_KO[p['action']]} {p['name']}{ticker}{price} - {p['speaker'] or '?'}{src}{at}: {p['reason']}"

def _parse_file(path):
    """trial.py가 쓴 파일: 머리글(제목/채널/주소/게시/자막) + 빈 줄 + 본문."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    head, _, body = raw.partition("\n\n")
    meta = dict(re.findall(r"^(\S+): (.*)$", head, re.M))
    return meta, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="trial-out", help="trial 자막 파일 폴더")
    ap.add_argument("--max", type=int, default=50)
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.dir, "**", "*.txt"), recursive=True))[:args.max]
    if not files:
        raise SystemExit(f"{args.dir} 안에 자막 파일이 없습니다.")
    out = []
    for n, path in enumerate(files, 1):
        meta, body = _parse_file(path)
        title, channel = meta.get("제목", os.path.basename(path)), meta.get("채널", "")
        print(f"\n[{n}/{len(files)}] {title[:60]} ({len(body):,}자)", flush=True)
        try:
            data = summarize(title, channel, body)
        except SummarizeError as e:
            print(f"실패: {title[:50]} - {e}")
            out.append({"file": os.path.basename(path), "title": title, "error": str(e)})
            continue
        print(f"\n■ [{channel}] {title[:70]}")
        print(f"  {data['summary']}")
        for i in data["issues"]:
            print(f"  - {i['category']:<16} {i['stance']:<8} {i['surprise']:<9} {i['scope']:<7} {i['country']:<3} "
                  f"{i['horizon']:<7} [{i['topic']}] {','.join(i['tickers'])}  {i['note'][:50]}")
            for ln in i["lines"]:
                print(f"      · {ln}")
        print(f"  종목: {', '.join(data['tickers'])}  섹터: {', '.join(data['sectors'])}  키워드: {', '.join(data['keywords'])}")
        for p in data["picks"]:
            print("  " + pick_line(p))
        out.append({"file": os.path.basename(path), "title": title, "channel": channel,
                    "url": meta.get("주소"), "published": meta.get("게시"), **data})
    with open(os.path.join(args.dir, "summaries.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    ok = sum(1 for o in out if "error" not in o)
    model = config.NVIDIA_MODEL if llm.provider() == "nvidia" else config.GEMINI_TEXT_MODEL
    print(f"\n요약 {ok}/{len(out)}개 성공 ({llm.provider()} · {model}) → {os.path.join(args.dir, 'summaries.json')}")


if __name__ == "__main__":
    main()

"""주제 걸러내기. 자막을 받기 전에 제목 · 설명만 보고 블로그 글감과 관계없는 영상을 뺀다.

유튜브 페이지 요청 없이 YouTube Data API로 받은 제목 · 설명과 Gemini 텍스트 판정만 쓴다.
판정에 실패하면 영상을 빼지 않는다 (필요한 영상을 잃지 않는 쪽으로).
"""
import json

import llm

PROMPT = """아래는 유튜브 경제 · 금융 채널의 영상 목록입니다.
이 영상들의 자막은 주식 · 투자 블로그(매일 종목 리포트)의 글감으로만 씁니다.
영상마다 글감으로 쓸 만한지 판정해 주세요.

쓸 만함 (keep=true):
- 주식시장 · 지수 · 개별 종목 · 기업 실적 · 산업 동향
- 금리 · 연준 · 물가 · 고용 등 경제지표와 경기 전망
- 채권 · 환율 · 원자재 · 암호화폐 등 시장
- 관세 · 무역 · 규제 · 정책처럼 시장이나 기업에 영향을 주는 정치 · 지정학 뉴스
- 투자자 · 애널리스트 · 경영자 인터뷰

쓸모없음 (keep=false):
- 시장 영향이 드러나지 않는 일반 정치 · 선거 · 사건사고 · 범죄 · 재난
- 스포츠 · 연예 · 생활 · 여행 · 음식 · 건강
- 채널 홍보 · 예고편 · 짧은 인사

애매하면 keep=true로 합니다.

JSON 배열로만 답합니다: [{"id": "영상 ID", "keep": true, "why": "10자 안팎 이유"}]

영상 목록:
"""


last_error = None  # 마지막 판정 실패 이유 (로그용)


def classify(videos):
    """videos: {영상 ID: {"title", "description"}} → {영상 ID: (keep, why)}. 실패하면 빈 dict, 이유는 last_error."""
    global last_error
    last_error = None
    if not videos:
        return {}
    items = [{"id": vid, "title": v.get("title", ""), "description": (v.get("description") or "")[:300]}
             for vid, v in videos.items()]
    try:
        rows = llm.generate_json(PROMPT + json.dumps(items, ensure_ascii=False), what="걸러내기")
        if isinstance(rows, dict):  # 모델이 {"videos": [...]}처럼 감싸서 줄 때
            rows = next((v for v in rows.values() if isinstance(v, list)), [])
        out = {}
        for r in rows or []:
            if isinstance(r, dict) and r.get("id") in videos:
                out[r["id"]] = (bool(r.get("keep", True)), str(r.get("why", ""))[:40])
        if not out:
            last_error = f"판정 결과가 비어 있음: {str(rows)[:150]}"
        return out
    except (llm.LLMError, ValueError, TypeError, AttributeError) as e:
        last_error = f"{type(e).__name__}: {str(e)[:200]}"
        return {}

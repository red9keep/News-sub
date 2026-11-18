"""수집기 설정. 값은 모두 환경 변수(GitHub Secrets / Variables)에서 읽는다."""
import os
from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))


def edition_day(now):
    """수집 묶음 날짜 (한국 시간 datetime → "YYYY-MM-DD"). 낮 12시에 바뀐다: 전날 12:00 ~ 오늘 11:59가 오늘 아침 글 한 묶음.

    상한 · 미룬 날(deferDay)과 summaries.edition이 이 날짜를 쓴다.
    """
    return (now + timedelta(hours=12)).strftime("%Y-%m-%d")

# 비밀값 (GitHub Secrets)
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")  # 서비스 계정 JSON 문자열
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 시황 해설 예비 키 (11-17): 기본 키가 한도 · 오류로 안 될 때만 쓴다. 한도는 프로젝트마다 따로라 다른 구글 프로젝트의 키여야 뜻이 있다
GEMINI_API_KEY_BACKUP = os.environ.get("GEMINI_API_KEY_BACKUP", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

# 요약 · 주제 걸러내기에 쓰는 텍스트 모델 제공자: gemini(기본) 또는 nvidia (worker/llm.py)
LLM_PROVIDER = (os.environ.get("LLM_PROVIDER") or "gemini").lower()
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL") or "z-ai/glm-5.3"

# 일반 설정 (GitHub Variables, 비어 있으면 기본값)
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET") or "ytb-v1-b96d5.firebasestorage.app"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"  # 받아쓰기(유튜브 주소 직접 읽기)
# 받아쓰기 예비 모델 (11-18). 첫 모델이 429 · 계속된 503이면 차례로 넘어간다. 모델마다 한도 · 혼잡이 따로라 하나가 막혀도 다른 것이 된다.
# 11-18 시험 (trial run 44219020694, 증시각도기 17분): 4-hydrogen 96초 끝 17:01 · 3.8-flash 482초 끝 17:15 모두 정상.
# 같은 시각 3.6-flash · 4-argon · 4-carbon은 첫 호출부터 429, 3-flash-preview는 404. 쉼표로 구분, "off"면 예비 없음
GEMINI_MODEL_FALLBACKS = [m.strip() for m in (os.environ.get("GEMINI_MODEL_FALLBACKS") or "gemini-4-hydrogen,gemini-3.8-flash").split(",")
                          if m.strip() and m.strip().lower() != "off"]
# 요약 · 주제 걸러내기(텍스트만) 모델. 비우면 GEMINI_MODEL과 같다.
# 10-21 시험: gemini-3.8-flash는 무료 한도(하루)가 금방 차고 503이 잦았다. gemini-3.5-flash-lite는 10개를 1분에 모두 성공
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL") or GEMINI_MODEL
# 요약 · 걸러내기 예비 모델 (11-16). 첫 모델이 404 · 429(하루 한도) · 계속된 5xx로 실패하면 이 모델로 한 번 더.
# gemini-3.1-flash-lite는 3.5 lite와 하루 한도를 따로 세고 품질이 같은 급 (블로그 비교, doc/블로그-AI-모델-안내.md). "off"면 예비 없음
GEMINI_TEXT_FALLBACK = os.environ.get("GEMINI_TEXT_FALLBACK") or "gemini-3.1-flash-lite"
MAX_DURATION_MIN = int(os.environ.get("MAX_DURATION_MIN") or 60)  # Gemini가 한 번에 받아쓰는 길이. 넘으면 나눠 받는다
SEGMENT_MIN = 30      # 나눠 받아쓸 때 한 구간 길이 (10-29)
MAX_LONG_MIN = 180    # 자막 없는 영상을 나눠서라도 받아쓰는 최대 길이. 넘으면 건너뜀 (30분 6번 = Gemini 6회)

# 유튜브 자막 받기 (yt-dlp)
#   home   : 집 공유기 VPN을 거칠 때만 받는다 (GitHub Actions 기본값)
#   direct : 지금 PC의 회선으로 바로 받는다 (집 PC에서 시험할 때)
#   off    : 자막을 받지 않고 모두 Gemini로 받아쓴다
CAPTIONS_MODE = (os.environ.get("CAPTIONS_MODE") or "home").lower()
# VPN 단계가 집 IP로 나가는 것을 확인한 뒤 넘겨주는 VPN 쪽 주소. yt-dlp 요청을 이 주소에 묶는다
YT_SOURCE_ADDRESS = os.environ.get("YT_SOURCE_ADDRESS", "")
# yt-dlp가 흉내 낼 유튜브 앱(player_client)과 건너뛸 단계(player_skip), 쉼표로 여러 개. 비우면 yt-dlp 기본값
# (10-26: 기본은 앱 3개(tv_samsung · visionos · web)를 모두 불러 영상당 요청이 6번. 자막만 필요하므로 줄이는 시험 중)
YT_PLAYER_CLIENT = [c for c in (os.environ.get("YT_PLAYER_CLIENT") or "").replace(" ", "").split(",") if c]
YT_PLAYER_SKIP = [c for c in (os.environ.get("YT_PLAYER_SKIP") or "").replace(" ", "").split(",") if c]
CAPTION_DELAY_SEC = (5, 15)          # 유튜브 요청 사이 무작위 대기
BLOCK_COOLDOWN_HOURS = (2, 6, 24)    # 막힌 횟수(연속)별 쉬는 시간. 세 번째부터는 24시간 (그사이 STALE_HOURS 지난 영상은 건너뜀)
# 받는 자막(영상) 수 상한. 넘친 영상은 대기(pending)로 두고 다음 실행 · 다음 날 이어서 받는다
MAX_CAPTIONS_PER_RUN = int(os.environ.get("MAX_CAPTIONS_PER_RUN") or 10)  # 실행 한 번에
MAX_CAPTIONS_PER_DAY = int(os.environ.get("MAX_CAPTIONS_PER_DAY") or 60)  # 묶음 하루에, 기본 묶음(미국 채널)
# 채널 limitGroup별 하루 상한 (10-25: 한국 채널을 미국 채널 60개와 따로 센다). limitGroup이 없는 채널은 위 기본 묶음
# 한국 20 → 30 (11-16, 한국 채널 13개로 늘어 하루 약 17 ~ 18개 예상)
GROUP_DAY_LIMITS = {"kr": int(os.environ.get("MAX_CAPTIONS_PER_DAY_KR") or 30)}
# 채널별 하루 몫 (10-27): 한 채널이 묶음 상한을 몰아 쓰지 않게, 묶음 상한 ÷ 묶음 안 채널 수(올림)까지만 받는다.
# 채널 문서 dayMax(숫자)가 있으면 그 값. 묶음의 마지막 예약 시각에서 이만큼(분) 지나면 몫을 풀어 남은 상한을 대기 영상에 준다
SHARE_RELEASE_AFTER_MIN = 40
# 필수 채널(channels.required=true)은 상한 · 주제 걸러내기를 적용하지 않고 맨 먼저 받는다.
# 그 밖의 채널은 자막 받기 전에 제목 · 설명으로 글감과 관계없는 영상을 뺀다 (off면 끔)
TOPIC_FILTER = (os.environ.get("TOPIC_FILTER") or "on").lower() != "off"
# 자막을 받은 뒤 Gemini로 영상별 구조화 요약(summaries)을 만든다 (off면 끔). 실패해도 자막 저장은 유지되고 다음 실행에서 다시 시도
SUMMARIZE = (os.environ.get("SUMMARIZE") or "on").lower() != "off"
MAX_SUMMARY_RETRIES_PER_RUN = 10  # 밀린 요약을 한 실행에서 다시 시도하는 수

# 실행 방식: schedule(cron) 또는 workflow_dispatch(수동/웹 요청)
RUN_TRIGGER = os.environ.get("RUN_TRIGGER") or "local"

# 재시도 규칙
ATTEMPTS_PER_RUN = 3       # 한 실행 안에서 영상당 최대 시도 횟수
MAX_TOTAL_ATTEMPTS = 3     # 누적 실패가 이만큼이면 자동 재시도 중단 (웹 재수집으로만 재시도). 10-29: 5 → 3
# 올라온 지 이만큼 지난 영상은 대기 · 실패여도 자동으로 다시 받지 않고 건너뜀 (뉴스라 늦으면 정보 가치가 없음).
# 웹 재수집은 예외
STALE_HOURS = 24
TRANSCRIBE_TIMEOUT_MIN = 10  # Gemini 받아쓰기 한 번의 응답 대기 상한. 넘기면 재시도 없이 실패로 두고 다음 실행에서 다시
LIVE_SETTLE_MIN = 30       # 라이브가 끝난 뒤 이만큼은 영상 변환 · 자동 자막 생성을 기다린다 (대기로 둠, 실패 아님)

# 새로 등록된 채널(lastRunAt 없음)은 이 시간 안에 지난 예약 시각만 수집 대상으로 본다
NEW_CHANNEL_LOOKBACK = timedelta(minutes=65)

# 로그 보관 기간 (Firestore TTL 정책이 expireAt 필드를 기준으로 삭제)
LOG_RETENTION = timedelta(days=30)

STORAGE_PREFIX = "ai_ready_texts"

# 필수 채널 빠짐 알림(notify.required_missing)에서 볼 요일 (월=0). 채널 문서에 weekdays가 있으면 그것이 먼저, 없으면 여기, 그다음 월 ~ 금.
# Mad Money는 미국 저녁 방송이라 한국 시간 다음 날 아침(화 ~ 토)에 올라온다 (10-21 ~ 11-04 기록)
REQUIRED_WEEKDAYS = {"CNBC Mad Money": [1, 2, 3, 4, 5]}

"""오류 원인과 대처를 한국어로. 로그 · 웹에는 "원인 → 대처" 한국어만 보이고, 영어 원문은 로그 줄을 펼친 detail에만 남긴다.

유튜브(yt-dlp) · Gemini · Firestore가 주는 영어 오류 문구를 아래 표로 맞춰 본다. 표마다 (찾을 문구, 원인, 대처).
대처는 수집기가 실제로 하는 일을 적는다 ("직접 할 일:"은 사람이 해야 풀리는 것).
표에 없는 오류는 "원인 미확인"으로 나오므로, 그런 줄이 로그에 보이면 원문을 보고 한 줄 더한다 (doc/수집-방식.md 3-1).
"""
import config

RETRY = (f"다음 실행에서 다시 (모두 {config.MAX_TOTAL_ATTEMPTS}회까지, 올라온 지 {config.STALE_HOURS}시간 지나면 건너뜀. "
         "그 뒤엔 웹 재수집으로만)")
STALE = (f"올라온 지 {config.STALE_HOURS}시간이 지나 정보 가치가 없음 → 대처: 건너뜀, 다시 시도하지 않음 "
         "(필요하면 웹 재수집)")
SKIP = "건너뜀, 다시 시도하지 않음"

# 다시 해도 받을 수 없는 영상 (yt-dlp). 수집기는 실패 대신 건너뜀으로 둔다.
# 위에서부터 맞춰 본다 ("removed by the uploader"가 "Video unavailable"과 같이 오므로)
PERMANENT = (
    ("removed by the uploader", "업로더가 삭제한 영상", SKIP),
    ("removed for violating", "유튜브 정책 위반으로 삭제된 영상", SKIP),
    ("account associated with this video has been terminated", "채널이 없어진 영상", SKIP),
    ("not made this video available in your country", "국가 제한: 한국에서 볼 수 없는 영상",
     f"{SKIP} (수집은 집 IP(한국)로만 하므로 다른 방법 없음)"),
    ("private video", "비공개 영상", f"{SKIP} (공개로 바뀌면 웹 재수집)"),
    ("members-only", "채널 멤버십 전용 영상", f"{SKIP} (멤버십 로그인이 필요해 수집기로는 못 받음)"),
    ("join this channel to get access", "채널 멤버십 전용 영상", f"{SKIP} (멤버십 로그인이 필요해 수집기로는 못 받음)"),
    ("confirm your age", "연령 제한 영상", f"{SKIP} (로그인이 필요해 수집기로는 못 받음)"),
)

BLOCKED = (f"자막 받기를 2 · 6 · 24시간(연속 횟수대로) 쉬고 남은 영상은 대기 → 쉬는 시간 뒤 다시. "
           f"그사이 올라온 지 {config.STALE_HOURS}시간 지난 영상은 건너뜀")
GOOGLE_SIDE ="잠시 뒤 다시, 안 되면 다음 실행에서 다시 (구글 쪽 문제라 기다리는 것 말고 방법 없음)"

# 그 밖의 오류. (찾을 문구들, 원인, 대처). 문구는 소문자. 위에서부터
KNOWN = (
    # 우리 코드가 한국어로 만든 오류 (원문이 따로 없는 것)
    (("정해진 틀(json 객체)이 아님", "요약 문장이 없음"), "Gemini 답이 정해진 틀이 아님", "다음 실행에서 다시 요약 (자막은 저장돼 있음)"),
    (("gemini가 빈 답",), "Gemini가 빈 답을 줌", RETRY),
    (("json3 형식 자막이 없",), "유튜브 자막 파일 형식이 바뀜", f"{RETRY}. 계속되면 수집기 코드 확인 필요"),
    # 유튜브
    (("not a bot",), "유튜브가 봇 확인을 요구함 (요청이 많아 막힘)", BLOCKED),
    (("http error 429", "too many requests"), "유튜브 요청 제한 (429)", BLOCKED),
    (("video unavailable",), "유튜브가 볼 수 없는 영상이라고만 답함 (이유 미공개)", f"{RETRY}. 계속되면 삭제 · 제한된 영상"),
    (("this live event will begin", "premieres in"), "아직 시작하지 않은 라이브 · 최초 공개", RETRY),
    (("unable to download webpage", "unable to download api page"), "유튜브 페이지를 받지 못함 (연결 문제)", RETRY),
    (("quotaexceeded", "dailylimitexceeded"), "유튜브 API 하루 할당량 초과",
     "새 영상 확인이 멈춤 → 할당량이 풀리는 한국 시간 오후 4시(서머타임 끝나면 5시) 뒤 실행부터 자동으로 다시"),
    # Gemini
    (("prepayment credits are depleted",), "Gemini 선불 크레딧 소진 (402)",
     "직접 할 일: AI Studio에서 결제 확인. 그때까지 받아쓰기 · 요약 안 됨 (자막 있는 영상은 계속 받음)"),
    (("api key not valid", "api_key_invalid"), "Gemini 키가 잘못됨", "직접 할 일: GitHub Secret GEMINI_API_KEY 확인"),
    (("permission_denied", "403 forbidden"), "Gemini 키 권한 없음 (403)", "직접 할 일: GitHub Secret GEMINI_API_KEY · 키의 프로젝트 확인"),
    (("exceeded your current quota", "resource_exhausted", "'code': 429", "429 too many"), "Gemini 요청 한도 초과 (429, 무료 등급)",
     "1분 기다려 다시, 또 막히면 하루 한도로 보고 남은 영상은 다음 실행으로 (하루 한도는 한국 시간 오후 4 · 5시에 풀림)"),
    (("input token count", "exceeds the maximum number of tokens"), "영상이 길어 Gemini 입력 한도를 넘음",
     f"{config.SEGMENT_MIN}분씩 나눠 다시 받아씀"),
    (("mediaresolution is not supported",), "이 Gemini 모델은 영상 받아쓰기 설정을 지원하지 않음",
     "직접 할 일: 받아쓰기 모델(변수 GEMINI_MODEL) 바꾸기"),
    (("is not found for api version", "404 not_found"), "Gemini 모델 이름이 없음 (404)",
     "직접 할 일: trial llm-models로 지금 있는 모델 이름을 보고 변수 GEMINI_MODEL · GEMINI_TEXT_MODEL 고치기"),
    (("'code': 410", "410 gone"), "Gemini · NVIDIA 모델이 종료됨 (410)",
     "직접 할 일: trial llm-models로 새 모델 이름을 보고 변수 고치기"),
    (("503", "unavailable", "overloaded", "high demand"), "Gemini 서버 혼잡 (503)",
     GOOGLE_SIDE),
    (("504", "deadline_exceeded", "deadline exceeded"), "서버 응답 시간 초과 (504)", GOOGLE_SIDE),
    (("500 internal", "'code': 500", "internal error"), "Gemini 서버 내부 오류 (500)", GOOGLE_SIDE),
    # 연결 · 응답 형식
    (("server disconnected", "connection reset", "connection aborted", "remotedisconnected"), "서버가 응답 없이 연결을 끊음",
     "잠시 뒤 다시, 안 되면 다음 실행에서 다시"),
    (("timed out", "timeout"), "응답 시간 초과", RETRY),
    (("expecting value", "json이 없음", "jsondecodeerror"), "응답이 비었거나 정해진 틀(JSON)이 아님",
     "한 번 더 묻고, 안 되면 다음 실행에서 다시"),
)

UNKNOWN = ("원인 미확인 (원문은 로그 줄을 펼쳐 보기)", f"{RETRY}. 계속되면 원문을 보고 표에 대처 추가")


def text(err):
    """오류와 그 원인(__cause__)들의 원문을 이어 붙인 것"""
    out, seen = [], set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        out.append(f"{type(err).__name__}: {err}")
        err = err.__cause__ or err.__context__
    return "\n".join(out)


def _match(err):
    """(원인, 대처). 오류와 원인 사슬 전체에서 찾는다"""
    msg = (err if isinstance(err, str) else text(err)).lower()
    for sign, cause, action in PERMANENT:
        if sign in msg:
            return cause, action
    for signs, cause, action in KNOWN:
        if any(s in msg for s in signs):
            return cause, action
    return UNKNOWN


def permanent(err):
    """다시 해도 받을 수 없는 영상이면 한국어 원인, 아니면 None"""
    msg = str(err).lower()
    return next((cause for sign, cause, _ in PERMANENT if sign in msg), None)


def explain(err):
    """오류 → 한국어 원인 한 줄"""
    return _match(err)[0]


def advice(err):
    """오류 → 대처 한 줄 (수집기가 하는 일, 또는 '직접 할 일')"""
    return _match(err)[1]


def describe(err):
    """로그 · 웹에 보일 '무엇이 잘못됐나 → 대처'. 수집기가 만든 한국어 메시지는 그대로, 영어 원문뿐이면 원인으로 바꾼다"""
    msg = str(err)
    if not any("가" <= ch <= "힣" for ch in msg):
        msg = explain(err)
    return f"{msg} → 대처: {advice(err)}"

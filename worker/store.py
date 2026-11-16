"""Firestore · Storage 읽기쓰기."""
import json
import os
import re
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter

import config
import llm
import summarize

_db = None


def init():
    """서비스 계정으로 Firebase에 연결한다.

    GitHub Actions에서는 FIREBASE_SERVICE_ACCOUNT(JSON 문자열)를,
    로컬에서는 GOOGLE_APPLICATION_CREDENTIALS(키 파일 경로)를 쓴다.
    """
    global _db
    if _db is not None:
        return _db
    if config.FIREBASE_SERVICE_ACCOUNT:
        cred = credentials.Certificate(json.loads(config.FIREBASE_SERVICE_ACCOUNT))
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    else:
        raise SystemExit("Firebase 인증 정보가 없습니다. FIREBASE_SERVICE_ACCOUNT 또는 "
                         "GOOGLE_APPLICATION_CREDENTIALS를 설정하세요.")
    firebase_admin.initialize_app(cred, {"storageBucket": config.STORAGE_BUCKET})
    _db = firestore.client()
    return _db


def now_utc():
    return datetime.now(timezone.utc)


def sanitize_name(name):
    """파일 이름에 쓸 수 없는 문자와 URL을 제거한다 (v1과 같은 규칙)."""
    clean = re.sub(r"https?://\S+", "", name or "")
    return "".join(c for c in clean if c not in '\\/:*?"<>|').strip()[:150]


# ---------- 실행 기록과 로그 ----------

class Run:
    """runs/{id} 문서와 그 아래 logs 하위 컬렉션."""

    def __init__(self, trigger):
        self.ref = _db.collection("runs").document()
        self.counts = {"done": 0, "skipped": 0, "failed": 0, "pending": 0}
        self.ref.set({
            "trigger": trigger,
            "status": "running",
            "startedAt": firestore.SERVER_TIMESTAMP,
            "counts": self.counts,
        })

    def log(self, message, level="info", detail=None):
        """detail: 오류 원문 전체 (웹 로그에서 펼쳐 본다). message에는 앞부분만 넣는다"""
        print(f"[{level}] {message}", flush=True)
        entry = {
            "level": level,  # info / success / skip / error
            "message": message,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "expireAt": now_utc() + config.LOG_RETENTION,
        }
        if detail:
            entry["detail"] = str(detail)[:4000]
        self.ref.collection("logs").add(entry)

    def count(self, key):
        """counts: 이번 실행에서 영상 상태를 바꾼 수. 이미 처리했거나 이번 실행에서 다룬 영상은 세지 않는다"""
        self.counts[key] += 1

    def finish(self, status="done", note=None, **extra):
        """extra: 웹 실행 기록에 보일 값 (유튜브 요청 수 등)"""
        data = {"status": status, "finishedAt": firestore.SERVER_TIMESTAMP, "counts": self.counts, **extra}
        if note:
            data["note"] = note
        self.ref.update(data)


# ---------- 채널 ----------

def enabled_channels():
    docs = _db.collection("channels").where(filter=FieldFilter("enabled", "==", True)).stream()
    return [(d.id, d.to_dict()) for d in docs]


def update_channel(channel_id, **fields):
    _db.collection("channels").document(channel_id).update(fields)


# ---------- 요청(jobs) ----------

def take_pending_jobs():
    """대기 중인 요청을 모두 가져오고 처리 중으로 바꾼다."""
    jobs = []
    for d in _db.collection("jobs").where(filter=FieldFilter("status", "==", "pending")).stream():
        d.reference.update({"status": "running", "startedAt": firestore.SERVER_TIMESTAMP})
        jobs.append((d.id, d.to_dict()))
    return jobs


def has_pending_jobs():
    """대기 중인 요청이 있는지만 본다 (처리 중으로 바꾸지 않음)."""
    return any(True for _ in _db.collection("jobs").where(filter=FieldFilter("status", "==", "pending")).limit(1).stream())


def finish_jobs(job_ids):
    for jid in job_ids:
        _db.collection("jobs").document(jid).update(
            {"status": "done", "finishedAt": firestore.SERVER_TIMESTAMP})


# ---------- 영상 ----------

def get_video(video_id):
    snap = _db.collection("videos").document(video_id).get()
    return snap.to_dict() if snap.exists else None


def set_video(video_id, **fields):
    fields["updatedAt"] = firestore.SERVER_TIMESTAMP
    _db.collection("videos").document(video_id).set(fields, merge=True)


def pending_video_ids():
    docs = _db.collection("videos").where(filter=FieldFilter("status", "==", "pending")).stream()
    return [d.id for d in docs]


def save_transcript(video_id, channel_name, title, text, header, source, language=None):
    """결과를 Storage 파일(v1과 같은 경로)과 Firestore(웹 표시용) 두 곳에 저장한다.

    source: captions-manual / captions-auto / gemini
    """
    date_str = datetime.now(config.KST).strftime("%Y-%m-%d")
    path = f"{config.STORAGE_PREFIX}/{date_str}/[{sanitize_name(channel_name)}] {sanitize_name(title)}.txt"
    body = header + "\n\n" + text
    blob = storage.bucket().blob(path)
    blob.upload_from_string(body.encode("utf-8"), content_type="text/plain; charset=utf-8")
    _db.collection("transcripts").document(video_id).set({
        "text": body,
        "storagePath": path,
        "source": source,
        "language": language,
        "createdAt": firestore.SERVER_TIMESTAMP,
    })
    return path


def get_transcript_text(video_id):
    """저장된 자막 본문 (머리글 포함). 없으면 None."""
    snap = _db.collection("transcripts").document(video_id).get()
    return snap.to_dict().get("text") if snap.exists else None


# ---------- 영상별 구조화 요약 (summaries) ----------

def save_summary(video_id, data, meta):
    """summaries/{영상ID}. 외부(다른 Google 계정)가 하루치 종합에 읽어 가는 표.

    meta: channelName · title · url · publishedAt · durationSec (videos 문서와 같은 값)
    date: 수집한 한국 날짜 (달력 날짜. Storage 폴더 날짜와 같음)
    edition: 수집 묶음 날짜 (낮 12시에 바뀜). 블로그 아침 글은 이 날짜로 모은다
    """
    now = datetime.now(config.KST)
    day = {"date": now.strftime("%Y-%m-%d"), "edition": config.edition_day(now)}
    _db.collection("summaries").document(video_id).set({
        **meta, **data, **day,
        "model": llm.last_model(),  # 예비 모델로 받았으면 그 이름 (11-16)
        "createdAt": firestore.SERVER_TIMESTAMP,
    })
    save_picks(video_id, data.get("picks") or [], {**meta, **day, "titleKo": data.get("titleKo", "")})


def save_picks(video_id, picks, info):
    """picks/{영상ID}_{번호}: 종목 의견 한 건 = 문서 한 개 (블로그 '추천 종목' 코너 · 종목별 조회용).

    summaries.picks와 같은 내용에 영상 정보를 붙여 펼친 것. 다시 요약하면 그 영상의 이전 의견을 지우고 새로 쓴다.
    key: 같은 종목을 묶는 값 (티커, 없으면 띄어쓰기를 뺀 종목 이름)
    """
    col = _db.collection("picks")
    batch = _db.batch()
    for d in col.where(filter=FieldFilter("videoId", "==", video_id)).stream():
        batch.delete(d.reference)
    keep = ("channelId", "channelName", "title", "titleKo", "url", "publishedAt", "date", "edition")
    for n, p in enumerate(picks, 1):
        batch.set(col.document(f"{video_id}_{n:02d}"), {
            **p, "key": summarize.pick_key(p), "videoId": video_id,
            **{k: info.get(k) for k in keep},
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
    batch.commit()


def summaries_without_title_ko(scan=300):
    """한국어 제목(titleKo)이 없는 최근 요약 [(영상ID, 제목)]. 다 채운 뒤에는 meta/backfill 표시로 더 읽지 않는다."""
    flag = _db.collection("meta").document("backfill")
    snap = flag.get()
    if snap.exists and snap.to_dict().get("titleKoDone"):
        return []
    docs = _db.collection("summaries").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(scan).stream()
    out = [(d.id, d.get("title") or "") for d in docs if not d.to_dict().get("titleKo")]
    if not out:
        flag.set({"titleKoDone": True}, merge=True)
    return out


def set_summary_fields(video_id, **fields):
    _db.collection("summaries").document(video_id).update(fields)


def pending_summary_ids(limit):
    """자막은 받았지만 요약이 아직 안 된 영상 (summaryStatus == pending)."""
    docs = _db.collection("videos").where(filter=FieldFilter("summaryStatus", "==", "pending")).limit(limit).stream()
    return [d.id for d in docs]


# ---------- 유튜브 차단 상태 ----------

def youtube_state():
    """meta/youtube: blockedUntil(쉬는 시간 끝), blockStreak(연속으로 막힌 횟수), lastBlockAt."""
    snap = _db.collection("meta").document("youtube").get()
    return snap.to_dict() if snap.exists else {}


def set_youtube_state(**fields):
    _db.collection("meta").document("youtube").set(fields, merge=True)


def set_youtube_counts(**fields):
    """하루 수 · 상한 맵을 통째로 바꾼다 (merge=True는 맵을 키별로 합쳐 전날 채널 키가 남는다)."""
    _db.collection("meta").document("youtube").set(fields, merge=list(fields))


# ---------- Slack 알림: 같은 알림은 하루 한 번 (notify.py) ----------

def alert_day(key):
    """meta/alerts에 적어 둔, 이 알림을 마지막으로 보낸 한국 날짜 (없으면 None)"""
    snap = _db.collection("meta").document("alerts").get()
    return (snap.to_dict() or {}).get(key) if snap.exists else None


def set_alert_day(key, day):
    _db.collection("meta").document("alerts").set({key: day}, merge=True)

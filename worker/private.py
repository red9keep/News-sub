"""공개 로그에 본문을 남기지 않는다 (11-18).

저장소가 공개라 GitHub Actions 실행 로그 · Artifacts는 누구나 본다. 요약 · 자막 · 시험 글처럼 본문이 든 것은
로그에 찍지 않고 비공개 Firestore `trials`에 저장해 웹(설정 > 시험 결과)에서 본다.

    trials/{id}              kind · title · chars · parts · runId · createdAt · expireAt  (목록용, 본문 없음)
    trials/{id}/parts/{번호}  text  (문서 하나 1MB 제한이라 나눠 담는다)
    Storage trial/{실행 번호}/  자막 파일처럼 다음 시험이 다시 읽는 파일 (브라우저에서는 못 읽음)

- Actions에서는 건수 · 글자 수만 찍는다.
- 집 PC에서 돌리면 전처럼 화면에도 찍는다 (공개 로그가 아니다).
- 보관은 RETENTION. 저장할 때 기한 지난 것을 지운다.
"""
import os
from datetime import timedelta

import config

ON_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_ID = os.environ.get("GITHUB_RUN_ID") or "local"
RETENTION = timedelta(days=14)
PART_CHARS = 200_000   # 한글 3바이트 × 20만 자 = 600KB
STORAGE_PREFIX = "trial"


def can_store():
    return bool(config.FIREBASE_SERVICE_ACCOUNT or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def _chunks(text):
    """PART_CHARS 안에서 줄 끝에 맞춰 자른다"""
    out = []
    while len(text) > PART_CHARS:
        cut = text.rfind("\n", 0, PART_CHARS) + 1 or PART_CHARS
        out.append(text[:cut])
        text = text[cut:]
    out.append(text)
    return out


class Report:
    """시험 결과 한 건. 본문이 든 줄을 모아 trials에 저장한다.

    echo: 집 PC에서 line()을 화면에도 찍을지 (자막 원문처럼 긴 것은 False)
    """

    def __init__(self, kind, title, echo=True):
        self.kind, self.title, self.echo, self.lines = kind, title, echo, []

    def line(self, text=""):
        """본문 한 줄 (또는 여러 줄). Actions 로그에는 찍지 않는다"""
        self.lines.append(str(text))
        if self.echo and not ON_ACTIONS:
            print(text, flush=True)

    def log(self, msg, *secret):
        """log 함수 자리에 넣는다. msg는 공개해도 되는 말, secret은 본문이 든 부분.
        Actions 로그에는 secret을 글자 수로 바꿔 찍고, 전체 줄은 시험 결과에 남긴다. (log=print면 둘 다 그대로 찍힌다)"""
        full = " ".join([str(msg), *map(str, secret)])
        self.lines.append(full)
        if ON_ACTIONS and secret:
            print(f"{msg} ({sum(len(str(s)) for s in secret)}자)", flush=True)
        else:
            print(full, flush=True)

    def save(self):
        """trials에 저장하고 문서 ID를 돌려준다. 저장할 것 · 저장할 곳이 없으면 None"""
        text = "\n".join(self.lines).strip()
        if not text:
            return None
        if not can_store():
            if ON_ACTIONS:
                print(f"시험 결과를 저장하지 못함: Firebase 인증 정보 없음 ({self.title}, {len(text):,}자)", flush=True)
            return None
        import store
        from firebase_admin import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        db = store.init()
        now = store.now_utc()
        col = db.collection("trials")
        for old in col.where(filter=FieldFilter("expireAt", "<", now)).stream():
            for p in old.reference.collection("parts").stream():
                p.reference.delete()
            old.reference.delete()
        parts = _chunks(text)
        ref = col.document()
        for n, part in enumerate(parts, 1):
            ref.collection("parts").document(f"{n:04d}").set({"text": part})
        # 목록 문서는 본문을 다 쓴 뒤에 (웹이 반쯤 쓴 결과를 열지 않게)
        ref.set({"kind": self.kind, "title": self.title, "chars": len(text), "parts": len(parts), "runId": RUN_ID,
                 "createdAt": firestore.SERVER_TIMESTAMP, "expireAt": now + RETENTION})
        print(f"시험 결과 저장: {self.title} ({len(text):,}자) → 웹 설정 > 시험 결과", flush=True)
        return ref.id


# ---------- Storage: 다음 시험이 다시 읽는 파일 ----------

def _bucket():
    import store
    from firebase_admin import storage
    store.init()
    return storage.bucket()


def upload_dir(local, run_id=RUN_ID):
    """폴더 안 파일을 trial/{실행 번호}/ 아래에 올린다. 기한 지난 지난 실행 파일은 지운다. → 올린 파일 수"""
    import store
    bucket = _bucket()
    limit = store.now_utc() - RETENTION
    for blob in bucket.list_blobs(prefix=f"{STORAGE_PREFIX}/"):
        if blob.time_created and blob.time_created < limit:
            blob.delete()
    n = 0
    for root, _, files in os.walk(local):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, local).replace(os.sep, "/")
            bucket.blob(f"{STORAGE_PREFIX}/{run_id}/{rel}").upload_from_filename(path)
            n += 1
    return n


def download_dir(run_id, local):
    """trial/{실행 번호}/ 아래 파일을 폴더로 받는다. → 받은 파일 수"""
    prefix = f"{STORAGE_PREFIX}/{run_id}/"
    n = 0
    for blob in _bucket().list_blobs(prefix=prefix):
        path = os.path.join(local, *blob.name[len(prefix):].split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blob.download_to_filename(path)
        n += 1
    return n

"""Storage 이번 달 요청 수를 보고, 무료 몫에 가까워지면 Slack으로 알린다 (11-16, usage.yml이 하루 한 번).

버킷 요청은 Cloud Monitoring storage.googleapis.com/api/request_count로 센다 (수집기 · 블로그 모두 포함).
- 쓰기(Class A: 올리기 · 목록 등) 무료 월 5천 → WRITE_ALERT(기본 4천) 넘으면 알림
- 읽기(Class B: 받기 · 정보 보기) 무료 월 5만 → READ_ALERT(기본 4만) 넘으면 알림
같은 알림은 한 달에 한 번 (Firestore meta/alerts에 보낸 달을 적음). 달은 UTC 기준(요금 달은 미국 태평양 시간이라 몇 시간 차이).
권한: 수집기 서비스 계정의 Firebase Admin 역할에 monitoring.timeSeries.list가 들어 있다.

    python worker/usage.py          # 세고, 넘었으면 알림
    python worker/usage.py --print  # 세기만
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import config
import notify
import store

WRITE_ALERT = int(os.environ.get("STORAGE_WRITE_ALERT") or 4000)
READ_ALERT = int(os.environ.get("STORAGE_READ_ALERT") or 40000)
WRITE_FREE, READ_FREE = 5000, 50000

# Class B (읽기). 나머지 메서드는 Class A(쓰기 · 목록 · 권한 바꾸기 등)로 센다 — 모르는 메서드는 비싼 쪽으로
READ_METHODS = {"ReadObject", "GetObjectMetadata", "GetBucketMetadata", "GetIamPolicy",
                "GetObjectIamPolicy", "GetBucketIamPolicy", "TestIamPermissions", "GetNotification",
                "GetObjectAccessControl", "GetBucketAccessControl", "GetDefaultObjectAccessControl"}


def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    scopes = ["https://www.googleapis.com/auth/monitoring.read"]
    if config.FIREBASE_SERVICE_ACCOUNT:
        info = json.loads(config.FIREBASE_SERVICE_ACCOUNT)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
                                                                      scopes=scopes)
    else:
        raise SystemExit("인증 정보가 없습니다 (FIREBASE_SERVICE_ACCOUNT 또는 GOOGLE_APPLICATION_CREDENTIALS)")
    return AuthorizedSession(creds), creds.project_id


def month_counts(session, project, bucket, now=None):
    """이번 달(UTC 1일 0시부터) 메서드별 요청 수 {메서드: 수}"""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    secs = max(60, int((now - start).total_seconds()))
    params = {
        "filter": f'metric.type="storage.googleapis.com/api/request_count" AND resource.label.bucket_name="{bucket}"',
        "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{secs}s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": "metric.label.method",
    }
    out = {}
    url = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
    while True:
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for ts in body.get("timeSeries", []):
            m = ts["metric"].get("labels", {}).get("method", "?")
            out[m] = out.get(m, 0) + sum(int(p["value"].get("int64Value", 0)) for p in ts.get("points", []))
        if not body.get("nextPageToken"):
            return out
        params["pageToken"] = body["nextPageToken"]


def split(counts):
    reads = sum(n for m, n in counts.items() if m in READ_METHODS)
    return sum(counts.values()) - reads, reads


def _sent_this_month(key, month):
    try:
        return store.alert_day(key) == month
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Storage 이번 달 요청 수 확인 · 알림")
    ap.add_argument("--print", action="store_true", help="세기만 하고 알림은 보내지 않음")
    args = ap.parse_args()

    session, project = _session()
    now = datetime.now(timezone.utc)
    counts = month_counts(session, project, config.STORAGE_BUCKET, now)
    writes, reads = split(counts)
    month = now.strftime("%Y-%m")
    print(f"{month} Storage 요청 (UTC {now:%m-%d %H:%M}까지): 쓰기 {writes:,} / 무료 {WRITE_FREE:,}, "
          f"읽기 {reads:,} / 무료 {READ_FREE:,}")
    for m, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {m}: {n:,} ({'읽기' if m in READ_METHODS else '쓰기'})")
    if args.print:
        return

    store.init()
    checks = [("storage-write", writes, WRITE_ALERT, WRITE_FREE, "쓰기(올리기 · 목록)"),
              ("storage-read", reads, READ_ALERT, READ_FREE, "읽기(받기)")]
    for key, n, alert, free, what in checks:
        if n < alert:
            continue
        if _sent_this_month(key, month):
            print(f"이번 달 이미 보낸 알림이라 건너뜀: {key}")
            continue
        over = "무료 몫을 넘음, 넘는 만큼 요금" if n >= free else f"무료 {free:,}까지 {free - n:,} 남음"
        if notify.send([f"Storage {what} 요청이 이번 달 {n:,}번 — {over} ({month}, 알림 기준 {alert:,})"]):
            store.set_alert_day(key, month)


if __name__ == "__main__":
    sys.exit(main())

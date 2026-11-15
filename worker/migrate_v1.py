"""v1 데이터(saved_urls.txt, downloaded.txt)를 Firestore로 한 번 옮긴다.

사용법 (로컬 PC에서):
    set GOOGLE_APPLICATION_CREDENTIALS=새_서비스계정_키.json
    python worker/migrate_v1.py            # 미리보기만
    python worker/migrate_v1.py --apply    # 실제로 저장

기본 위치는 v1이 쓰던 문서\\StockInsightPro 폴더다. 다른 곳이면 --dir로 지정한다.
"""
import argparse
import os

from firebase_admin import firestore

import store

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "StockInsightPro")


def read_lines(path):
    for enc in ("utf-8", "cp949"):
        try:
            with open(path, encoding=enc) as f:
                return [l.strip() for l in f if l.strip()]
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            return []
    return []


def parse_channels(lines):
    """'이름 | URL | 개수 | 시각1, 시각2' 형식."""
    out = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            print(f"  형식이 맞지 않아 건너뜀: {line}")
            continue
        name, url, count, times = parts
        out.append({
            "name": name.strip("[]").strip() or name,
            "url": url,
            "count": max(1, min(20, int(count))),
            "times": [t.strip() for t in times.split(",") if t.strip()],
            "enabled": True,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--apply", action="store_true", help="실제로 Firestore에 저장")
    args = ap.parse_args()

    channels = parse_channels(read_lines(os.path.join(args.dir, "saved_urls.txt")))
    video_ids = sorted(set(read_lines(os.path.join(args.dir, "downloaded.txt"))))

    print(f"채널 {len(channels)}개, 처리 기록 {len(video_ids)}개")
    for c in channels:
        print(f"  - {c['name']} | {c['url']} | {c['count']}개 | {', '.join(c['times'])}")
    if not args.apply:
        print("\n미리보기입니다. 저장하려면 --apply를 붙여 다시 실행하세요.")
        return

    db = store.init()
    existing_urls = {d.to_dict().get("url") for d in db.collection("channels").stream()}
    for c in channels:
        if c["url"] in existing_urls:
            print(f"  이미 있음: {c['name']}")
            continue
        db.collection("channels").add({**c, "createdAt": firestore.SERVER_TIMESTAMP, "migrated": True})

    # 처리 기록은 '완료'로만 표시한다 (제목 등은 v1에 남아 있지 않음)
    for i in range(0, len(video_ids), 400):
        batch = db.batch()
        for vid in video_ids[i:i + 400]:
            batch.set(db.collection("videos").document(vid),
                      {"status": "done", "migrated": True}, merge=True)
        batch.commit()
    print("이전 완료")


if __name__ == "__main__":
    main()

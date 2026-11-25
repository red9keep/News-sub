# ytb-web

등록한 유튜브 채널의 새 영상을 정해진 시각에 받아 요약하고 Firebase에 저장하는 개인용 수집기와 관리 웹.

| 경로 | 내용 |
| --- | --- |
| `worker/` | 수집기 (Python) |
| `web/` | 관리 웹 (Firebase Hosting) |
| `functions/` | 웹에서 수집을 거는 함수 |
| `cron-trigger/` | 정시 실행용 Cloudflare Worker |
| `.github/workflows/` | 수집 · 시험 · 배포 |

설정 · 운영 문서는 비공개 저장소에 따로 둔다.

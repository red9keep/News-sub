# cron-trigger

GitHub Actions 예약 실행(cron)은 정시를 보장하지 않는다. 2026-10-22에는 하루 내내 한 번도 돌지 않았다.
이 Cloudflare Worker가 정해진 한국 시각에 `collect.yml`을 `workflow_dispatch`(`scheduled=true`)로 건다.
`scheduled=true`인 실행은 예약 실행과 똑같다: 예약 시각이 지난 채널만 받고(`collect_all` 무시), 로그에 "예약"으로 남는다.

방법은 AlphaScope `infra/cron-trigger`와 같다 (`D:\Alpha-Scope.blogspot\doc\운영\정시-실행-방법.md`). Worker는 따로 둔다.

| 회차 (Firestore 채널 예약) | 실제 실행 | 대상 | 횟수 |
| --- | --- | --- | --- |
| 07:30 ~ 11:30, 30분마다 | 회차 뒤 3 ~ 27분 중 무작위 | 필수 채널 (당잠사 · 증시각도기 · Mad Money) | 9 |
| 17:00 ~ 20:00, 매시 | 회차 뒤 3 ~ 50분 중 무작위 | 한국 채널 (매경TV 뉴스 · 삼성증권 · MTN 퀵브리핑 · MTN 투자뉴스7 · 미래에셋 RE포트 · 애널리스트리포트) | 4 |
| 21:20 ~ 다음 날 07:20, 매시 | 회차 뒤 5 ~ 55분 중 무작위. 02 ~ 05시 회차는 각각 절반쯤 건너뜀 | 미국 일반 채널 (블룸버그 · CNBC · 폭스 · WSJ) | 9 ~ 11 |

- 매번 같은 분에 접속하는 기계적인 모양을 없애려고 회차마다 분을 무작위로 고른다 (10-23). 무작위 값은 날짜 · 회차로 계산해 저장할 것이 없다. Worker는 1분마다 깨어나 "지금이 그 분인가"만 본다 (`due()`).
- 실제로 어느 채널을 받을지는 수집기가 Firestore 채널 예약(`times`)과 `lastRunAt`으로 정한다. Worker는 "지금 돌아라"만 건다. 일반 채널끼리의 순서와 대기 영상 순서도 수집기가 실행마다 섞는다.
- `collect.yml`의 `schedule`(GitHub cron)은 예비로 하루 두 번(06:47 · 11:50)만 둔다. 둘 다 돌면 `concurrency`로 줄을 서고, 뒤 실행은 "할 일 확인"에서 VPN 없이 바로 끝난다.
- 한국 시간은 서머타임이 없어 UTC와 분이 같다. 깨어남 격자는 `crons = ["* * * * *"]` 하나 (무료 플랜 한도 계정당 5개).
- 며칠치 실행 시각 미리 보기: `plan(한국 날짜 0시를 분으로)`이 회차별 시각 · 건너뜀을 돌려준다.

## 배포 (사용자)

1. GitHub fine-grained token 발급 (`red9keep` 계정): Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Repository access: **Only select repositories → `red9keep/News-sub`**
   - Permissions → Repository permissions: **Actions: Read and write** 하나만
   - 만료일을 정했다면 그 날짜를 작업기록에 적어 둔다 (만료되면 Worker가 401로 실패한다)
2. 배포 (Cloudflare 무료 계정):

   ```
   cd cron-trigger
   npx wrangler login
   npx wrangler secret put GITHUB_TOKEN   # 1의 토큰 붙여넣기 (화면 · 문서 · 채팅에 남기지 않는다)
   npx wrangler deploy
   ```

처음 쓰는 Cloudflare 계정이면 두 가지에서 막힌다 (10-22 겪음):
- `You need to verify your email address` (10034): 대시보드에서 이메일 인증 후 `secret put`부터 다시
- `You need a workers.dev subdomain` (10063, cron만 등록 안 됨): 대시보드 Workers & Pages 화면을 한 번 열면 생긴다. 그 뒤 `deploy` 다시. 성공하면 `schedule: 5,25,35 * * * *`가 찍힌다

`collect.yml`에 `scheduled` 입력이 들어간 커밋이 main에 올라간 뒤 배포한다. 그 전에는 GitHub가 "Unexpected inputs"(422)로 거부한다.

## 확인

- Cloudflare 대시보드 → Workers → `yutb-collect-trigger` → Logs / Cron Events
- `gh run list -R red9keep/News-sub -w collect` 에서 `workflow_dispatch` 실행이 정시(5분 안쪽)에 생기는지
- 로컬 시험: `npx wrangler dev --test-scheduled` 후 `curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"`
  — 지금 시각이 표에 있으면 **실제로 수집이 걸린다** (없으면 아무것도 안 함)

## 시각을 바꿀 때

함께 맞춘다: ① `src/index.js`의 `SLOTS` (base = 채널 예약 시각) ② `collect.yml`의 예비 cron ③ Firestore 채널 `times`. 바꾼 뒤 `npx wrangler deploy`.

시황 해설(`commentary.yml`, 11-16)은 `src/index.js`의 `COMMENTARY`에서 정시(08:10 am · 20:30 pm KST)에 건다. 시각을 바꾸면 `commentary.yml`의 예비 cron과 `worker/commentary.py`의 마감(`SLOTS`)도 같이 본다. 바꾼 뒤 `npx wrangler deploy`.

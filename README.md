# ytb-web (유튜브 스마트 비서 웹 버전)

등록한 유튜브 채널의 새 영상을 정해진 시각에 받아써서 Firebase에 저장하고, 웹에서 관리·조회한다.
설계는 `doc/ytb-web-design.md`, 진행 상황은 `doc/작업기록.md` 참고.

```
GitHub Actions (예약)                         Firebase
  worker/collector.py ── YouTube Data API (새 영상 확인)
         │            ├─ yt-dlp 자막 ── 집 공유기 VPN ── 유튜브   (1순위)
         │            └─ Gemini API (자막 없을 때만 받아쓰기)     (2순위)
         │                                     Hosting   ← web/ (채널 관리 · 결과 · 로그)
         │                                     Firestore ← channels · videos · transcripts · jobs · runs · meta
         └──────────────────────────────────▶  Storage   ← ai_ready_texts/날짜/[채널] 제목.txt
웹 "지금 수집" → jobs 문서 → Cloud Function(functions/) → GitHub Actions 즉시 실행
```

유튜브 자막은 클라우드 IP에서 받으면 차단되므로, 집 공유기(ipTIME) L2TP VPN을 거쳐 집 IP로 받는다.
yt-dlp 요청만 VPN 주소에 묶기 때문에 VPN이 끊겨도 클라우드 IP로 새지 않는다.

## 폴더 구성

| 경로 | 내용 |
| --- | --- |
| `worker/` | 수집기 (Python). `collector.py`가 진입점 |
| `worker/captions.py` | 유튜브 자막 받기 (yt-dlp, 자막 파일만). 요청 간격 · 차단 감지 |
| `worker/transcribe.py` | Gemini 받아쓰기. 자막이 없는 영상에만 쓴다 |
| `worker/trial.py` | 하루치 자막 시험 수집 (Firebase · API 키 불필요). 채널 목록은 `trial_channels.txt` |
| `worker/llm.py` | 요약 · 걸러내기의 텍스트 모델 호출 (Gemini 또는 NVIDIA Build, 재시도 · 호출 간격) |
| `worker/summarize.py` | 영상별 구조화 요약 (Gemini). 이슈 분류표 `CATEGORIES`는 여기. 설계는 `doc/데이터-설계.md` |
| `worker/topic.py` | 자막 받기 전 제목 · 설명으로 글감과 무관한 영상 걸러내기 (Gemini) |
| `worker/count_new.py` | 하루 새 영상 수 · 걸러내기 미리 보기 (공식 API만, trial `count`) |
| `scripts/vpn-up.sh` | GitHub Actions에서 집 공유기 VPN 접속 · 집 IP 확인 |
| `worker/migrate_v1.py` | v1 데이터(`saved_urls.txt`, `downloaded.txt`) 1회 이전 |
| `web/` | 웹 화면 (빌드 없음, Firebase Hosting) |
| `web/ui-kit.css` | `external-ui-kit`에서 **고치지 않고** 복사한 파일. 키트가 바뀌면 다시 복사만 한다 |
| `web/app.css` | 앱 전용 스타일. 색 · 글자 · 여백은 `--ui-` 변수만 쓴다 |
| `functions/` | 웹의 "지금 수집" 요청을 GitHub Actions 실행으로 바꾸는 함수 |
| `firestore.rules`, `storage.rules` | 보안 규칙. `__OWNER_UID__`는 배포할 때 자동으로 바뀜 |
| `.github/workflows/collect.yml` | 예약·수동 수집 |
| `.github/workflows/trial.yml` | 수동 시험: VPN 접속만(`vpn-only`) 또는 하루치 자막 받기(`captions`) |
| `.github/workflows/deploy.yml` | main에 push하면 웹·규칙 배포 |

## 처음 설정 (한 번만)

### 0. 노출된 키 정리
- [ ] Google Cloud 콘솔 → IAM → 서비스 계정 → 기존 `firebase_key.json`의 키 ID를 찾아 **삭제**
- [ ] 유튜브 로그아웃 (기존 `cookies.txt` 세션 종료)
- [ ] GitHub의 `ytb-v3` 저장소를 private으로 바꾸거나 보관(Archive)

### 1. Google Cloud / Firebase 콘솔 (프로젝트 `ytb-v1-b96d5`)
1. **Authentication** → 로그인 방법 → **Google** 사용 설정
2. **Firestore Database**가 없으면 생성 (위치는 `asia-northeast3` 서울 권장)
3. Firestore → **TTL 정책** 추가: 컬렉션 그룹 `logs`, 필드 `expireAt` (로그 30일 뒤 자동 삭제)
4. Google Cloud 콘솔 → API 및 서비스 → **YouTube Data API v3 사용 설정** → 사용자 인증 정보 → **API 키 생성** → 키 제한: YouTube Data API v3만
5. [Google AI Studio](https://aistudio.google.com/apikey)에서 **Gemini API 키** 발급
6. 서비스 계정 → **새 키(JSON) 발급**. 역할: `Firebase 관리자`, `서비스 사용량 소비자`, `서비스 사용량 뷰어`
   - 역할은 IAM 화면에서 이 서비스 계정 줄의 연필(주 구성원 수정)로 추가한다. 뷰어가 없으면 deploy가 `serviceusage ... 403`으로 멈춘다
   - 이 파일은 저장소 폴더 밖에 보관한다

### 2. GitHub 저장소
1. GitHub에서 **public** 저장소 생성 (`red9keep/News-sub`, Actions 무료 무제한. 문서 `doc/`는 비공개 `red9keep/News-doc`에 따로)
2. 이 폴더를 push
   ```
   git add -A
   git commit -m "ytb-web 초기 구현"
   git remote add origin https://github.com/red9keep/News-sub.git
   git push -u origin main
   ```
3. Settings → Secrets and variables → Actions
   - **Secrets**
     | 이름 | 값 |
     | --- | --- |
     | `FIREBASE_SERVICE_ACCOUNT` | 1-6에서 받은 JSON 파일 내용 전체 |
     | `GEMINI_API_KEY` | 1-5의 키 |
     | `YOUTUBE_API_KEY` | 1-4의 키 |
     | `NVIDIA_API_KEY` | (선택) NVIDIA Build 키. `LLM_PROVIDER=nvidia`일 때 요약 · 걸러내기에 씀 |
     | `VPN_SERVER` | 공유기 ipDISK 주소 전체 (`○○○.iptime.org`) |
     | `VPN_PSK` | 공유기 L2TP 서버 비밀키 (큰따옴표 `"` 는 쓰지 않는다) |
     | `VPN_USER` | 공유기 VPN 접속 계정 |
     | `VPN_PASSWORD` | 공유기 VPN 접속 암호 |
     | `SLACK_WEBHOOK_URL` | (선택) 망가졌을 때만 보내는 Slack 알림 주소 (Incoming Webhook, AlphaScope 알림과 같은 채널). 없으면 보내지 않음. `worker/notify.py` |
   - **Variables**
     | 이름 | 값 | 필수 |
     | --- | --- | --- |
     | `OWNER_UID` | 처음엔 `none`, 3단계에서 실제 값으로 변경 | 필수 |
     | `GEMINI_MODEL` | 받아쓰기 모델. 비우면 `gemini-3.8-flash` | 선택 |
     | `GEMINI_MODEL_FALLBACKS` | 받아쓰기 예비 모델 (쉼표로 구분, 차례대로). 첫 모델이 429 · 계속된 503 · 404면 다음 모델로. 비우면 `gemini-4-hydrogen,gemini-3.8-flash`, `off`면 예비 없음 | 선택 |
     | `GEMINI_TEXT_MODEL` | 요약 · 걸러내기 모델. 비우면 `GEMINI_MODEL`과 같음. 권장 `gemini-3.5-flash-lite` (한도 넉넉, 빠름) | 선택 |
     | `MAX_DURATION_MIN` | 비우면 `60` (Gemini 받아쓰기에만 적용) | 선택 |
     | `MAX_CAPTIONS_PER_RUN` | 비우면 `10`. 실행 한 번에 받는 자막(영상) 수 상한 | 선택 |
     | `MAX_CAPTIONS_PER_DAY` | 비우면 `60`, 지금 `57`. 하루에 받는 자막 수 상한 (기본 묶음 = 미국 채널. `limitGroup: kr` 채널은 따로 20개, 필수 채널은 상한 없음). 채널마다 하루 몫(묶음 상한 ÷ 채널 수, 채널 `dayMax`로 바꿈)이 있다 | 선택 |
     | `LLM_PROVIDER` | 비우면 `gemini`. `nvidia`면 요약 · 걸러내기를 NVIDIA Build로 (받아쓰기는 항상 Gemini) | 선택 |
     | `NVIDIA_MODEL` | 비우면 `z-ai/glm-5.3` | 선택 |
     | `TOPIC_FILTER` | 비우면 `on`. 일반 채널 영상을 제목 · 설명으로 걸러냄. `off`면 끔 | 선택 |
     | `SUMMARIZE` | 비우면 `on`. 자막 받은 뒤 영상별 구조화 요약(`summaries`) 생성. `off`면 끔 | 선택 |
     | `CAPTIONS_MODE` | 비우면 `home` (VPN 경유 자막). `off`면 자막 없이 모두 Gemini | 선택 |
     | `STORAGE_BUCKET` | 비우면 `ytb-v1-b96d5.firebasestorage.app` | 선택 |

### 3. 웹 배포와 소유자 등록
1. Actions → **deploy** → Run workflow
2. `https://ytb-v1-b96d5.web.app` 접속 → Google 로그인
3. "접근 권한이 없습니다" 화면에 나오는 **내 UID**를 복사
4. GitHub 변수 `OWNER_UID`에 그 값을 넣고 **deploy**를 다시 실행 → 새로고침하면 본 화면이 열림

### 4. "지금 수집" 함수 배포 (로컬 PC, 한 번만)
1. GitHub → Settings(계정) → Developer settings → **Fine-grained token** 생성
   - 저장소: `News-sub`만, 권한: **Actions: Read and write**
2. 로컬에서:
   ```
   npm install -g firebase-tools
   firebase login
   cd ytb-web
   echo GH_REPO=red9keep/News-sub> functions\.env
   firebase functions:secrets:set GH_DISPATCH_TOKEN     (1의 토큰 붙여넣기)
   python -m venv functions\venv
   functions\venv\Scripts\pip install -r functions\requirements.txt
   firebase deploy --only functions
   ```
   - 함수를 쓰지 않으려면 이 단계를 건너뛰고, GitHub Actions 화면의 **collect → Run workflow**로 직접 실행해도 된다

### 5. v1 데이터 이전 (로컬 PC, 한 번만)
```
pip install -r worker/requirements.txt
set GOOGLE_APPLICATION_CREDENTIALS=<1-6의 JSON 경로>
python worker/migrate_v1.py            # 미리보기
python worker/migrate_v1.py --apply    # 저장
```
채널 7개와 처리 기록이 옮겨진다. 이미 처리한 영상은 다시 받아쓰지 않는다.

### 6. 시험 실행
0. 새 영상 수 세기 (유튜브 요청 없음): Actions → **trial** → mode `count` → 로그에서 예약 시각별 새 영상 · 대기 · 놓침 확인
1. 집 공유기 VPN: Actions → **trial** → mode `vpn-only` → 단계 [1/6]~[6/6]이 모두 통과하는지 확인
   - [4/6]에서 실패: 공유기 IPSec 설정 · 비밀키 / [5/6]: VPN 계정 · 암호 / [2/6]·[6/6]: DDNS 주소
2. 하루치 자막 (Firebase 설정 전에도 가능): **trial** → mode `captions` → 끝나면 실행 화면 아래 Artifacts의 `trial-captions` 내려받기
3. 운영 수집: **collect** → Run workflow (모든 채널 수집) → 웹의 로그 탭에서 진행 확인

집 PC에서 VPN 없이 자막 시험만 할 때:
```
pip install -r worker/requirements.txt
set CAPTIONS_MODE=direct
python worker/trial.py
```

## 유튜브 차단 대비

- 새 영상 확인은 YouTube Data API(차단 없음), yt-dlp는 새 영상의 자막을 받을 때만 쓴다
- 유튜브 요청 사이 5~15초 무작위 대기, 한 번에 하나씩, 계정 쿠키는 쓰지 않는다
- 받는 자막 수 상한: 실행당 10개, 하루 60개(임시). 넘친 영상은 대기로 두고 다음 실행에서 대기 영상부터 받는다
- 봇 확인 · 429 같은 차단 신호가 오면 그 실행의 자막 받기를 멈추고 남은 영상은 대기로 둔다
- 쉬는 시간: 연속 1번째 2시간, 2번째 6시간, 3번째부터 24시간 (Firestore `meta/youtube`). 자막을 한 번 받으면 연속 횟수가 0으로 돌아간다
- 집 IP가 막히면 그동안 집에서 유튜브를 볼 때도 확인 화면이 뜰 수 있다

## 운영

- 예약 수집: 한국 시간 07:35 ~ 11:35 30분마다(필수 채널) + 21:25 ~ 다음 날 07:25 매시(일반 채널, 미국장 시간). 하루 상한은 낮 12시에 바뀌는 날짜로 센다. 채널의 예약 시각이 지난 뒤 첫 실행에서 처리된다
- 채널 문서에 `dailyLimit`(숫자)이 있으면 오늘 게시된 영상을 그만큼 받은 뒤 그날 남은 예약을 건너뛴다
- 실행 시간대를 바꾸려면 `collect.yml`의 cron을 고친다 (UTC 기준, 한국 시간 - 9시간)
- 실패한 영상은 다음 실행에서 자동 재시도하고, 누적 5회 실패하면 웹의 재수집 버튼으로만 다시 시도한다
- Gemini 일일 한도에 걸리면 남은 영상은 대기 상태로 두고 다음 실행에서 이어서 처리한다
- VPN 연결이 안 되거나 유튜브 차단으로 쉬는 중이면 새 영상은 대기로 두고, 풀린 뒤 실행에서 이어서 처리한다 (Gemini로 넘기지 않는다)

## 웹 화면 로컬 미리보기 (가짜 데이터)
```
python preview/serve.py
```
`http://localhost:8000` 접속 (`?login`을 붙이면 로그인된 상태로 시작). 실제 Firebase에 연결하지 않으며,
"지금 전체 수집"과 재수집 버튼은 수집 과정을 흉내 낸다. 새로고침하면 처음 상태로 돌아간다.

## 로컬에서 수집기 실행 (개발용)
```
set GOOGLE_APPLICATION_CREDENTIALS=<서비스 계정 JSON 경로>
set GEMINI_API_KEY=...
set YOUTUBE_API_KEY=...
set COLLECT_ALL=true
cd worker
python collector.py
```

// 한국 시각 표에 따라 collect.yml을 workflow_dispatch(scheduled=true)로 건다.
// GitHub cron은 정시를 보장하지 않는다 (2026-10-22 하루 0회). collect.yml의 schedule은 예비로 몇 번만 남아 있다.
// 같은 시각에 둘 다 돌아도 concurrency로 줄을 서고, 뒤 실행은 "할 일 확인"에서 바로 끝난다.
//
// 매번 같은 분(分)에 접속하지 않도록 회차마다 실행 시각을 무작위로 고른다 (10-23 사용자 결정).
// 무작위 값은 날짜 · 회차로 계산하므로 저장할 것이 없고, 1분마다 깨어나 "지금이 그 분인가"만 본다.

const REPO = "red9keep/News-sub";
const WORKFLOW = "collect.yml";

const DAY_MIN = 24 * 60;

// 회차 표. base는 Firestore 채널 예약(times) 시각, [lo, hi]는 그 뒤 몇 분 안에서 고를지
//   필수 채널 (당잠사 · 증시각도기 · Mad Money): 07:30 ~ 11:30 30분마다, 예약 뒤 3 ~ 27분
//   일반 채널 (블룸버그 · CNBC · 폭스 · WSJ):     21:20 ~ 다음 날 07:20 매시, 예약 뒤 5 ~ 55분
//     새벽 02 ~ 05시 회차는 날마다 절반쯤 건너뛴다 (밤새 매시간 접속하는 모양을 없앤다)
//   한국 채널 (매경 · 삼성증권 · MTN · 미래에셋):  17:00 ~ 20:00 매시, 예약 뒤 3 ~ 50분 (10-25 추가)
//     장 마감 뒤 낮에 올라온 영상을 받는다. 미국 채널 밤 회차와 겹치지 않게 21:20 전에 끝낸다
const SLOTS = [
  ...[0, 1, 2, 3, 4, 5, 6, 7, 8].map((k) => ({ base: 7 * 60 + 30 + 30 * k, lo: 3, hi: 27, skip: 0 })),
  ...[17, 18, 19, 20].map((h) => ({ base: h * 60, lo: 3, hi: 50, skip: 0 })),
  ...[21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7].map((h) => ({
    base: h * 60 + 20, lo: 5, hi: 55, skip: h >= 2 && h <= 5 ? 0.5 : 0,
  })),
];

// 시황 해설 (commentary.yml, 11-16). 유튜브에 접속하지 않으므로 시각을 흩지 않고 정시에 건다.
//   am 08:10 (마감 08:30, 당잠사 07:35 회차 요약 뒤) · pm 20:30 (마감 20:40)
// 11-16 사용자 결정: 품질을 확인할 때까지 멈춤 (빈 표). 다시 켤 때 아래 두 줄을 되살리고 commentary.yml schedule도 같이
//   { at: 8 * 60 + 10, slot: "am" },
//   { at: 20 * 60 + 30, slot: "pm" },
const COMMENTARY = [];

// 문자열 → [0, 1) 고른 값 (FNV-1a + 섞기). 같은 문자열이면 늘 같은 값
function rand(key) {
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b) >>> 0;
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35) >>> 0;
  h = (h ^ (h >>> 16)) >>> 0;
  return h / 2 ** 32;
}

// 한국 날짜 dayStart(분 단위, 1970-01-01 KST 0시부터)의 회차들 → [{ at: 분, base, skipped }]
export function plan(dayStart) {
  const day = new Date(dayStart * 60000).toISOString().slice(0, 10);
  return SLOTS.map((s) => {
    const key = `${day}|${s.base}`;
    const at = dayStart + s.base + s.lo + Math.floor(rand(key) * (s.hi - s.lo + 1));
    return { at, base: s.base, skipped: rand(`${key}|skip`) < s.skip };
  });
}

// 지금(UTC Date)이 실행할 분인가. 23:20 회차는 자정을 넘을 수 있어 전날 표도 본다
export function due(date) {
  const now = Math.floor((date.getTime() + 9 * 3600 * 1000) / 60000); // 한국 시간, 분 단위
  const today = Math.floor(now / DAY_MIN) * DAY_MIN;
  return [today - DAY_MIN, today].some((d) => plan(d).some((s) => !s.skipped && s.at === now));
}

function hhmm(min) {
  const m = ((min % DAY_MIN) + DAY_MIN) % DAY_MIN;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

// 지금이 시황 해설을 걸 분이면 그 편 ("am" · "pm"), 아니면 null
export function commentaryDue(date) {
  const now = Math.floor((date.getTime() + 9 * 3600 * 1000) / 60000);
  const m = ((now % DAY_MIN) + DAY_MIN) % DAY_MIN;
  const hit = COMMENTARY.find((c) => c.at === m);
  return hit ? hit.slot : null;
}

async function dispatch(token, workflow = WORKFLOW, inputs = { scheduled: "true" }) {
  const res = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "yutb-collect-trigger",
    },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (res.status !== 204) throw new Error(`${workflow}: HTTP ${res.status} ${await res.text()}`);
}

export default {
  async scheduled(event, env) {
    const date = new Date(event.scheduledTime);
    const at = hhmm(Math.floor(date.getTime() / 60000) + 9 * 60);
    const slot = commentaryDue(date);
    if (slot) {
      // 실패해도 같은 분의 수집 실행은 건다 (오류는 로그에만)
      try {
        await dispatch(env.GITHUB_TOKEN, "commentary.yml", { scheduled: "true", slot });
        console.log(`dispatched commentary.yml (${slot}) at KST ${at}`);
      } catch (e) {
        console.error(String(e));
      }
    }
    if (!due(date)) return;
    await dispatch(env.GITHUB_TOKEN); // 실패하면 예외가 Cloudflare 로그(Cron Events)에 남는다
    console.log(`dispatched ${WORKFLOW} at KST ${at}`);
  },
};

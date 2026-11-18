// 미리보기 전용 가짜 Firebase. 실제 서버에 연결하지 않고 메모리에서만 동작한다.
// serve.py가 import map으로 gstatic의 Firebase 모듈 대신 이 파일을 쓰게 한다.

// ---------- 시간 ----------
class Ts {
  constructor(d) { this.d = d; }
  toDate() { return this.d; }
}
const ago = (min) => new Ts(new Date(Date.now() - min * 60000));
const SERVER_TS = Symbol("serverTimestamp");
export const serverTimestamp = () => SERVER_TS;

// ---------- 가짜 데이터 ----------
const store = new Map(); // 경로 → 데이터
const put = (path, data) => store.set(path, data);

put("channels/c1", { name: "CNBC Television", url: "https://www.youtube.com/@CNBCtelevision", count: 10, times: ["09:10", "10:10", "12:10", "14:10", "16:10", "17:10"], enabled: true, lastRunAt: ago(35) });
put("channels/c2", { name: "WSJ", url: "https://www.youtube.com/@wsj", count: 1, times: ["09:10", "10:10"], enabled: true, lastRunAt: ago(95) });
put("channels/c3", { name: "CNBC Mad Money", url: "https://www.youtube.com/playlist?list=PLxxxx", count: 1, times: ["09:10", "10:10", "12:10", "13:10"], enabled: true, lastRunAt: ago(150) });
put("channels/c4", { name: "당잠사", url: "https://www.youtube.com/playlist?list=PLyyyy", count: 1, times: ["07:30", "08:00"], enabled: true, required: true, lastRunAt: ago(60 * 26) });
// 탭 모두 보기 시험용 (탭이 화면 폭을 넘게)
["블룸버그 텔레비전", "폭스 비즈니스", "증시각도기", "매경TV 뉴스", "삼성증권", "MTN 퀵브리핑", "MTN 투자뉴스7", "미래에셋 RE포트", "미래에셋 애널리스트리포트"]
  .forEach((name, i) => put(`channels/k${i}`, {
    name, url: "https://www.youtube.com/@x", count: 3, times: ["17:00"], enabled: true,
    ...(i < 2 ? {} : i === 2 ? { required: true, dailyLimit: 1 } : { limitGroup: "kr", ...(i === 3 ? { dayMax: 6 } : {}) }),
  }));
put("channels/c3", { ...store.get("channels/c3"), required: true, dailyLimit: 1 });
put("channels/c4", { ...store.get("channels/c4"), dailyLimit: 1 });

const videos = [
  ["v1", "c1", "CNBC Television", "Markets open higher as tech rallies", "done", 34],
  ["v2", "c1", "CNBC Television", "Fed officials signal patience on rate cuts", "done", 40],
  ["v3", "c2", "WSJ", "How a Chip Shortage Turned Into a Glut", "done", 96],
  ["v4", "c1", "CNBC Television", "Live: Earnings season preview", "failed", 120, "Gemini 오류 400: Request contains an invalid argument."],
  ["v5", "c3", "CNBC Mad Money", "Mad Money 10/18/26 | Audio Only", "skipped", 150, "영상 길이 62분, 상한 60분 초과"],
  ["v6", "c1", "CNBC Television", "Oil prices slide on supply outlook", "done", 210],
  ["v7", "c2", "WSJ", "[비공개 영상]", "skipped", 240, "비공개 또는 삭제된 영상"],
  ["v8", "c1", "CNBC Television", "Midterm race tightens in Georgia", "skipped", 60, "주제 무관: 정치인 유세 중심의 일반 정치 뉴스"],
  ["v9", "c1", "CNBC Television", "Rapid Recap: How Wall Street is responding", "pending", 30],
  ["v10", "c1", "CNBC Television", "Yesterday clip", "done", 60 * 24],
  ["v11", "c3", "CNBC Mad Money", "Mad Money 10/25/26 | Audio Only", "failed", 60 * 23, "3회 시도 실패: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.'}}"],
];
for (const [id, channelId, channelName, title, status, min, error] of videos) {
  put(`videos/${id}`, { channelId, channelName, title, status, error: error || null, attempts: status === "failed" ? 2 : 1, updatedAt: ago(min), processedAt: status === "done" ? ago(min) : null, topicFiltered: error?.startsWith("주제 무관") || undefined });
  if (status === "done") {
    put(`transcripts/${id}`, { text: `제목: ${title}\n채널: ${channelName}\n주소: https://www.youtube.com/watch?v=${id}\n게시일: 2026-10-19T00:00:00Z\n\n[00:00] (미리보기용 예시 텍스트입니다)\n[00:04] Good morning, welcome to the show.\n[00:09] Stocks are moving higher this morning as investors digest the latest data.\n[00:17] Let's bring in our correspondent for more on what's driving the market.\n[00:25] ...` });
  }
}

// 영상별 요약 (v6은 요약 없음 → "텍스트 보기"만)
const summaries = {
  v1: {
    titleKo: "기술주 반등에 美 증시 상승 출발",
    summary: "기술주 강세로 미국 증시가 상승 출발했다. 엔비디아 실적 기대가 반도체 전반을 끌어올렸고, 국채 금리가 내려 성장주 부담이 줄었다. 진행자는 실적 시즌 첫 주가 방향을 정할 것으로 봤다.",
    issues: [
      { category: "AI_CAPEX", topic: "데이터센터 투자 기대", stance: "bullish", tickers: ["NVDA", "SMH"], note: "AI 서버 수요가 줄지 않았다는 신호가 반도체를 밀어 올렸다." },
      { category: "RATES_FED", topic: "국채 금리 하락", stance: "bullish", tickers: [], note: "10년물 금리 하락이 성장주에 숨통을 틔웠다." },
    ],
    tickers: ["NVDA", "SMH", "QQQ"], keywords: ["반도체", "금리", "실적 시즌"], speakers: ["Carl Quintanilla (CNBC)"],
    picks: [
      { name: "엔비디아", ticker: "NVDA", market: "US", action: "buy", targetPrice: 250, currency: "USD", speaker: "모건스탠리", source: "report", reason: "AI 서버 주문이 내년까지 차 있다며 목표주가를 올렸다.", atSec: 185 },
      { name: "마이크론", ticker: "MU", market: "US", action: "hold", targetPrice: null, currency: "", speaker: "Carl Quintanilla (CNBC)", source: "own", reason: "재고 조정이 끝날 때까지 기다리자고 했다.", atSec: 412 },
    ],
  },
  v2: {
    summary: "연준 인사들이 금리 인하를 서두르지 않겠다는 신호를 보냈다. 물가 둔화가 확인될 때까지 기다리겠다는 입장이다.",
    issues: [{ category: "RATES_FED", topic: "연준 12월 인하 기대 후퇴", stance: "bearish", tickers: [], note: "인하 시점이 늦춰질 수 있다는 발언이 이어졌다." }],
    tickers: [], keywords: ["연준", "금리 인하"], speakers: [],
  },
  v3: {
    summary: "반도체 부족이 1년 만에 공급 과잉으로 바뀐 과정을 다뤘다. 재고 조정이 끝나야 가격이 안정된다는 분석이다.",
    issues: [{ category: "GROWTH_JOBS", topic: "반도체 재고 조정", stance: "neutral", tickers: ["MU"], note: "재고가 줄어드는 속도가 관건이다." }],
    tickers: ["MU"], keywords: ["반도체", "재고"], speakers: [],
    picks: [
      { name: "엔비디아", ticker: "NVDA", market: "US", action: "buy", targetPrice: null, currency: "", speaker: "김 대표 (출연자)", source: "own", reason: "조정 때마다 나눠 사라고 했다.", atSec: 3723 },
      { name: "SK하이닉스", ticker: "", market: "KR", action: "buy", targetPrice: 320000, currency: "KRW", speaker: "김 대표 (출연자)", source: "own", reason: "HBM 점유율이 계속 오른다고 봤다.", atSec: 3800 },
    ],
  },
};
const EDITION = (d) => new Date(d.getTime() + 21 * 3600 * 1000).toISOString().slice(0, 10);   // 낮 12시 경계 (app.js currentEdition과 같음)
const V = Object.fromEntries(videos.map((v) => [v[0], v]));
for (const [id, s] of Object.entries(summaries)) {
  const [, channelId, channelName, title, , min] = V[id];
  // 오늘 묶음 = 보는 날의 한국 날짜 (app.js currentEdition)
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
  put(`summaries/${id}`, { ...s, channelId, channelName, title, url: `https://www.youtube.com/watch?v=${id}`, durationSec: 540, createdAt: ago(min), edition: today,
    publishedAt: new Date(Date.now() - (min + 90) * 60000).toISOString() });   // 유튜브 게시 시각 (ISO, 받기 전)
}
// 이전 날짜 보기 · 쪽 번호 시험용: CNBC 지난 12일치
for (let day = 1; day <= 12; day++) {
  const at = new Date(Date.now() - day * 86400000);
  for (let k = 0; k < (day % 3) + 1; k++) {
    const id = `p${day}_${k}`;
    put(`summaries/${id}`, {
      channelId: "c1", channelName: "CNBC Television", title: `Past clip ${day}-${k}`, url: `https://www.youtube.com/watch?v=${id}`, durationSec: 300,
      summary: `${day}일 전 영상 요약입니다. 쪽 번호 시험용 가짜 글입니다.`, issues: [{ category: "EARNINGS", topic: "실적 시험", stance: "neutral", tickers: ["AAPL"], note: "가짜 주장." }],
      tickers: ["AAPL"], keywords: ["실적"], speakers: [], createdAt: new Ts(at), edition: EDITION(at), publishedAt: new Date(at.getTime() - 3600000).toISOString(),
    });
    put(`transcripts/${id}`, { text: `제목: Past clip ${day}-${k}\n\n[00:00] (가짜 자막)` });
  }
}

// 수집기 상태 (수집 현황의 한도 · 몫)
put("meta/youtube", {
  dayKey: EDITION(new Date()), dayCount: 78, dayCounts: { main: 57, kr: 13, required: 8 }, dayLimits: { main: 57, kr: 20 },
  shares: { c1: 15, c2: 15, k0: 15, k1: 15, k3: 6, k4: 4, k5: 4, k6: 4, k7: 4, k8: 4 }, sharesReleased: ["kr"],
});

function seedRun(id, minAgo, trigger, status, counts, lines, extra = {}) {
  put(`runs/${id}`, { trigger, status, startedAt: ago(minAgo), finishedAt: ago(minAgo - 2), counts, ...extra });
  lines.forEach(([level, message, detail], i) => put(`runs/${id}/logs/l${i}`, { level, message, detail, createdAt: new Ts(new Date(Date.now() - minAgo * 60000 + i * 4000)) }));
}
seedRun("r1", 35, "schedule", "done", { done: 2, failed: 1, skipped: 1, pending: 1 }, [
  ["info", "작업 시작: 채널 1개"],
  ["info", "[CNBC Television] 최신 10개 확인"],
  ["skip", "[CNBC Television] 이미 처리됨: Oil prices slide on supply…"],
  ["info", "[CNBC Television] 받아쓰기 시작: Markets open higher as tech…"],
  ["success", "[CNBC Television] 완료: Markets open higher as tech…"],
  ["info", "Gemini 오류 400, 10초 후 재시도 (1/3)"],
  ["error", "[CNBC Television] 실패 (2회째): Live: Earnings season preview… - Gemini 오류 400: Request contains an inval",
    "영상 v4 (https://www.youtube.com/watch?v=v4)\nTranscribeError: Gemini 오류 400: Request contains an invalid argument. {'error': {'code': 400, 'status': 'INVALID_ARGUMENT'}}"],
  ["success", "모든 작업을 마쳤습니다."],
], { ytRequests: 7 });
seedRun("r2", 95, "schedule", "done", { done: 1, failed: 0, skipped: 0, pending: 0 }, [
  ["info", "작업 시작: 채널 1개"],
  ["info", "[WSJ] 최신 1개 확인"],
  ["success", "[WSJ] 완료: How a Chip Shortage Turned In…"],
  ["success", "모든 작업을 마쳤습니다."],
]);

// 시험 결과 (설정 > 시험 결과): 목록 문서 + parts에 본문
[["t1", 20, "commentary", "시황 해설 2026-11-18-am 시험 (dry)", "# 11.18 시황 — 보기 글\n\n한눈에 보기 문단입니다.\n\n- 관전 포인트 하나"],
 ["t2", 180, "summarize", "요약 시험 3개 (gemini)", "■ [CNBC Television] Markets open higher\n  요약 한 줄 보기입니다.\n\n요약 3/3개 성공"]]
  .forEach(([id, minAgo, kind, title, text]) => {
    put(`trials/${id}`, { kind, title, chars: text.length, parts: 1, runId: "local", createdAt: ago(minAgo) });
    put(`trials/${id}/parts/0001`, { text });
  });

// ---------- Firestore 흉내 ----------
let seq = 0;
const newId = () => `m${Date.now().toString(36)}${(seq++).toString(36)}`;
const listeners = new Set();
const notify = () => queueMicrotask(() => listeners.forEach((l) => l()));
const resolve = (data) => Object.fromEntries(Object.entries(data).map(([k, v]) => [k, v === SERVER_TS ? new Ts(new Date()) : v]));

export const getFirestore = () => ({});
export const collection = (_db, ...segs) => ({ kind: "col", path: segs.join("/") });
export const doc = (_db, ...segs) => ({ kind: "doc", path: segs.join("/"), id: segs.at(-1) });
export const where = (field, op, value) => ({ type: "where", field, op, value });   // == · >= 만 흉내
export const orderBy = (field, dir = "asc") => ({ type: "orderBy", field, dir });
export const limit = (n) => ({ type: "limit", n });
export const query = (col, ...cons) => ({ kind: "query", path: col.path, cons });

const docSnap = (path) => {
  const data = store.get(path);
  return { id: path.split("/").at(-1), exists: () => data !== undefined, data: () => data && { ...data }, ref: { kind: "doc", path } };
};

function colSnap(ref) {
  const prefix = ref.path + "/";
  let docs = [...store.keys()]
    .filter((p) => p.startsWith(prefix) && !p.slice(prefix.length).includes("/"))
    .map(docSnap);
  for (const c of ref.cons || []) {
    if (c.type === "where" && c.op === ">=") {
      const t = (v) => (v instanceof Ts ? v.d.getTime() : v instanceof Date ? v.getTime() : v);
      docs = docs.filter((d) => d.data()[c.field] != null && t(d.data()[c.field]) >= t(c.value));
    } else if (c.type === "where") docs = docs.filter((d) => d.data()[c.field] === c.value);
    if (c.type === "orderBy") {
      docs = docs.filter((d) => d.data()[c.field] != null);
      const key = (d) => { const v = d.data()[c.field]; return v instanceof Ts ? v.d.getTime() : v; };
      docs.sort((a, b) => (key(a) > key(b) ? 1 : -1) * (c.dir === "desc" ? -1 : 1));
    }
    if (c.type === "limit") docs = docs.slice(0, c.n);
  }
  return { docs };
}

export function onSnapshot(ref, next) {
  const fire = () => next(ref.kind === "doc" ? docSnap(ref.path) : colSnap(ref));
  listeners.add(fire);
  queueMicrotask(fire);
  return () => listeners.delete(fire);
}
export const getDoc = async (ref) => docSnap(ref.path);
export const getDocs = async (ref) => colSnap(ref);
export async function addDoc(col, data) {
  const id = newId();
  put(`${col.path}/${id}`, resolve(data));
  notify();
  if (col.path === "jobs") simulateCollector(id, data);
  return { kind: "doc", path: `${col.path}/${id}`, id };
}
export async function updateDoc(ref, data) {
  put(ref.path, { ...(store.get(ref.path) || {}), ...resolve(data) });
  notify();
}
export async function deleteDoc(ref) {
  store.delete(ref.path);
  notify();
}

// ---------- 수집기 흉내 ("지금 수집" / 재수집) ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function simulateCollector(jobId, job) {
  await sleep(600);
  await updateDoc({ path: `jobs/${jobId}` }, { dispatch: "ok" });
  await sleep(1200);
  const runId = newId();
  const counts = { done: 0, failed: 0, skipped: 0, pending: 0 };
  put(`runs/${runId}`, { trigger: "workflow_dispatch", status: "running", startedAt: new Ts(new Date()), counts });
  const log = async (level, message) => {
    put(`runs/${runId}/logs/${newId()}`, { level, message, createdAt: new Ts(new Date()) });
    notify();
    await sleep(700);
  };
  if (job.type === "reprocess") {
    const v = store.get(`videos/${job.videoId}`) || {};
    await log("info", "작업 시작: 채널 0개");
    await log("info", "대기 중인 영상 1개 처리");
    await log("info", `[${v.channelName}] 받아쓰기 시작: ${v.title}`);
    await updateDoc({ path: `videos/${job.videoId}` }, { status: "done", error: null, processedAt: SERVER_TS, updatedAt: SERVER_TS });
    put(`transcripts/${job.videoId}`, { text: `제목: ${v.title}\n\n[00:00] (재수집된 미리보기 텍스트)` });
    counts.done++;
    await log("success", `[${v.channelName}] 완료: ${v.title}`);
  } else {
    await log("info", "작업 시작: 채널 3개 (전체 수집 요청)");
    await log("info", "[CNBC Television] 최신 10개 확인");
    await log("skip", "[CNBC Television] 이미 처리됨: Markets open higher as tech…");
    counts.skipped++;
    await log("info", "[CNBC Television] 받아쓰기 시작: New video from preview");
    const vid = newId();
    put(`videos/${vid}`, { channelId: "c1", channelName: "CNBC Television", title: "New video from preview", status: "done", attempts: 1, processedAt: new Ts(new Date()), updatedAt: new Ts(new Date()) });
    put(`transcripts/${vid}`, { text: "제목: New video from preview\n\n[00:00] (미리보기 수집 결과)" });
    counts.done++;
    await log("success", "[CNBC Television] 완료: New video from preview");
    await log("info", "[WSJ] 최신 1개 확인");
    await log("skip", "[WSJ] 이미 처리됨: How a Chip Shortage Turned In…");
    counts.skipped++;
    await log("success", "모든 작업을 마쳤습니다.");
  }
  await updateDoc({ path: `runs/${runId}` }, { status: "done", finishedAt: SERVER_TS, counts });
}

// 주소에 ?tab=results 처럼 붙이면 그 탭을 연다
const startTab = new URLSearchParams(location.search).get("tab");
if (startTab) {
  const timer = setInterval(() => {
    if (document.querySelector("#app-main")?.hidden !== false) return; // 화면이 준비될 때까지 기다린다
    clearInterval(timer);
    document.querySelector(`[data-tab="${startTab}"]`)?.click();
  }, 100);
}
// 주소에 ?click=선택자 를 붙이면 화면이 준비된 뒤 그 요소들을 차례로 누른다 (예: ?click=%23tabs-toggle)
for (const sel of new URLSearchParams(location.search).getAll("click")) {
  const timer = setInterval(() => {
    const el = document.querySelector(sel);
    if (!el || document.querySelector("#app-main")?.hidden !== false) return;
    clearInterval(timer);
    el.click();
  }, 150);
}
// 주소에 ?fold 를 붙이면 첫 화면의 "이전 날짜 보기"를 펼친다
if (new URLSearchParams(location.search).has("fold")) {
  const timer = setInterval(() => {
    const fold = document.querySelector("#date-rows button");
    if (!fold) return;
    clearInterval(timer);
    document.querySelector("#date-fold").open = true;
  }, 100);
}
// 주소에 ?open=text 를 붙이면 첫 영상의 요약 · 텍스트 보기 팝업을 연다
if (new URLSearchParams(location.search).has("open")) {
  const timer = setInterval(() => {
    const btn = [...document.querySelectorAll("#home-items button, #video-rows button")]
      .find((b) => ["원본 자막", "요약 보기", "텍스트 보기"].includes(b.textContent));
    if (!btn) return;
    clearInterval(timer);
    btn.click();
  }, 100);
}

// ---------- Auth 흉내 ----------
export const initializeApp = () => ({});
const PREVIEW_USER = { uid: "preview-owner-uid", email: "preview@localhost" };
// 주소에 ?login을 붙이면 로그인된 상태로 시작한다
const authState = { user: location.search.includes("login") ? PREVIEW_USER : null, cbs: new Set() };
export const getAuth = () => authState;
export class GoogleAuthProvider {}
export function onAuthStateChanged(auth, cb) {
  auth.cbs.add(cb);
  queueMicrotask(() => cb(auth.user));
  return () => auth.cbs.delete(cb);
}
export async function signInWithPopup(auth) {
  auth.user = PREVIEW_USER;
  auth.cbs.forEach((cb) => cb(auth.user));
}
export const signInWithRedirect = signInWithPopup;
export const getRedirectResult = async () => null;
export async function signOut(auth) {
  auth.user = null;
  auth.cbs.forEach((cb) => cb(null));
}

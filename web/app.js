// 화면 규칙은 external-ui-kit(UI-가이드.md v2)을 따른다.
// Firebase 설정값은 Firebase Hosting이 제공하는 /__/firebase/init.json에서 받는다 (저장소에 넣지 않음).
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import {
  getAuth, onAuthStateChanged, signInWithPopup, signInWithRedirect, getRedirectResult, signOut, GoogleAuthProvider,
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";
import {
  initializeFirestore, persistentLocalCache, persistentMultipleTabManager, collection, doc, query, where, orderBy, limit, onSnapshot,
  addDoc, updateDoc, deleteDoc, getDoc, getDocs, serverTimestamp,
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";

const $ = (sel) => document.querySelector(sel);

// ---------- 공통 도우미 ----------

/** DOM 요소 생성. 문자열은 textContent로 넣어 HTML 주입을 막는다. */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** <symbol>로 정의한 아이콘 (가이드 13) */
function icon(id, size = 16) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(NS, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

/** 제목 + 바로 옆 새 창 열기 아이콘 버튼 */
function titleWithLink(text, href, name) {
  return h("span", { class: "rec-title" },
    h("span", {}, text),
    h("a", { class: "ui-icon-btn rec-link", href, target: "_blank", rel: "noopener", "aria-label": name, title: name }, icon("i-external")));
}

/** 알림: 3초, 오류는 7초 (가이드 11-7) */
function toast(msg, isError = false) {
  const t = $("#toast");
  const base = isError ? "ui-toast is-error" : "ui-toast";
  t.textContent = msg;
  t.className = `${base} is-show`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.className = base; }, isError ? 7000 : 3000);
}

/** 처리 중에는 그 버튼만 흐리게 하고 글자를 '… 중'으로 (가이드 17-2) */
async function withBusy(btn, busyText, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.textContent = busyText;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
    btn.textContent = label;
  }
}

// 날짜는 점 표기, 앞자리 0 없이: 2026.10.19 13:08 (가이드 3-3)
const dateFmt = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "numeric", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
function parts(ts) {
  const d = ts.toDate ? ts.toDate() : new Date(ts);
  return Object.fromEntries(dateFmt.formatToParts(d).map((p) => [p.type, p.value]));
}
function fmtTime(ts) {
  if (!ts) return "";
  const p = parts(ts);
  return `${p.year}.${p.month}.${p.day} ${p.hour}:${p.minute}`;
}
function fmtClock(ts) {
  if (!ts) return "";
  const p = parts(ts);
  return `${p.hour}:${p.minute}:${p.second}`;
}

/** 작업 상태 라벨 (가이드 10-1) */
const VIDEO_STATUS = {
  done: ["완료", "ui-label--good"],
  pending: ["대기", "ui-label--warn"],
  failed: ["실패", "ui-label--fail"],
  skipped: ["건너뜀", "ui-label--neutral"],
};
const RUN_STATUS = {
  running: ["진행 중", "ui-label--warn"],
  done: ["완료", "ui-label--good"],
  quota: ["한도 도달", "ui-label--warn"],
  blocked: ["유튜브 차단", "ui-label--warn"],
  error: ["오류", "ui-label--fail"],
};
function label([text, cls] = ["알 수 없음", "ui-label--neutral"]) {
  return h("span", { class: `ui-label ui-label--bare ${cls}` }, text);
}

/** <dialog> 공통: × 버튼, 바깥(상자 밖 좌표) 누르기로 닫기 (가이드 11-4) */
function setupDialog(dlg) {
  dlg.querySelector(".ui-pop-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => {
    if (e.target !== dlg) return;
    const r = dlg.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
  });
}
setupDialog($("#text-dialog"));
setupDialog($("#confirm-dialog"));

/** 되돌리기 어려운 동작은 한 문장으로 묻고 한다 (가이드 17-1) */
function ask({ title, text, ok, danger = false }) {
  const dlg = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  const okBtn = $("#confirm-ok");
  okBtn.textContent = ok;
  okBtn.className = danger ? "ui-btn-white ui-danger" : "ui-btn ui-btn--block";
  return new Promise((resolve) => {
    let answer = false;
    okBtn.onclick = () => { answer = true; dlg.close(); };
    $("#confirm-cancel").onclick = () => dlg.close();
    dlg.addEventListener("close", () => resolve(answer), { once: true });
    dlg.showModal();
  });
}

/** 카드 안 펼침 버튼 (가이드 6-1): 처음 몇 줄만 보이고 나머지는 접는다 */
function foldButton(btn, total, shown, expanded, noun) {
  btn.hidden = total <= shown;
  btn.setAttribute("aria-expanded", String(expanded));
  btn.firstElementChild.textContent = expanded ? "접기" : `${noun} ${total - shown}개 더 보기`;
}

// ---------- Firebase 시작 ----------

const config = await (await fetch("/__/firebase/init.json")).json();
// 로그인 처리 주소를 지금 열린 주소(web.app)와 맞춘다. 기본값 firebaseapp.com은 다른 주소라
// 휴대폰 브라우저가 로그인 결과 전달을 막는다 (OAuth 클라이언트에 이 주소의 /__/auth/handler 등록 필요)
if (/\.(web\.app|firebaseapp\.com)$/.test(location.hostname)) config.authDomain = location.host;
const app = initializeApp(config);
const auth = getAuth(app);
// 받은 문서를 브라우저(IndexedDB)에 남긴다. 다시 열거나 새로고침해도 30분 안이면 바뀐 문서만 읽기로 센다 (11-12, 읽기 줄이기).
// 브라우저가 IndexedDB를 막으면 SDK가 메모리 캐시로 돌아간다
const db = initializeFirestore(app, { localCache: persistentLocalCache({ tabManager: persistentMultipleTabManager() }) });

let unsubscribers = [];
let settingsListening = false;   // 설정 화면용 구독(영상 · 수집 현황 · meta · 실행 기록)은 설정을 처음 열 때 시작한다
function stopListeners() {
  unsubscribers.forEach((u) => u());
  unsubscribers = [];
  settingsListening = false;
}

/** 로그인 전에는 다른 화면 · 버튼을 보여 주지 않는다 (가이드 1-8, 14) */
function show(view) {
  $("#login-view").hidden = view !== "login";
  $("#denied-view").hidden = view !== "denied";
  $("#app-header").hidden = view !== "app";
  $("#app-main").hidden = view !== "app";
}

// 휴대폰은 새 창이 막히기 쉬워 페이지를 넘겨 로그인한다. 돌아온 뒤 결과는 onAuthStateChanged가 받는다
const isMobile = matchMedia("(pointer: coarse)").matches;
const loginFailed = (e) => toast(`로그인하지 못했습니다. ${e.code || e.message}`, true);
getRedirectResult(auth).catch(loginFailed);
$("#login-btn").addEventListener("click", () => {
  const provider = new GoogleAuthProvider();
  if (isMobile) return signInWithRedirect(auth, provider).catch(loginFailed);
  signInWithPopup(auth, provider).catch((e) => (e.code === "auth/popup-blocked"
    ? signInWithRedirect(auth, provider).catch(loginFailed) : loginFailed(e)));
});
document.querySelectorAll(".logout-btn").forEach((b) => b.addEventListener("click", () => signOut(auth)));
$("#uid-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#denied-uid").textContent);
  toast("복사했습니다.");
});

onAuthStateChanged(auth, (user) => {
  stopListeners();
  if (!user) return show("login");
  $("#user-email").textContent = user.email || user.uid;
  $("#denied-uid").textContent = user.uid;
  show("app");
  startListeners();
});

/** 보안 규칙에 막히면(소유자가 아니면) 안내 화면으로 바꾼다. */
function onListenError(err) {
  if (err.code === "permission-denied") {
    stopListeners();
    show("denied");
  } else {
    console.error(err);
    toast(`불러올 수 없습니다. ${err.message}`, true);
  }
}

/** 첫 화면은 채널 · 요약만 읽는다. 나머지는 설정 화면을 열 때 (11-12, 읽기 줄이기) */
function startListeners() {
  listenChannels();
  listenSummaries();
  route();
}
function startSettingsListeners() {
  if (settingsListening) return;
  settingsListening = true;
  listenVideos();
  listenStats();
  listenYtMeta();
  listenRuns();
}

// ---------- 화면 이동 (주소 해시에 남김, 가이드 9) ----------
//   #            첫 화면 (모아보기). #ch=채널ID 로 채널 탭, #cat=분류코드 로 분류 라벨 탭을 기억한다
//   #settings    설정 화면 (처음엔 설정 · 로그). #logs · #results · #channels · #trials 는 설정 안의 하위 탭

const SETTINGS_TABS = ["logs", "results", "channels", "trials"];

function route() {
  const hash = location.hash.slice(1);
  const settings = hash === "settings" || SETTINGS_TABS.includes(hash);
  $("#view-home").hidden = settings;
  $("#view-settings").hidden = !settings;
  $("#tabs-toggle").hidden = settings;   // 탭은 첫 화면에만 있다
  $("#cat-toggle").hidden = settings;
  if (settings) {
    startSettingsListeners();
    const name = SETTINGS_TABS.includes(hash) ? hash : "logs";
    if (name === "logs") renderStats();
    if (name === "trials") listenTrials();   // 이 탭을 처음 열 때만 읽는다
    document.querySelectorAll(".settings-tabs .ui-subtab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    for (const n of SETTINGS_TABS) $(`#view-${n}`).hidden = n !== name;
  } else {
    const m = /^(ch|cat)=(.+)$/.exec(hash);
    home.picks = hash === "picks";
    if (m?.[1] === "cat") { home.cat = decodeURIComponent(m[2]); }
    else { home.cat = null; if (!home.picks) home.channelId = m ? decodeURIComponent(m[2]) : ALL; }
    renderHome();
  }
}
document.querySelectorAll(".settings-tabs .ui-subtab").forEach((t) => t.addEventListener("click", () => { location.hash = t.dataset.tab; }));
window.addEventListener("hashchange", route);

// ---------- 첫 화면: 채널 탭 · 오늘 묶음 요약 ----------

// 탭 순서 (사용자 지정). 맨 앞은 모아보기, 여기 없는 채널은 이름순으로 뒤에 붙는다
const TAB_ORDER = ["당잠사", "증시각도기", "CNBC Mad Money"];
const ALL = "all";              // 모아보기 탭의 channelId
const CAT_WINDOW_H = 48;        // 분류 라벨 탭: 최근 몇 시간에 올라온 영상 (사용자 지정)
const PICK_WINDOW_D = 5;        // 추천 종목 탭: 최근 며칠 (요약 구독 400개가 닷새쯤이라 그 안에서)
const DATE_PAGE = 5;
const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

// cat: 분류 라벨 탭에서 고른 분류 (null = 채널 탭). picks: 추천 종목 탭. date: null = 오늘 묶음
const home = { channelId: ALL, cat: null, picks: false, date: null, page: 1 };
let homeSummaries = [];   // 고른 채널의 요약 (최신순)
let homeUnsub = null;
let homeListening = null; // 지금 구독 중인 채널 ID

/** 오늘 묶음 = 보는 날의 한국 날짜. 수집기는 전날 12:00 ~ 오늘 11:59에 받은 것에 오늘 날짜(edition)를 붙이므로,
 *  아침에 열면 어젯밤 미국 방송 + 오늘 아침 한국 방송이 "오늘"이고, 오후에 열어도 그 묶음이 그대로 보인다 */
function currentEdition() {
  return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

function fmtDate(ymd) {
  if (!ymd) return "";
  const [y, m, d] = ymd.split("-").map(Number);
  return `${y}.${m}.${d} (${WEEKDAY[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]})`;
}

function tabChannels() {
  const rank = (c) => { const i = TAB_ORDER.indexOf(c.name); return i < 0 ? TAB_ORDER.length : i; };
  return [...channelsCache].sort((a, b) => rank(a) - rank(b) || (a.name || "").localeCompare(b.name || "", "ko"));
}

/** 유튜브 게시 시각(ms). 없으면 요약 저장 시각 */
function publishedMs(s) {
  if (s.publishedAt) return Date.parse(s.publishedAt);
  return s.createdAt?.toMillis ? s.createdAt.toMillis() : 0;
}
const byPublished = (a, b) => publishedMs(b) - publishedMs(a);

/** 전체 요약 구독(summariesCache, 최근 400개)을 목록으로 */
function allSummaries() {
  return [...summariesCache].map(([id, s]) => ({ id, ...s }));
}

/** 분류 라벨 탭에 쓰는 요약: 최근 48시간에 유튜브에 올라온 것 */
function recentSummaries() {
  const from = Date.now() - CAT_WINDOW_H * 3600 * 1000;
  return allSummaries().filter((s) => publishedMs(s) >= from);
}

/** [분류, 영상 수], 많은 순 */
function categoryCounts(list) {
  const counts = new Map();
  for (const s of list) for (const c of new Set((s.issues || []).map((i) => i.category).filter(Boolean))) counts.set(c, (counts.get(c) || 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || (CATEGORY_NAME[a[0]] || a[0]).localeCompare(CATEGORY_NAME[b[0]] || b[0], "ko"));
}

function tabButton(text, selected, onclick) {
  return h("button", { class: "ui-tab", role: "tab", type: "button", "aria-selected": String(selected), onclick }, text);
}

/** 한 줄 탭에서 고른 탭이 화면 밖이면 가로로만 당겨 보인다 (라벨을 눌러 뒤쪽 탭으로 갈 때) */
function revealSelectedTab() {
  const tabs = $("#channel-tabs");
  const t = tabs.querySelector("[aria-selected='true']");
  if (!t || tabs.classList.contains("is-all")) return;
  if (t.offsetLeft < tabs.scrollLeft || t.offsetLeft + t.offsetWidth > tabs.scrollLeft + tabs.clientWidth) {
    tabs.scrollLeft = t.offsetLeft - (tabs.clientWidth - t.offsetWidth) / 2;
  }
}

function renderChannelTabs() {
  const list = tabChannels();
  if (list.length && home.channelId !== ALL && !list.some((c) => c.id === home.channelId)) home.channelId = ALL;
  const go = (id) => () => { home.date = null; home.page = 1; location.hash = `ch=${encodeURIComponent(id)}`; window.scrollTo({ top: 0 }); };
  const on = (id) => !home.picks && home.channelId === id;
  $("#channel-tabs").replaceChildren(
    tabButton("모아보기", on(ALL), go(ALL)),
    tabButton("추천 종목", home.picks, () => { location.hash = "picks"; window.scrollTo({ top: 0 }); }),
    ...list.map((c) => tabButton(c.name, on(c.id), go(c.id))));
  revealSelectedTab();
}

function renderCategoryTabs(counts) {
  $("#channel-tabs").replaceChildren(...counts.map(([c, n]) => tabButton(`${CATEGORY_NAME[c] || c} (${n})`, c === home.cat,
    () => openCategory(c))));
  revealSelectedTab();
}

/** 분류 라벨을 누르면 그 라벨 탭으로 (요약 팝업에서 눌렀으면 팝업을 닫는다) */
function openCategory(c) {
  if ($("#text-dialog").open) $("#text-dialog").close();
  location.hash = `cat=${encodeURIComponent(c)}`;
  window.scrollTo({ top: 0 });
}

// 탭 모두 보기: 머리의 아이콘으로 한 줄(가로 스크롤) ↔ 여러 줄 전환. 이 브라우저에만 기억한다
const TABS_KEY = "tabsAll";
function setTabsAll(on, remember = true) {
  $("#channel-tabs").classList.toggle("is-all", on);
  $("#tabs-toggle").setAttribute("aria-pressed", String(on));
  if (remember) try { localStorage.setItem(TABS_KEY, on ? "1" : ""); } catch { /* 저장 못 해도 화면은 그대로 */ }
  // 한 줄로 돌아오면 고른 탭이 보이게 가로로만 맞춘다
  if (!on) $("#channel-tabs [aria-selected='true']")?.scrollIntoView({ block: "nearest", inline: "center" });
}
$("#tabs-toggle").addEventListener("click", () => setTabsAll(!$("#channel-tabs").classList.contains("is-all")));
try { if (localStorage.getItem(TABS_KEY)) setTabsAll(true); } catch { /* 기본은 한 줄 */ }
// 펼친 채로 탭을 여러 번 골라 보다가 본문을 읽으려고 아래로 내리면 한 줄로 접는다 (기억은 그대로).
// 탭을 고를 때 맨 위로 가는 건 위쪽이라 접히지 않는다
let tabsScrollY = window.scrollY;   // 펼친 뒤 가장 위였던 곳. 여기서 조금이라도 내려가면 접는다
window.addEventListener("scroll", () => {
  const y = window.scrollY;
  if (!$("#channel-tabs").classList.contains("is-all") || y < tabsScrollY) { tabsScrollY = y; return; }
  if (y > tabsScrollY + 8) { setTabsAll(false, false); tabsScrollY = window.scrollY; }
}, { passive: true });

// 분류 라벨 탭 ↔ 채널 탭 (머리 아이콘). 라벨 탭은 영상이 가장 많은 분류부터 연다
$("#cat-toggle").addEventListener("click", () => {
  if (home.cat) { location.hash = `ch=${encodeURIComponent(home.channelId)}`; return; }
  const first = categoryCounts(recentSummaries())[0];
  openCategory(first ? first[0] : "OTHER");
});

/** 고른 채널의 요약만 구독한다 (이전 날짜 목록까지 쓰므로 넉넉히). 모아보기 · 분류 라벨 탭은 전체 구독을 쓴다 */
function listenHome() {
  if (homeListening === home.channelId) return;
  if (homeUnsub) { homeUnsub(); unsubscribers = unsubscribers.filter((u) => u !== homeUnsub); }
  homeListening = home.channelId;
  homeSummaries = [];
  const q = query(collection(db, "summaries"), where("channelId", "==", home.channelId), orderBy("createdAt", "desc"), limit(400));
  homeUnsub = onSnapshot(q, (snap) => {
    homeSummaries = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    renderHome();
  }, onListenError);
  unsubscribers.push(homeUnsub);
}

function renderHome() {
  if (!channelsCache) return;
  $("#cat-toggle").setAttribute("aria-pressed", String(!!home.cat));
  if (home.cat) return renderCategoryHome();
  renderChannelTabs();
  if (home.picks) return renderPicksHome();
  const all = home.channelId === ALL;
  if (!all) listenHome();
  const source = all ? allSummaries() : homeSummaries;
  const today = currentEdition();
  const date = home.date || today;
  $("#home-date").textContent = fmtDate(date);
  $("#home-today").hidden = date === today;

  // 모아보기는 여러 채널이 섞이므로 유튜브 게시 시각 최신순
  const items = source.filter((s) => s.edition === date);
  if (all) items.sort(byPublished);
  $("#home-items").replaceChildren(...(items.length ? items.map((s) => summaryItem(s))
    : [h("div", { class: "ui-empty" }, !channelsCache.length ? "등록된 채널이 없습니다. 설정에서 채널을 등록하세요."
      : date === today ? "오늘 받은 영상이 아직 없습니다." : "이 날짜에 받은 영상이 없습니다.")]));

  // 이전 날짜: 오늘 묶음을 뺀 나머지 날짜를 최신순, 5개씩 쪽 번호
  const counts = new Map();
  for (const s of source) if (s.edition && s.edition !== today) counts.set(s.edition, (counts.get(s.edition) || 0) + 1);
  const dates = [...counts.keys()].sort().reverse();
  const pages = Math.max(1, Math.ceil(dates.length / DATE_PAGE));
  if (home.page > pages) home.page = pages;
  const shown = dates.slice((home.page - 1) * DATE_PAGE, home.page * DATE_PAGE);
  $("#date-fold").hidden = false;
  $("#date-rows").replaceChildren(...(shown.length ? shown.map((d) => h("button", {
    class: "ui-row run-row date-row", type: "button", "aria-current": String(d === home.date),
    onclick: () => { home.date = d; renderHome(); window.scrollTo({ top: 0 }); },
  }, h("span", { class: "ui-row-name ui-num" }, fmtDate(d)), h("span", { class: "ui-row-value ui-num ui-sub" }, `${counts.get(d)}개`)))
    : [h("div", { class: "ui-empty" }, "이전 날짜가 없습니다.")]));
  const segs = $("#date-pages");
  segs.hidden = pages <= 1;
  segs.replaceChildren(...Array.from({ length: pages }, (_, i) => h("button", {
    class: "ui-seg", type: "button", "aria-pressed": String(i + 1 === home.page), onclick: () => { home.page = i + 1; renderHome(); },
  }, String(i + 1))));
}

/** 분류 라벨 탭: 최근 48시간에 올라온 영상 중 그 분류 이슈가 있는 것, 게시 시각 최신순 */
function renderCategoryHome() {
  const recent = recentSummaries();
  const counts = categoryCounts(recent);
  // 주소로 들어온 분류가 48시간 안에 없을 때 (모르는 코드면 탭을 만들지 않는다)
  if (CATEGORY_NAME[home.cat] && !counts.some(([c]) => c === home.cat)) counts.push([home.cat, 0]);
  renderCategoryTabs(counts);
  $("#home-date").textContent = `최근 ${CAT_WINDOW_H}시간`;
  $("#home-today").hidden = true;
  $("#date-fold").hidden = true;
  const items = recent.filter((s) => (s.issues || []).some((i) => i.category === home.cat)).sort(byPublished);
  $("#home-items").replaceChildren(...(items.length ? items.map((s) => summaryItem(s, { cat: home.cat }))
    : [h("div", { class: "ui-empty" }, `최근 ${CAT_WINDOW_H}시간에 이 분류의 영상이 없습니다.`)]));
}
/** 추천 종목 탭: 최근 며칠에 올라온 영상의 종목 의견을 종목별로 묶는다. 의견이 많은 종목 → 최근 의견 순 */
function renderPicksHome() {
  $("#home-date").textContent = `최근 ${PICK_WINDOW_D}일`;
  $("#home-today").hidden = true;
  $("#date-fold").hidden = true;
  const from = Date.now() - PICK_WINDOW_D * 86400 * 1000;
  const groups = new Map();
  for (const s of allSummaries().filter((x) => publishedMs(x) >= from).sort(byPublished)) {
    for (const p of s.picks || []) {
      const key = p.ticker || (p.name || "").replace(/\s/g, "");   // 수집기 picks.key와 같은 규칙
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ ...p, s });
    }
  }
  const list = [...groups.values()].sort((a, b) => b.length - a.length || publishedMs(b[0].s) - publishedMs(a[0].s));
  loadPickStats();
  $("#home-items").replaceChildren(...[pickStatsCard()].filter(Boolean), ...(list.length ? list.map(pickGroup)
    : [h("div", { class: "ui-empty" }, `최근 ${PICK_WINDOW_D}일에 방송에서 나온 종목 의견이 없습니다.`)]));
}

// ---------- 종목 의견 적중률 (meta/pickStats, worker/prices.py가 하루 1번 씀, 11-13) ----------
// 문서 1개만 읽는다. 추천 종목 탭을 처음 열 때 한 번 (새로고침 전까지 다시 읽지 않음)
let pickStats = null;   // null = 아직, false = 읽는 중 · 없음, 객체 = 읽음
const HORIZON_NAME = { 5: "1주", 20: "1개월", 60: "3개월" };

function loadPickStats() {
  if (pickStats !== null) return;
  pickStats = false;
  getDoc(doc(db, "meta", "pickStats")).then((snap) => {
    if (!snap.exists()) return;
    const d = snap.data();
    d.byPick = new Map((d.recent || []).map((r) => [`${r.videoId}|${r.name}`, r]));
    pickStats = d;
    if (home.picks && !$("#view-home").hidden) renderHome();
  }).catch(() => {});
}

/** 등락 표시: ▲ 1.2% (빨강) · ▼ 0.8% (파랑) · 없음 — (가이드 4-4) */
function delta(x) {
  if (x == null) return h("span", { class: "ui-delta ui-sub" }, "—");
  const pct = `${Math.abs(x * 100).toFixed(1)}%`;
  return x > 0 ? h("span", { class: "ui-delta ui-up" }, `▲ ${pct}`) : x < 0 ? h("span", { class: "ui-delta ui-down" }, `▼ ${pct}`)
    : h("span", { class: "ui-delta" }, pct);
}
const hitText = (r, n) => (r[`n${n}`] ? `${Math.round((r[`hit${n}`] / r[`n${n}`]) * 100)}% (${r[`hit${n}`]}/${r[`n${n}`]})` : "—");

/** 위쪽 카드: 전체 적중률 + 채널별 · 발언자별 (접힘) */
function pickStatsCard() {
  if (!pickStats || !pickStats.overall) return null;
  const hs = pickStats.horizons || [5, 20, 60];
  const o = pickStats.overall;
  const up = pickStats.updatedAt?.toDate ? parts(pickStats.updatedAt) : null;
  const rows = (list) => list.map((r) => h("div", { class: "ui-row" },
    h("span", { class: "ui-row-name" }, r.name,
      h("small", { class: "ui-num" }, [`매수 · 매도 의견 ${r.picks}건`, ...hs.map((n) => `${HORIZON_NAME[n]} ${hitText(r, n)}`)].join(" · "))),
    h("span", { class: "ui-row-value ui-num" }, r[`n${hs[0]}`] ? `${Math.round((r[`hit${hs[0]}`] / r[`n${hs[0]}`]) * 100)}%` : "—")));
  const fold = (title, list) => {   // 접기 (가이드 6-1, statFold와 같은 모양)
    if (!list?.length) return null;
    const chev = icon("i-chev");
    chev.setAttribute("class", "ui-chev");
    return h("details", { class: "ui-fold" }, h("summary", {}, h("span", {}, `${title} (${list.length})`), chev), ...rows(list));
  };
  return h("article", { class: "sum-item" },
    h("div", { class: "sum-title" }, "적중률 ", h("span", { class: "ui-paren" }, "(시장 대비)")),
    h("small", { class: "ui-sub" }, "매수는 지수보다 더 오르면, 매도는 덜 오르면 적중. 방송 뒤 첫 종가 기준, 거래일로 셈"
      + (up ? ` · ${up.year}.${up.month}.${up.day} 계산` : "")),
    h("div", { class: "ui-stats" }, ...hs.map((n) => h("div", { class: "ui-stat" },
      h("span", { class: "ui-stat-name" }, HORIZON_NAME[n] || `${n}일`),
      h("span", { class: "ui-stat-value ui-num" }, o[`n${n}`] ? `${Math.round((o[`hit${n}`] / o[`n${n}`]) * 100)}%` : "—"),
      h("small", { class: "ui-num ui-sub" }, o[`n${n}`] ? `${o[`hit${n}`]}/${o[`n${n}`]}건` : "아직 없음")))),
    fold("채널별", pickStats.channels),
    fold("발언자별 (2건 이상)", pickStats.speakers));
}

/** 의견 한 건 아래 수익률 줄: 방송 뒤 ▲ 3.1% · 시장 대비 ▲ 1.0% · 1주 적중 */
function pickPerfLine(p) {
  const r = pickStats?.byPick?.get(`${p.s.id}|${p.name}`);
  if (!r || r.rNow == null) return null;
  const hit = [5, 20, 60].filter((n) => r[`hit${n}`] != null).map((n) => `${HORIZON_NAME[n]} ${r[`hit${n}`] ? "적중" : "빗나감"}`);
  return h("small", { class: "ui-num ui-sub pick-perf" }, `방송 뒤 ${r.days}거래일 `, delta(r.rNow),
    " · 시장 대비 ", delta(r.xNow), hit.length ? ` · ${hit.join(" · ")}` : "");
}
$("#home-today").addEventListener("click", () => { home.date = null; renderHome(); });
$("#to-top").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));

/** 요약 한 건. 보조 줄: 유튜브 게시 시각 · 영상 길이. 영상 보기 · 원본 자막 버튼은 맨 아래 오른쪽
 *  opts.cat: 그 분류 이슈를 앞으로 */
function summaryItem(s, opts = {}) {
  // 짧게 (사용자 지정): 채널 이름 · 연도 없이 "10.28 10:51 게시 · 9분"
  const p = s.publishedAt ? parts(s.publishedAt) : null;
  const meta = [
    p ? `${p.month}.${p.day} ${p.hour}:${p.minute} 게시` : null,
    s.durationSec ? `${Math.round(s.durationSec / 60)}분` : null,
  ].filter(Boolean);
  return h("article", { class: "sum-item" },
    h("div", { class: "sum-title" }, s.titleKo || s.title || s.id),
    meta.length ? h("small", { class: "ui-num ui-sub" }, meta.join(" · ")) : null,
    h("div", { class: "summary" }, ...summaryView(s, opts.cat)),
    h("div", { class: "sum-foot" },
      h("a", { class: "ui-btn-outline", href: s.url || `https://www.youtube.com/watch?v=${s.id}`, target: "_blank", rel: "noopener" }, "영상 보기"),
      h("button", { class: "ui-btn-outline", type: "button", onclick: () => openText({ id: s.id, title: s.titleKo || s.title }, "text") }, "원본 자막")));
}

// ---------- 웹 요청 (재수집) ----------

/** Cloud Function이 GitHub 실행을 요청한 결과(dispatch 필드)를 확인한다. */
function watchDispatch(ref) {
  const unsub = onSnapshot(ref, (snap) => {
    const r = snap.data()?.dispatch;
    if (!r) return;
    unsub();
    if (r !== "ok") toast(`GitHub 실행을 요청하지 못했습니다. (${r})`, true);
  });
  setTimeout(unsub, 60000);
}

// ---------- 채널 ----------

const channelForm = $("#channel-form");
// form.id / form.name은 폼 자체의 속성이라 입력 칸은 elements로 접근한다
const fields = channelForm.elements;
const fold = $("#channel-fold");
let channelsCache = [];

fields.count.replaceChildren(...Array.from({ length: 20 }, (_, i) => h("option", { value: i + 1 }, `${i + 1}개`)));
fields.count.value = "5";
// 하루 몫: 비우면 수집기가 묶음 한도 ÷ 묶음 안 채널 수로 정한다 (collector.channel_shares)
fields.dayMax.replaceChildren(h("option", { value: "" }, "자동 (묶음 한도 ÷ 채널 수)"),
  ...Array.from({ length: 60 }, (_, i) => h("option", { value: i + 1 }, `${i + 1}개`)));

function listenChannels() {
  unsubscribers.push(onSnapshot(collection(db, "channels"), (snap) => {
    channelsCache = snap.docs.map((d) => ({ id: d.id, ...d.data() }))
      .sort((a, b) => (a.name || "").localeCompare(b.name || "", "ko"));
    renderChannels();
    renderChannelFilter();
    renderHome();
  }, onListenError));
}

function renderChannels() {
  $("#channel-count").textContent = channelsCache.length ? `${channelsCache.length}개` : "";
  const box = $("#channel-rows");
  if (!channelsCache.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, "아직 등록된 채널이 없습니다."));
    return;
  }
  box.replaceChildren(...channelsCache.map((c) => {
    const on = c.enabled !== false;
    const editing = channelEditing.has(c.id);
    return h("div", { class: "ui-row ui-row--top rec-row" },
      h("span", { class: "ui-row-name" },
        h("span", { class: "rec-title" },
          titleWithLink(c.name, c.url, "채널 주소 열기"),
          h("button", {
            class: "ui-icon-btn rec-link", type: "button", "aria-pressed": String(editing),
            "aria-label": "편집", title: "편집", onclick: () => toggleChannelEdit(c.id),
          }, icon("i-edit"))),
        h("small", { class: "ui-num" }, `${c.count}개${c.dayMax ? ` · 하루 몫 ${c.dayMax}개` : ""} · ${(c.times || []).join(", ")}`),
        h("small", { class: "ui-num" }, c.lastRunAt ? `마지막 수집 : ${fmtTime(c.lastRunAt)}` : "아직 수집하지 않았습니다."),
        // 수정 · 중지 · 삭제는 편집 아이콘을 눌렀을 때만 보인다 (실수로 누르지 않게)
        editing ? h("span", { class: "ui-row-actions" },
          h("button", { class: "ui-btn-outline", type: "button", onclick: () => editChannel(c) }, "수정"),
          h("button", { class: "ui-btn-outline", type: "button", onclick: (e) => toggleChannel(c, e.currentTarget) }, on ? "중지" : "사용"),
          h("button", { class: "ui-btn-outline ui-danger", type: "button", onclick: () => removeChannel(c) }, "삭제")) : null),
      h("span", { class: "ui-row-value" }, label(on ? ["사용", "ui-label--good"] : ["중지", "ui-label--neutral"])));
  }));
}

const channelEditing = new Set();   // 편집 버튼을 펼친 채널 ID
function toggleChannelEdit(id) {
  if (!channelEditing.delete(id)) channelEditing.add(id);
  renderChannels();
}

// 자동 수집 전환 버튼 (켜기 · 끄기도 전환 버튼으로, 가이드 9-1)
const segs = [...document.querySelectorAll("#enabled-segs .ui-seg")];
function setEnabled(on) {
  segs.forEach((s) => s.setAttribute("aria-pressed", String((s.dataset.value === "on") === on)));
}
segs.forEach((s) => s.addEventListener("click", () => setEnabled(s.dataset.value === "on")));
const isEnabled = () => segs.find((s) => s.dataset.value === "on").getAttribute("aria-pressed") === "true";

function parseTimes(text) {
  const times = text.split(",").map((t) => t.trim()).filter(Boolean);
  const bad = times.filter((t) => !/^([01]?\d|2[0-3]):[0-5]\d$/.test(t));
  if (bad.length) throw new Error(`시각은 08:10처럼 입력하세요 : ${bad.join(", ")}`);
  if (!times.length) throw new Error("수집 시각을 하나 이상 입력하세요.");
  return times.map((t) => t.padStart(5, "0"));
}

channelForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const err = $("#form-error");
  err.textContent = "";
  let data;
  try {
    const name = fields.name.value.trim();
    const url = fields.url.value.trim();
    if (!name) throw new Error("채널 이름을 입력하세요.");
    if (!/^https?:\/\/\S+$/.test(url)) throw new Error("주소는 https://로 시작하게 입력하세요.");
    data = { name, url, count: Number(fields.count.value), times: parseTimes(fields.times.value), enabled: isEnabled(),
      dayMax: fields.dayMax.value ? Number(fields.dayMax.value) : null };
  } catch (ex) {
    err.textContent = ex.message;
    return;
  }
  const id = fields.id.value;
  withBusy($("#channel-submit"), "저장 중…", async () => {
    try {
      if (id) {
        const old = channelsCache.find((c) => c.id === id);
        // 주소가 바뀌면 재생목록 ID를 다시 찾도록 비운다
        if (old && old.url !== data.url) data.playlistId = null;
        await updateDoc(doc(db, "channels", id), data);
        toast("수정했습니다.");
      } else {
        await addDoc(collection(db, "channels"), { ...data, createdAt: serverTimestamp() });
        toast("등록했습니다.");
      }
      resetChannelForm();
    } catch (ex) {
      err.textContent = `저장하지 못했습니다. ${ex.message}`;
    }
  });
});

function editChannel(c) {
  fields.id.value = c.id;
  fields.name.value = c.name || "";
  fields.url.value = c.url || "";
  fields.count.value = String(c.count || 5);
  fields.dayMax.value = c.dayMax ? String(c.dayMax) : "";
  fields.times.value = (c.times || []).join(", ");
  setEnabled(c.enabled !== false);
  $("#form-title").textContent = "채널 수정";
  $("#channel-submit").textContent = "수정 저장";
  $("#channel-cancel").hidden = false;
  $("#form-actions").classList.add("ui-pair");
  $("#form-error").textContent = "";
  fold.open = true;
  fold.scrollIntoView({ behavior: "smooth", block: "start" });
}

function resetChannelForm() {
  channelForm.reset();
  fields.id.value = "";
  fields.count.value = "5";
  fields.dayMax.value = "";
  setEnabled(true);
  $("#form-title").textContent = "채널 등록";
  $("#channel-submit").textContent = "등록";
  $("#channel-cancel").hidden = true;
  $("#form-actions").classList.remove("ui-pair");
  $("#form-error").textContent = "";
  fold.open = false;
}
$("#channel-cancel").addEventListener("click", resetChannelForm);

function toggleChannel(c, btn) {
  const on = c.enabled !== false;
  withBusy(btn, "저장 중…", async () => {
    try {
      await updateDoc(doc(db, "channels", c.id), { enabled: !on });
      toast(on ? `'${c.name}' 자동 수집을 중지했습니다.` : `'${c.name}' 자동 수집을 다시 켰습니다.`);
    } catch (ex) {
      toast(`저장하지 못했습니다. ${ex.message}`, true);
    }
  });
}

async function removeChannel(c) {
  const ok = await ask({
    title: "채널 삭제",
    text: `'${c.name}' 채널을 삭제할까요? 이미 수집한 결과는 남습니다.`,
    ok: "삭제",
    danger: true,
  });
  if (!ok) return;
  try {
    await deleteDoc(doc(db, "channels", c.id));
    toast("삭제했습니다.");
  } catch (ex) {
    toast(`삭제하지 못했습니다. ${ex.message}`, true);
  }
}

// ---------- 결과 ----------

const VIDEO_PAGE = 20;
let videosCache = null;
let videosExpanded = false;

function listenVideos() {
  // v1에서 옮겨 온 기록(updatedAt 없음)은 목록에 나오지 않는다
  const q = query(collection(db, "videos"), orderBy("updatedAt", "desc"), limit(200));
  unsubscribers.push(onSnapshot(q, (snap) => {
    videosCache = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    renderVideos();
  }, onListenError));
}

function renderChannelFilter() {
  const sel = $("#filter-channel");
  const current = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "모든 채널"),
    ...channelsCache.map((c) => h("option", { value: c.id }, c.name)));
  sel.value = current;
}
for (const id of ["#filter-channel", "#filter-status"]) {
  $(id).addEventListener("change", () => { videosExpanded = false; renderVideos(); });
}
$("#video-fold").addEventListener("click", () => { videosExpanded = !videosExpanded; renderVideos(); });

function renderVideos() {
  const box = $("#video-rows");
  const btn = $("#video-fold");
  if (!videosCache) return;
  const ch = $("#filter-channel").value;
  const st = $("#filter-status").value;
  const list = videosCache.filter((v) => (!ch || v.channelId === ch) && (!st || v.status === st));
  if (!list.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, videosCache.length ? "조건에 맞는 영상이 없습니다." : "아직 수집한 영상이 없습니다."));
    btn.hidden = true;
    return;
  }
  const shown = videosExpanded ? list : list.slice(0, VIDEO_PAGE);
  box.replaceChildren(...shown.map(videoRow));
  foldButton(btn, list.length, VIDEO_PAGE, videosExpanded, "영상");
}

// ---------- 영상별 요약 (summaries) ----------

let summariesCache = new Map();

/** 최근 요약 400개 (하루 80개 안팎 → 닷새쯤). 수집 결과 목록 · 모아보기 · 분류 라벨 탭이 함께 쓴다 */
function listenSummaries() {
  const q = query(collection(db, "summaries"), orderBy("createdAt", "desc"), limit(400));
  unsubscribers.push(onSnapshot(q, (snap) => {
    summariesCache = new Map(snap.docs.map((d) => [d.id, d.data()]));
    renderVideos();
    if (!$("#view-home").hidden && (home.cat || home.picks || home.channelId === ALL)) renderHome();
  }, onListenError));
}

// 이슈 분류 17개 (doc/데이터-설계.md 2장) → 화면 이름. 11-07 이전 문서의 옛 이름 세 개도 같은 이름으로 보인다
const CATEGORY_NAME = {
  RATES_FED: "금리·중앙은행", INFLATION: "물가", GROWTH_JOBS: "경기·고용", TARIFF_TRADE: "관세·무역",
  GEOPOLITICS: "지정학", OIL_ENERGY: "에너지·원자재", AI_CAPEX: "AI 투자", EARNINGS: "실적",
  CORP_EVENT: "기업 이벤트", CREDIT_BANKS: "은행·신용", MARKET_STRUCTURE: "수급·변동성", REGULATION: "규제",
  FISCAL_POLITICS: "재정·정치", FX_GLOBAL: "환율·해외 증시", CRYPTO: "암호화폐", REAL_ESTATE: "부동산", OTHER: "기타",
  RATES_CB: "금리·중앙은행", ENERGY_COMMODITY: "에너지·원자재", FX: "환율·해외 증시",
};
// stance = 이슈에 적힌 종목 · 자산의 가격 방향
const STANCE = { bullish: ["상승", "ui-label--pos"], bearish: ["하락", "ui-label--neg"], neutral: ["중립", "ui-label--neutral"] };

// 종목 의견 (summaries.picks[].action). 매수 = 빨강 · 매도 = 파랑 (상승 · 하락 라벨과 같은 색)
const ACTION = { buy: ["매수", "ui-label--pos"], sell: ["매도", "ui-label--neg"], hold: ["보유", "ui-label--neutral"], watch: ["관심", "ui-label--warn"] };

function fmtPrice(p) {
  const n = Number(p.targetPrice).toLocaleString("ko-KR");
  return p.currency === "USD" ? `$${n}` : p.currency === "KRW" ? `${n}원` : `${n} ${p.currency || ""}`.trim();
}

/** 의견 라벨 · (종목) · 목표주가 · 말한 사람 한 줄. withName: 요약 안에서는 종목 이름도 적는다 */
function pickHead(p, withName) {
  return h("div", { class: "pick-head" },
    label(ACTION[p.action] || ACTION.watch),
    withName ? h("span", { class: "sum-topic" }, p.name, p.ticker && p.ticker !== p.name ? h("span", { class: "ui-num sum-tickers pick-ticker" }, p.ticker) : null) : null,
    p.targetPrice ? h("span", { class: "ui-num" }, `목표 ${fmtPrice(p)}`) : null,
    h("span", { class: "ui-sub" }, `${p.speaker || "말한 사람 모름"}${p.source === "report" ? " · 리포트" : ""}`));
}

/** 추천 종목 탭의 종목 하나: 이름 · 티커, 의견 수, 방송별 의견 (최신순) */
function pickGroup(list) {
  const p0 = list[0];
  const buys = list.filter((p) => p.action === "buy").length;
  return h("article", { class: "sum-item" },
    h("div", { class: "sum-title" }, p0.name, p0.ticker && p0.ticker !== p0.name ? h("span", { class: "ui-num sum-tickers pick-ticker" }, p0.ticker) : null),
    h("small", { class: "ui-num ui-sub" }, [`의견 ${list.length}건`, buys ? `매수 ${buys}` : null].filter(Boolean).join(" · ")),
    ...list.map((p) => {
      const pp = p.s.publishedAt ? parts(p.s.publishedAt) : null;
      const url = `https://www.youtube.com/watch?v=${p.s.id}${p.atSec != null ? `&t=${p.atSec}s` : ""}`;
      return h("div", { class: "pick-row" },
        pickHead(p, false),
        p.reason ? h("p", { class: "sum-note" }, p.reason) : null,
        pickPerfLine(p),
        h("small", { class: "ui-num ui-sub" }, [p.s.channelName, pp ? `${pp.month}.${pp.day} ${pp.hour}:${pp.minute} 게시` : null].filter(Boolean).join(" · "),
          " · ", h("a", { class: "ui-link", href: url, target: "_blank", rel: "noopener" }, "영상에서 보기")));
    }));
}

/** focusCat: 분류 라벨 탭에서 고른 분류. 그 분류 이슈를 앞으로 놓고 라벨을 눌린 모양으로 */
function summaryView(s, focusCat = null) {
  const list = [...(s.issues || [])];
  if (focusCat) list.sort((a, b) => (b.category === focusCat) - (a.category === focusCat));
  // 분류 라벨은 버튼 (가이드 10-1: 누르면 그 목록으로) → 최근 48시간의 그 분류 영상
  const issues = list.map((i) => h("div", { class: "sum-issue" },
    h("div", { class: "sum-topic" }, i.topic || ""),
    h("div", { class: "sum-issue-head" },
      h("button", { class: "ui-label cat-label", type: "button", "aria-current": String(i.category === focusCat),
        title: "이 분류만 보기", onclick: () => openCategory(i.category) }, CATEGORY_NAME[i.category] || i.category),
      label(STANCE[i.stance] || STANCE.neutral)),
    i.note ? h("p", { class: "sum-note" }, i.note) : null,
    i.tickers?.length ? h("small", { class: "ui-num sum-tickers" }, i.tickers.join(" · ")) : null));
  const picks = (s.picks || []).map((p) => h("div", { class: "sum-issue" }, pickHead(p, true), p.reason ? h("p", { class: "sum-note" }, p.reason) : null));
  const line = (name, xs, cls) => (xs?.length
    ? h("p", { class: "sum-line" }, h("span", { class: "ui-sub" }, `${name} : `), h("span", { class: cls }, xs.join(" · "))) : null);
  return [
    h("p", { class: "sum-text" }, s.summary || ""),
    issues.length ? h("div", { class: "ui-subhead" }, "이슈") : null,
    ...issues,
    picks.length ? h("div", { class: "ui-subhead" }, "종목 의견") : null,
    ...picks,
    line("종목", s.tickers, "sum-tickers"),
    line("키워드", s.keywords),
    line("발언", s.speakers),
  ].filter(Boolean);
}

function videoRow(v) {
  const sum = summariesCache.get(v.id);
  const actions = [
    v.status === "done" ? h("button", { class: "ui-btn-outline", type: "button", onclick: () => openText(v) }, sum ? "요약 보기" : "텍스트 보기") : null,
    v.status !== "pending" ? h("button", { class: "ui-btn-outline", type: "button", onclick: (e) => reprocess(v, e.currentTarget) }, "재수집") : null,
  ].filter(Boolean);
  return h("div", { class: "ui-row ui-row--top rec-row" },
    h("span", { class: "ui-row-name" }, titleWithLink(sum?.titleKo || v.title || v.id, `https://www.youtube.com/watch?v=${v.id}`, "영상 열기"),
      h("small", { class: "ui-num" }, [fmtTime(v.processedAt || v.updatedAt), v.channelName].filter(Boolean).join(" · ")),
      sum?.summary ? h("span", { class: "ui-small row-summary" }, sum.summary) : null,
      v.error ? h("span", { class: "ui-row-error" }, v.error) : null,
      actions.length ? h("span", { class: "ui-row-actions" }, actions) : null),
    h("span", { class: "ui-row-value" }, label(VIDEO_STATUS[v.status])));
}

async function reprocess(v, btn) {
  const ok = await ask({
    title: "재수집",
    text: `'${v.title || v.id}' 영상을 다시 받아쓸까요? 기존 텍스트는 새 결과로 바뀝니다.`,
    ok: "재수집",
  });
  if (!ok) return;
  await withBusy(btn, "요청 중…", async () => {
    try {
      // deferDay · topicFiltered가 남아 있으면 수집기가 기한 지남 · 주제 무관으로 다시 건너뛴다
      await updateDoc(doc(db, "videos", v.id), {
        status: "pending", attempts: 0, error: null, deferDay: null, topicFiltered: false, updatedAt: serverTimestamp(),
      });
      const ref = await addDoc(collection(db, "jobs"), {
        type: "reprocess", videoId: v.id, status: "pending", requestedAt: serverTimestamp(),
      });
      toast("재수집을 요청했습니다.");
      watchDispatch(ref);
    } catch (ex) {
      toast(`요청하지 못했습니다. ${ex.message}`, true);
    }
  });
}

// ---------- 텍스트 보기 ----------

let currentText = { title: "", body: "", summary: "", pane: "text" };

/** 요약 | 자막 탭. 복사 · 다운로드는 보고 있는 쪽을 대상으로 한다 */
function showPane(pane) {
  currentText.pane = pane;
  document.querySelectorAll("#text-tabs .ui-subtab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.pane === pane)));
  $("#text-summary").hidden = pane !== "summary";
  $("#text-body").hidden = pane !== "text";
  $("#text-dialog .ui-pop-body").scrollTop = 0;
}
document.querySelectorAll("#text-tabs .ui-subtab").forEach((t) => t.addEventListener("click", () => showPane(t.dataset.pane)));

/** 복사 · 다운로드용 요약 글 */
function summaryPlain(s) {
  const issues = (s.issues || []).map((i) => [
    `- [${CATEGORY_NAME[i.category] || i.category} · ${(STANCE[i.stance] || STANCE.neutral)[0]}] ${i.topic || ""}`,
    i.note ? `  ${i.note}` : "",
    i.tickers?.length ? `  ${i.tickers.join(" · ")}` : "",
  ].filter(Boolean).join("\n"));
  return [
    s.summary || "",
    issues.length ? `\n이슈\n${issues.join("\n")}` : "",
    s.tickers?.length ? `\n종목 : ${s.tickers.join(" · ")}` : "",
    s.keywords?.length ? `키워드 : ${s.keywords.join(" · ")}` : "",
  ].filter(Boolean).join("\n");
}

async function openText(v, pane = "summary") {
  const snap = await getDoc(doc(db, "transcripts", v.id));
  if (!snap.exists()) return toast("저장된 텍스트가 없습니다.", true);
  const sum = summariesCache.get(v.id);
  currentText = { title: sum?.titleKo || v.title || v.id, body: snap.data().text || "", summary: sum ? summaryPlain(sum) : "", pane: "text" };
  $("#text-title").textContent = currentText.title;
  $("#text-body").textContent = currentText.body;
  $("#text-summary").replaceChildren(...(sum ? summaryView(sum) : []));
  $("#text-tabs").hidden = !sum;
  $("#text-dialog").showModal();
  showPane(sum && pane === "summary" ? "summary" : "text");
}

$("#text-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText(currentText.pane === "summary" ? currentText.summary : currentText.body);
  toast("복사했습니다.");
});
$("#text-download").addEventListener("click", () => {
  const isSum = currentText.pane === "summary";
  const blob = new Blob([isSum ? currentText.summary : currentText.body], { type: "text/plain;charset=utf-8" });
  const a = h("a", { href: URL.createObjectURL(blob), download: `${isSum ? "(요약) " : ""}${currentText.title.replace(/[\\/:*?"<>|]/g, "")}.txt` });
  a.click();
  URL.revokeObjectURL(a.href);
  toast("다운로드했습니다.");
});

// ---------- 시험 결과 (trials) ----------
// 시험 실행이 남긴 본문 (worker/private.py). 목록 문서에는 본문이 없고, 눌렀을 때 parts를 읽는다

const TRIAL_PAGE = 10;
const TRIAL_KIND = { summarize: "요약", captions: "자막 받기", transcribe: "받아쓰기", picks: "종목 의견", prices: "수익률", commentary: "시황 해설" };
let trialsCache = [];
let trialsExpanded = false;
let trialsListening = false;

function listenTrials() {
  if (trialsListening) return;
  trialsListening = true;
  const q = query(collection(db, "trials"), orderBy("createdAt", "desc"), limit(30));
  unsubscribers.push(onSnapshot(q, (snap) => {
    trialsCache = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    renderTrials();
  }, onListenError));
  unsubscribers.push(() => { trialsListening = false; });
}
$("#trial-fold").addEventListener("click", () => { trialsExpanded = !trialsExpanded; renderTrials(); });

function renderTrials() {
  const box = $("#trial-rows");
  if (!trialsCache.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, "아직 시험 결과가 없습니다."));
    $("#trial-fold").hidden = true;
    return;
  }
  const shown = trialsExpanded ? trialsCache : trialsCache.slice(0, TRIAL_PAGE);
  box.replaceChildren(...shown.map((t) => h("button", {
    type: "button", class: "ui-row ui-row--top rec-row run-row", onclick: () => openTrial(t),
  },
  h("span", { class: "ui-row-name" }, t.title || t.id,
    h("small", { class: "ui-num" }, [fmtTime(t.createdAt), `${(t.chars || 0).toLocaleString("ko-KR")}자`].filter(Boolean).join(" · "))),
  h("span", { class: "ui-row-value" }, label([TRIAL_KIND[t.kind] || t.kind || "시험", "ui-label--neutral"])))));
  foldButton($("#trial-fold"), trialsCache.length, TRIAL_PAGE, trialsExpanded, "시험 결과");
}

async function openTrial(t) {
  let snap;
  try {
    snap = await getDocs(collection(db, "trials", t.id, "parts"));
  } catch (e) {
    return toast(`불러올 수 없습니다. ${e.message}`, true);
  }
  const body = snap.docs.slice().sort((a, b) => a.id.localeCompare(b.id)).map((d) => d.data().text || "").join("");
  if (!body) return toast("저장된 내용이 없습니다.", true);
  currentText = { title: t.title || t.id, body, summary: "", pane: "text" };
  $("#text-title").textContent = currentText.title;
  $("#text-body").textContent = body;
  $("#text-summary").replaceChildren();
  $("#text-tabs").hidden = true;
  $("#text-dialog").showModal();
  showPane("text");
}

// ---------- 로그 ----------

const RUN_PAGE = 3;
const TRIGGER_LABEL = { schedule: "예약", workflow_dispatch: "수동", local: "로컬" };
let runsCache = [];
let runsExpanded = false;
let selectedRun = null;
let latestRunId = null;
let logUnsub = null;

function listenRuns() {
  const q = query(collection(db, "runs"), orderBy("startedAt", "desc"), limit(30));
  unsubscribers.push(onSnapshot(q, (snap) => {
    runsCache = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    // 새 실행이 생기면 자동으로 그 실행을 보여 준다
    if (runsCache.length && runsCache[0].id !== latestRunId) {
      latestRunId = runsCache[0].id;
      selectRun(latestRunId);
    }
    renderRuns();
  }, onListenError));
  unsubscribers.push(() => {
    if (logUnsub) logUnsub();
    logUnsub = null;
    selectedRun = null;
    latestRunId = null;
  });
}
$("#run-fold").addEventListener("click", () => { runsExpanded = !runsExpanded; renderRuns(); });

function renderRuns() {
  const box = $("#run-rows");
  if (!runsCache.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, "아직 실행 기록이 없습니다."));
    $("#run-fold").hidden = true;
    return;
  }
  const shown = runsExpanded ? runsCache : runsCache.slice(0, RUN_PAGE);
  box.replaceChildren(...shown.map((r) => {
    const c = r.counts || {};
    // counts는 이번 실행에서 상태를 바꾼 영상 수 (10-27부터 '이미 처리됨' · 중복은 건너뜀에 넣지 않음)
    const nums = [TRIGGER_LABEL[r.trigger] || r.trigger, `완료 ${c.done || 0}`, `대기 ${c.pending || 0}`,
      `건너뜀 ${c.skipped || 0}`, `실패 ${c.failed || 0}`];
    if (r.ytRequests !== undefined) nums.push(`유튜브 요청 ${r.ytRequests}번`);
    return h("button", {
      type: "button", class: "ui-row ui-row--top rec-row run-row",
      "aria-current": r.id === selectedRun ? "true" : null,
      onclick: () => { selectRun(r.id); renderRuns(); },
    },
    h("span", { class: "ui-row-name ui-num" }, fmtTime(r.startedAt),
      h("small", {}, nums.join(" · ")),
      r.note ? h("span", { class: "ui-row-error" }, r.note) : null),
    h("span", { class: "ui-row-value" }, label(runLabel(r))));
  }));
  foldButton($("#run-fold"), runsCache.length, RUN_PAGE, runsExpanded, "실행");
}

/** 실행은 끝까지 돌았지만 실패한 영상이 있으면 '완료' 대신 '일부 실패' */
function runLabel(r) {
  if (r.status === "done" && r.counts?.failed) return ["일부 실패", "ui-label--warn"];
  return RUN_STATUS[r.status];
}

// 로그 줄 거르기: 전체 · 오류 · 건너뜀(대기 포함). 오류 원문(detail)이 있는 줄은 눌러서 펼친다
let logDocs = [];
let logFilter = "";
const logSegs = [...document.querySelectorAll("#log-segs .ui-seg")];
logSegs.forEach((s) => s.addEventListener("click", () => {
  logFilter = s.dataset.filter;
  logSegs.forEach((x) => x.setAttribute("aria-pressed", String(x === s)));
  renderLogLines();
}));

function selectRun(runId) {
  if (selectedRun === runId) return;
  selectedRun = runId;
  if (logUnsub) logUnsub();
  logDocs = [];
  renderLogMeta();
  $("#log-lines").replaceChildren(h("div", { class: "ui-empty" }, "불러오는 중…"));
  const q = query(collection(db, "runs", runId, "logs"), orderBy("createdAt"));
  logUnsub = onSnapshot(q, (snap) => {
    logDocs = snap.docs.map((d) => d.data());
    renderLogMeta();
    renderLogLines();
  }, onListenError);
}

function renderLogMeta() {
  const run = runsCache.find((r) => r.id === selectedRun);
  const errors = logDocs.filter((l) => l.level === "error").length;
  $("#log-meta").textContent = [run ? fmtTime(run.startedAt) : "", errors ? `오류 ${errors}` : ""].filter(Boolean).join(" · ");
}

function renderLogLines() {
  const box = $("#log-lines");
  if (!logDocs.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, "아직 로그가 없습니다."));
    return;
  }
  const list = logFilter ? logDocs.filter((l) => l.level === logFilter) : logDocs;
  if (!list.length) {
    box.replaceChildren(h("div", { class: "ui-empty" }, logFilter === "error" ? "오류가 없습니다." : "해당하는 줄이 없습니다."));
    return;
  }
  box.replaceChildren(...list.map((l) => {
    const head = [
      h("span", { class: "log-time ui-sub ui-num" }, fmtClock(l.createdAt)),
      h("span", { class: "log-msg" }, l.message),
    ];
    if (!l.detail) return h("div", { class: `log-line log-${l.level}` }, ...head);
    const chev = icon("i-chev");
    chev.setAttribute("class", "ui-chev log-chev");
    return h("details", { class: `log-line log-${l.level}` },
      h("summary", {}, ...head, chev),
      h("pre", { class: "log-detail" }, l.detail));
  }));
}

// ---------- 수집 현황 (설정 · 로그) ----------
// 묶음 = 전날 12:00 ~ 당일 11:59 (수집기 config.edition_day). 영상의 묶음은
// 완료 → 받은 시각(processedAt), 대기 · 기한 지남 → 미룬 묶음(deferDay), 나머지 → 마지막 변경 시각으로 정한다

const SKIP_REASONS = [
  ["주제 무관", (v, e) => v.topicFiltered || e.startsWith("주제 무관")],
  ["상한 · 보류로 기한 지남", (v, e) => e.includes("기한 지남")],
  ["자막 없고 길이 초과", (v, e) => e.includes("Gemini 상한")],
  ["비공개 · 삭제", (v, e) => e.includes("비공개")],
];

let statVideos = null;
let statEdition = null;   // 구독할 때의 오늘 묶음
let statDay = 0;          // 0 = 오늘 묶음, 1 = 어제 묶음
let statUnsub = null;
const statOpen = new Set();   // 펼친 목록 (다시 그려도 유지)

function editionOf(ts) {
  const d = ts?.toDate ? ts.toDate() : new Date(ts);
  return new Date(d.getTime() + 21 * 3600 * 1000).toISOString().slice(0, 10);   // 한국 시간 + 12시간
}
function shiftDay(ymd, n) {
  const d = new Date(`${ymd}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function videoEdition(v) {
  if (v.status === "done" && v.processedAt) return editionOf(v.processedAt);
  if (v.deferDay) return v.deferDay;
  return editionOf(v.updatedAt || Date.now());
}

/** 어제 묶음 시작(그제 12:00 한국 시간 = 그제 03:00 UTC)부터 바뀐 영상을 모두 받는다 */
function listenStats() {
  if (statUnsub) { statUnsub(); unsubscribers = unsubscribers.filter((u) => u !== statUnsub); }
  statEdition = editionOf(Date.now());
  statVideos = null;
  const from = new Date(`${shiftDay(statEdition, -2)}T03:00:00Z`);
  const q = query(collection(db, "videos"), where("updatedAt", ">=", from), orderBy("updatedAt", "desc"));
  statUnsub = onSnapshot(q, (snap) => {
    statVideos = snap.docs.map((d) => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) }));
    renderStats();
  }, onListenError);
  unsubscribers.push(statUnsub);
}

document.querySelectorAll("#stat-segs .ui-seg").forEach((s) => s.addEventListener("click", () => {
  statDay = Number(s.dataset.day);
  document.querySelectorAll("#stat-segs .ui-seg").forEach((x) => x.setAttribute("aria-pressed", String(x === s)));
  renderStats();
}));

/** 접히는 목록 (가이드 6-1 접기) */
function statFold(key, title, rows) {
  const chev = icon("i-chev");
  chev.setAttribute("class", "ui-chev");
  const el = h("details", { class: "ui-fold stat-fold", open: statOpen.has(key) },
    h("summary", {}, h("span", {}, title), chev), ...rows);
  el.addEventListener("toggle", () => { if (el.open) statOpen.add(key); else statOpen.delete(key); });
  return el;
}

// meta/youtube: 오늘 묶음 받기 시도 수(dayCounts · chCounts) · 묶음 한도(dayLimits) · 채널 몫(shares). 수집기 Collector.save_counts
let ytMeta = null;
function listenYtMeta() {
  unsubscribers.push(onSnapshot(doc(db, "meta", "youtube"), (snap) => {
    ytMeta = snap.exists() ? snap.data() : null;
    renderStats();
  }, onListenError));
}

const GROUPS = [["main", "미국 채널"], ["kr", "한국 채널"], ["required", "필수 채널"]];
const channelGroup = (c) => (c.required ? "required" : c.limitGroup || "main");

/** 채널별 완료: 묶음마다 모든 채널을 0개까지 보이고, 한도 · 몫을 함께 적는다 (몫은 지금 설정 기준) */
const editionMinute = (h, m) => ((h - 12 + 24) % 24) * 60 + m;   // 묶음 시작(낮 12시)부터 지난 분 (collector.edition_minute)

/** 완료 0개인 채널의 이유. 그 묶음의 영상 상태 → 중지 → 수집 시각 · 마지막 확인 순으로 본다 */
function zeroReason(c, day, today, by) {
  const mine = (list) => list.filter((v) => v.channelId === c.id);
  const why = [];
  const pend = mine(by.pending), fail = mine(by.failed), skip = mine(by.skipped);
  if (pend.length) why.push(`대기 ${pend.length}개 (한도 · 몫 · 유튜브 쉼)`);
  if (fail.length) why.push(`실패 ${fail.length}개`);
  if (skip.length) {
    const names = new Map();
    for (const v of skip) {
      const name = (SKIP_REASONS.find(([, test]) => test(v, v.error || "")) || ["기타"])[0];
      names.set(name, (names.get(name) || 0) + 1);
    }
    why.push(`건너뜀 ${[...names].map(([k, n]) => `${k} ${n}`).join(", ")}`);
  }
  if (why.length) return why.join(" · ");
  if (c.enabled === false) return "중지된 채널";
  if (!today) return "새 영상 없음";
  const slots = (c.times || []).map((t) => t.split(":").map(Number)).map(([h, m]) => [editionMinute(h, m), `${h}`.padStart(2, "0") + `:${`${m}`.padStart(2, "0")}`]);
  const kst = new Date(Date.now() + 9 * 3600 * 1000);
  const nowMin = editionMinute(kst.getUTCHours(), kst.getUTCMinutes());
  if (slots.length && slots.every(([m]) => m > nowMin)) {
    return `수집 시각 전 (${slots.sort((a, b) => a[0] - b[0])[0][1]}부터)`;
  }
  const start = new Date(`${shiftDay(day, -1)}T03:00:00Z`);
  const last = c.lastRunAt?.toDate ? c.lastRunAt.toDate() : null;
  if (last && last >= start) return `새 영상 없음 (마지막 확인 ${fmtTime(c.lastRunAt)})`;
  return `아직 확인 못 함${last ? ` (마지막 확인 ${fmtTime(c.lastRunAt)})` : ""}`;
}

function channelDoneRows(done, pending, today, day, by) {
  const tally = (list) => list.reduce((m, v) => m.set(v.channelId, (m.get(v.channelId) || 0) + 1), new Map());
  const doneBy = tally(done), pendBy = tally(pending);
  const limits = ytMeta?.dayLimits || {}, shares = ytMeta?.shares || {};
  const shown = channelsCache.filter((c) => c.enabled !== false || doneBy.has(c.id));
  const groups = [...GROUPS, ...[...new Set(shown.map(channelGroup))].filter((g) => !GROUPS.some(([k]) => k === g)).map((g) => [g, g])];
  const rows = [];
  for (const [g, name] of groups) {
    const list = shown.filter((c) => channelGroup(c) === g);
    if (!list.length) continue;
    const sum = list.reduce((n, c) => n + (doneBy.get(c.id) || 0), 0);
    const cap = g === "required" ? list.reduce((n, c) => n + (c.dailyLimit || 1), 0) : limits[g];
    const freed = today && ytMeta?.sharesReleased?.includes(g) ? " · 몫 풀림" : "";
    rows.push(h("div", { class: "ui-subhead ui-num" }, `${name} ${sum}${cap ? `/${cap}` : ""}개${g === "required" ? " (목표, 상한 없음)" : ""}${freed}`));
    list.sort((a, b) => (doneBy.get(b.id) || 0) - (doneBy.get(a.id) || 0) || (a.name || "").localeCompare(b.name || "", "ko"));
    for (const c of list) {
      const n = doneBy.get(c.id) || 0, share = g === "required" ? c.dailyLimit || 1 : shares[c.id];
      const wait = today && pendBy.get(c.id) ? ` · 대기 ${pendBy.get(c.id)}` : "";
      rows.push(h("div", { class: `ui-row ${n ? "" : "ui-row--top"}` },
        h("span", { class: "ui-row-name" }, c.name || c.id, c.enabled === false ? h("span", { class: "ui-paren" }, " (중지)") : null,
          n ? null : h("small", { class: "ui-sub stat-why" }, zeroReason(c, day, today, by))),
        h("span", { class: `ui-row-value ui-num ${n ? "" : "ui-sub"}` }, `${n}${share ? `/${share}` : ""}개${wait}`)));
    }
  }
  const lost = done.filter((v) => !channelsCache.some((c) => c.id === v.channelId)).length;
  if (lost) rows.push(...countRows([["삭제한 채널", lost]]));
  return rows;
}

function countRows(entries) {
  return entries.map(([name, n]) => h("div", { class: "ui-row" },
    h("span", { class: "ui-row-name" }, name), h("span", { class: "ui-row-value ui-num" }, `${n}개`)));
}

function renderStats() {
  if (statEdition && editionOf(Date.now()) !== statEdition) return listenStats();   // 낮 12시가 지나 묶음이 바뀜
  if (!statVideos) return;
  const day = shiftDay(statEdition, -statDay);
  $("#stat-range").textContent = `${fmtDate(shiftDay(day, -1))} 12:00 ~ ${fmtDate(day)} 11:59 묶음`;
  const by = { done: [], pending: [], skipped: [], failed: [] };
  for (const v of statVideos) if (by[v.status] && videoEdition(v) === day) by[v.status].push(v);
  const noSummary = by.done.filter((v) => v.summaryStatus === "pending" || v.summaryStatus === "failed");

  // 하루 한도 = 묶음 한도 합 + 필수 채널 하루 목표 합 (지금 설정 기준. 어제도 같은 값으로 본다)
  const enabled = channelsCache.filter((c) => c.enabled !== false);
  const limits = ytMeta?.dayLimits;
  const total = limits && Object.values(limits).reduce((a, b) => a + b, 0)
    + enabled.filter((c) => c.required).reduce((n, c) => n + (c.dailyLimit || 1), 0);
  const stat = (name, list, cls = "", cap = 0) => h("div", { class: "ui-stat" },
    h("span", { class: "ui-stat-name" }, name),
    h("span", { class: `ui-stat-value ${list.length ? cls : ""}` }, `${list.length}${cap ? `/${cap}` : ""}개`));
  $("#stat-nums").replaceChildren(
    stat("완료", by.done, "", total), stat("대기", by.pending, "stat-warn"), stat("건너뜀", by.skipped), stat("실패", by.failed, "stat-bad"),
    ...(noSummary.length ? [stat("요약 못 함", noSummary, "stat-warn")] : []));

  const parts = [];
  // 한도는 받기 시도 수로 센다 (실패 포함). 오늘 묶음만 수집기가 남긴 값이 있다
  if (statDay === 0 && ytMeta?.dayKey === day && ytMeta.dayCounts) {
    const used = GROUPS.map(([g, name]) => `${name} ${ytMeta.dayCounts[g] || 0}${limits?.[g] ? `/${limits[g]}` : ""}`);
    parts.push(h("p", { class: "ui-small ui-sub ui-num stat-used" }, `한도 사용 (받기 시도, 실패 포함) : ${used.join(" · ")}`));
  }
  if (by.failed.length) {
    parts.push(h("div", { class: "ui-subhead" }, `실패 ${by.failed.length}개`), ...by.failed.map(videoRow));
  }
  if (noSummary.length) {
    parts.push(h("div", { class: "ui-subhead" }, `요약 못 함 ${noSummary.length}개 (다음 실행에서 다시)`),
      ...noSummary.map((v) => videoRow({ ...v, error: v.summaryError || "요약 대기" })));
  }
  if (channelsCache.length) {
    parts.push(statFold("done", `채널별 완료 ${by.done.length}${total ? `/${total}` : ""}개`, channelDoneRows(by.done, by.pending, statDay === 0, day, by)));
  }
  if (by.skipped.length) {
    const reasons = new Map();
    for (const v of by.skipped) {
      const e = v.error || "";
      const name = (SKIP_REASONS.find(([, test]) => test(v, e)) || ["기타"])[0];
      reasons.set(name, (reasons.get(name) || 0) + 1);
    }
    parts.push(statFold("skipReason", `건너뜀 사유 ${by.skipped.length}개`, countRows([...reasons].sort((a, b) => b[1] - a[1]))),
      statFold("skipped", "건너뛴 영상 보기", by.skipped.map(videoRow)));
  }
  if (by.pending.length) parts.push(statFold("pending", `대기 영상 ${by.pending.length}개 보기`, by.pending.map(videoRow)));
  $("#stat-detail").replaceChildren(...(parts.length ? parts : [h("div", { class: "ui-empty" }, "이 묶음에 처리한 영상이 없습니다.")]));
}

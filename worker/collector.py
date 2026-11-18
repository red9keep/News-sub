"""수집기 진입점. GitHub Actions(collect.yml)가 실행한다.

순서: 요청 읽기 → 예약 시각이 지난 채널 처리 → 대기(pending) 영상 처리 → 기록 마무리
영상마다: 유튜브 자막(captions) → 자막이 없을 때만 Gemini 받아쓰기(transcribe)
"""
import os
import random
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta

import captions
import config
import feed
import store
import reasons
import summarize
import topic
import transcribe
import youtube
from transcribe import QuotaExceeded
import notify


def short(text, n=30):
    return text if len(text) <= n else text[:n] + "…"


def is_due(times, last_run, now):
    """lastRunAt 이후 지금까지 사이에 지난 예약 시각(한국 시간)이 있으면 True."""
    lower = last_run or (now - config.NEW_CHANNEL_LOOKBACK)
    for t in times or []:
        try:
            h, m = (int(x) for x in t.strip().split(":"))
        except ValueError:
            continue
        for days_back in (0, 1):
            cand = (now - timedelta(days=days_back)).replace(hour=h, minute=m, second=0, microsecond=0)
            if lower < cand <= now:
                return True
    return False


def done_today(ch, day_key):
    """이 채널에서 오늘(한국 날짜) 게시된 영상을 몇 개 받았는지."""
    return ch.get("doneCount", 0) if ch.get("doneDay") == day_key else 0


def finished_today(ch, day_key):
    """하루 목표(dailyLimit)만큼 오늘 영상을 받았으면 True. 그날 남은 예약 시각은 건너뛴다."""
    limit = ch.get("dailyLimit")
    return bool(limit) and done_today(ch, day_key) >= limit


def due_channels(channels, now, collect_all):
    day_key = now.strftime("%Y-%m-%d")
    return [(cid, ch) for cid, ch in channels
            if (collect_all or is_due(ch.get("times"), ch.get("lastRunAt"), now))
            and not finished_today(ch, day_key)]


def channel_group(ch):
    """상한 묶음: required(필수, 상한 없음) · limitGroup(예: kr) · main(기본, 미국 채널)."""
    return "required" if ch.get("required") else ch.get("limitGroup") or "main"


def group_limit(group):
    return config.GROUP_DAY_LIMITS.get(group, config.MAX_CAPTIONS_PER_DAY)


def channel_shares(channels):
    """채널 ID → 하루 몫. dayMax가 있으면 그 값, 없으면 묶음 상한 ÷ 묶음 안 채널 수(올림). 필수 채널은 몫 없음."""
    sizes = Counter(channel_group(ch) for ch in channels.values())
    return {cid: int(ch.get("dayMax") or 0) or -(-group_limit(g) // sizes[g])
            for cid, ch in channels.items() if (g := channel_group(ch)) != "required"}


def edition_minute(h, m):
    """묶음 시작(낮 12시)부터 지난 분. 예약 시각 22:20 → 620, 07:20 → 1160."""
    return (h - 12) % 24 * 60 + m


def released_groups(channels, now):
    """마지막 예약 시각에서 SHARE_RELEASE_AFTER_MIN분이 지나 채널 몫을 푼 묶음.
    그 뒤로는 몫을 다 쓴 채널의 대기 영상도 묶음 상한이 남은 만큼 받는다."""
    last = {}
    for ch in channels.values():
        for t in ch.get("times") or []:
            try:
                h, m = (int(x) for x in t.strip().split(":"))
            except ValueError:
                continue
            g = channel_group(ch)
            last[g] = max(last.get(g, 0), edition_minute(h, m))
    now_min = edition_minute(now.hour, now.minute)
    return {g for g, m in last.items() if now_min >= m + config.SHARE_RELEASE_AFTER_MIN}


def captions_mode(now, yt_state):
    """자막 받기 가능 여부. 반환: ("use" | "off" | "defer", 보류 이유)."""
    if config.CAPTIONS_MODE == "off":
        return "off", None
    if config.CAPTIONS_MODE == "home" and not config.YT_SOURCE_ADDRESS:
        return "defer", "집 공유기 VPN 연결이 확인되지 않음"
    until = yt_state.get("blockedUntil")
    if until and until > now:
        return "defer", f"유튜브 차단으로 {until.astimezone(config.KST):%m-%d %H:%M}까지 쉬는 중"
    return "use", None


def cooldown_after_block(now, yt_state):
    """이번 차단이 연속 몇 번째인지와 쉬는 시간이 끝나는 시각."""
    last = yt_state.get("lastBlockAt")
    streak = yt_state.get("blockStreak", 0) + 1 if last and now - last < timedelta(hours=24) else 1
    hours = config.BLOCK_COOLDOWN_HOURS[min(streak, len(config.BLOCK_COOLDOWN_HOURS)) - 1]
    return streak, now + timedelta(hours=hours)


class Collector:
    def __init__(self, run, now, yt_state, required=(), channels=None):
        self.run = run
        self.channels = channels or {}  # 채널 ID → 설정 (오늘 받은 수를 세기 위해)
        self.required = set(required)  # 필수 채널 ID: 상한 · 주제 걸러내기 없이 먼저 받는다
        self.now = now
        self.handled = set()
        self.summarized = set()  # 이번 실행에서 요약을 시도한 영상 (밀린 요약 재시도와 겹치지 않게)
        self.yt_state = yt_state
        self.yt, self.defer_reason = captions_mode(now, yt_state)
        self.blocked_until = None
        # Slack 알림용 (notify.run_digest): 기다림(라이브 직후 · Gemini 혼잡) · 보류(VPN · 차단) · 받기 실패 · 요약 실패
        self.waits = 0
        self.yt_deferred = 0
        self.fetch_failed = 0
        self.summary_failed, self.summary_cause = 0, None
        self.llm_day_limit = False   # Gemini 요약 하루 한도를 만나면 이번 회차엔 더 요약하지 않는다
        # 받는 자막 수 상한 (실행당 · 하루). 하루 수는 meta/youtube에 한국 날짜와 함께 남긴다
        self.taken = 0
        self.day_key = config.edition_day(now)  # 상한 · 미룬 날 기준. 밤에 미룬 영상이 자정에 버려지지 않게
        self.today = now.strftime("%Y-%m-%d")  # 필수 채널 하루 목표 기준 (달력 날짜)
        same_day = yt_state.get("dayKey") == self.day_key
        self.day_count = yt_state.get("dayCount", 0) if same_day else 0
        # 상한 묶음별 하루 수: main(기본, 미국 채널) · kr 등 limitGroup · required(필수, 상한 없음)
        self.day_counts = dict(yt_state.get("dayCounts") or {}) if same_day else {}
        # 채널별 하루 받기 시도 수와 몫 (한 채널이 묶음 상한을 몰아 쓰지 않게)
        self.ch_counts = dict(yt_state.get("chCounts") or {}) if same_day else {}
        self.shares = channel_shares(self.channels)
        self.released = released_groups(self.channels, now)
        self.save_counts()

    def group_of(self, ch_id):
        if ch_id in self.required:
            return "required"
        return (self.channels.get(ch_id) or {}).get("limitGroup") or "main"

    def limit_reason(self, group, ch_id):
        if self.taken >= config.MAX_CAPTIONS_PER_RUN:
            return f"이번 실행 상한 {config.MAX_CAPTIONS_PER_RUN}개"
        limit = group_limit(group)
        if self.day_counts.get(group, 0) >= limit:
            return f"오늘 상한 {limit}개" + (f" ({group})" if group != "main" else "")
        share = self.shares.get(ch_id)
        # 직접 정한 몫(dayMax)은 몫을 푼 뒤에도 지킨다 (11-12, 폭스 정치 영상 줄이기). 자동 몫만 푼다
        fixed = bool((self.channels.get(ch_id) or {}).get("dayMax"))
        if share and (fixed or group not in self.released) and self.ch_counts.get(ch_id, 0) >= share:
            return f"채널 하루 몫 {share}개"
        return None

    def take(self, group, ch_id):
        """자막 받기(또는 Gemini)를 시작할 때 센다. 실패해도 요청은 나갔으므로 센다."""
        self.taken += 1
        self.day_count += 1
        self.day_counts[group] = self.day_counts.get(group, 0) + 1
        if ch_id:
            self.ch_counts[ch_id] = self.ch_counts.get(ch_id, 0) + 1
        self.save_counts()

    def save_counts(self):
        """meta/youtube에 오늘 수와 상한 · 몫을 남긴다 (웹 수집 현황이 읽는다)."""
        groups = {"main", "required", *config.GROUP_DAY_LIMITS, *self.day_counts}
        store.set_youtube_counts(
            dayKey=self.day_key, dayCount=self.day_count,
            dayCounts={g: self.day_counts.get(g, 0) for g in groups},
            chCounts=self.ch_counts,
            dayLimits={g: group_limit(g) for g in groups if g != "required"},
            shares=self.shares, sharesReleased=sorted(self.released))

    def process_channel(self, ch_id, ch):
        name = ch.get("name", "이름없음")
        if ch.get("playlistId"):
            kind, src = "playlist", ch["playlistId"]
        else:
            kind, src = youtube.resolve_source(ch["url"])
            if kind == "playlist":
                store.update_channel(ch_id, playlistId=src)

        ids = [src] if kind == "video" else youtube.latest_video_ids(src, int(ch.get("count", 5)))
        details = youtube.video_details(ids)
        required = ch_id in self.required
        self.run.log(f"[{name}] 최신 {len(ids)}개 확인" + (" (필수 채널)" if required else ""))
        if ch.get("titleMatch"):
            ids = self.filter_titles(ids, details, ch_id, name, ch["titleMatch"])
        if not required and config.TOPIC_FILTER:
            self.filter_topics(ids, details, ch_id, name)
        for vid in ids:
            self.process_video(vid, details.get(vid), ch_id, name, forced=False, required=required)
        # 한도 초과로 중간에 멈추면 여기까지 오지 않으므로, 다음 실행에서 이 채널을 다시 처리한다
        store.update_channel(ch_id, lastRunAt=self.now)

    def filter_titles(self, ids, details, ch_id, ch_name, pattern):
        """채널 titleMatch(정규식, 대소문자 무시)가 제목에 없는 영상은 받지 않는다 (11-13, 하나증권 모닝브리프처럼
        채널 업로드에 섞여 올라오는 코너만 받기). 처음 보는 것만 건너뜀으로 기록해 다음 실행에서 다시 보지 않는다."""
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            self.run.log(f"[{ch_name}] titleMatch 정규식 오류, 거르지 않음 ({e})", "error")
            return ids
        keep = []
        for vid in ids:
            info = details.get(vid)
            if not info or rx.search(info.get("title") or ""):
                keep.append(vid)   # 정보가 없으면(공개 전 등) process_video가 기다렸다 다시 본다
                continue
            if vid in self.handled or store.get_video(vid) is not None:
                continue
            self.handled.add(vid)
            store.set_video(vid, channelId=ch_id, channelName=ch_name, title=info["title"],
                            url=f"https://www.youtube.com/watch?v={vid}", publishedAt=info.get("publishedAt"),
                            durationSec=info.get("durationSec"), status="skipped", titleFiltered=True,
                            error=f"제목 거르기: '{pattern}' 없음")
        n = len(ids) - len(keep)
        if n:
            self.run.log(f"[{ch_name}] 제목에 '{pattern}' 없는 영상 {n}개 건너뜀")
        return keep

    def filter_topics(self, ids, details, ch_id, ch_name):
        """처음 보는 영상만 제목 · 설명으로 판정해, 글감과 관계없으면 자막을 받지 않고 건너뜀으로 기록한다."""
        fresh = {vid: details[vid] for vid in ids
                 if vid not in self.handled and details.get(vid) and details[vid]["live"] == "none"
                 and store.get_video(vid) is None}
        if not fresh:
            return
        verdicts = topic.classify(fresh)
        if not verdicts:
            self.run.log(f"[{ch_name}] 주제 판정 실패, 걸러내지 않고 모두 받습니다 ({topic.last_error})", "error")
            return
        for vid, (keep, why) in verdicts.items():
            if keep:
                continue
            info = fresh[vid]
            self.handled.add(vid)
            store.set_video(vid, channelId=ch_id, channelName=ch_name, title=info["title"],
                            url=f"https://www.youtube.com/watch?v={vid}", publishedAt=info.get("publishedAt"),
                            durationSec=info["durationSec"], status="skipped", topicFiltered=True,
                            error=f"주제 무관: {why}")
            self.run.log(f"[{ch_name}] 주제 무관이라 건너뜀 ({why}): {short(info['title'])}", "skip")
            self.run.count("skipped")

    def process_pending(self, required):
        """웹에서 재수집을 요청했거나 상한 · 차단으로 미뤄진 영상. required=True면 필수 채널 것만, False면 나머지만.

        필수 채널이 아닌 영상은 미뤄진 날(deferDay)이 지나면 받지 않는다 (사용자 결정: 넘친 것은 다음 날 받지 않음).
        """
        docs = {}
        for vid in store.pending_video_ids():
            if vid in self.handled:
                continue
            doc = store.get_video(vid) or {}
            if (doc.get("channelId") in self.required) == required:
                docs[vid] = doc
        for vid, doc in list(docs.items()):
            # 상한 · 차단 · 라이브로 미뤄진 영상(deferDay 있음)만. 웹 재수집은 deferDay를 지우므로 오래돼도 받는다
            if doc.get("deferDay") and self.stale(doc.get("publishedAt")):
                self.skip_stale(vid, doc.get("channelName", ""), doc.get("title", vid))
                del docs[vid]
        if not required:
            for vid, doc in list(docs.items()):
                if doc.get("topicFiltered"):
                    # 10-27까지는 주제 무관 영상이 다음 실행에서 다시 대기로 들어갔다. 웹 재수집은 topicFiltered를 지운다
                    store.set_video(vid, status="skipped", deferDay=None)
                    self.run.log(f"[{doc.get('channelName', '')}] 주제 무관 영상이라 대기에서 뺌: "
                                 f"{short(doc.get('title', vid))}", "skip")
                    self.handled.add(vid)
                    del docs[vid]
                elif doc.get("deferDay") and doc["deferDay"] != self.day_key:
                    store.set_video(vid, status="skipped", error=f"{doc['deferDay']}에 상한 · 보류로 못 받아 기한 지남")
                    self.run.log(f"[{doc.get('channelName', '')}] 전날 못 받은 영상이라 받지 않음: "
                                 f"{short(doc.get('title', vid))}", "skip")
                    self.run.count("skipped")
                    self.handled.add(vid)
                    del docs[vid]
        if not docs:
            return
        if self.yt == "defer":
            self.run.log(f"대기 중인 영상 {len(docs)}개는 다음 실행으로 미룹니다 ({self.defer_reason})", "skip")
            self.yt_deferred += len(docs)
            return
        self.run.log(f"대기 중인 {'필수 채널 ' if required else ''}영상 {len(docs)}개 처리")
        details = youtube.video_details(list(docs))
        order = list(docs.items())
        if not required:
            random.shuffle(order)  # 매번 같은 순서로 요청하지 않게 (필수 채널은 그대로)
        for vid, doc in order:
            self.process_video(vid, details.get(vid), doc.get("channelId"),
                               doc.get("channelName", "재수집"), forced=True, required=required)

    def process_video(self, vid, info, ch_id, ch_name, forced, required=False):
        if vid in self.handled and not forced:
            # 같은 영상이 여러 채널(예: Mad Money 재생목록과 CNBC 채널)에 올라온 경우
            self.run.log(f"[{ch_name}] 이번 실행에서 이미 다룬 영상: {vid}", "skip")
            return
        self.handled.add(vid)
        existing = store.get_video(vid) or {}
        title = (info or {}).get("title") or existing.get("title") or vid
        url = f"https://www.youtube.com/watch?v={vid}"
        status = existing.get("status")
        attempts = existing.get("attempts", 0)

        # 이미 끝난 영상은 다시 받지 않고 실행 건수(counts)에도 넣지 않는다.
        # 건너뜀(주제 무관 · 기한 지남 · 길이 초과 등)도 끝난 것으로 본다. 10-27까지는 다음 실행에서 다시 받았다
        if status in ("done", "skipped") and not forced:
            why = "이미 처리됨" if status == "done" else f"이미 건너뛴 영상 ({existing.get('error') or '사유 없음'})"
            self.run.log(f"[{ch_name}] {why}: {short(title)}", "skip")
            return

        if not info:
            # API가 정보를 안 주는 영상. Mad Money는 재생목록에 먼저 올라오고 공개는 나중이라
            # 처음 본 순간 건너뛰면 그 회차를 영영 놓친다 (10-28 ~ 11-11 10회 중 9회). 24시간은 대기로 두고 다시 본다
            first = existing.get("notVisibleSince")
            since = datetime.fromisoformat(first.replace("Z", "+00:00")) if first else self.now
            if self.now - since > timedelta(hours=config.STALE_HOURS):
                store.set_video(vid, channelId=ch_id, channelName=ch_name, title=title,
                                status="skipped", deferDay=None, error="비공개 또는 삭제된 영상")
                self.run.log(f"[{ch_name}] 비공개·삭제 영상 건너뜀 ({config.STALE_HOURS}시간 동안 안 보임): {short(title)}", "skip")
                self.run.count("skipped")
            else:
                store.set_video(vid, channelId=ch_id, channelName=ch_name, title=title, status="pending",
                                deferDay=self.day_key, notVisibleSince=since.isoformat())
                self.run.log(f"[{ch_name}] 아직 공개 전(또는 비공개)이라 다음 실행에서 다시: {short(title)}", "skip")
                self.run.count("pending")
            return

        meta = {"channelId": ch_id, "channelName": ch_name, "title": title, "url": url,
                "publishedAt": info.get("publishedAt"), "durationSec": info["durationSec"]}

        if info["live"] != "none":
            # 라이브·예정 영상은 기록하지 않는다. 방송이 끝나 일반 영상이 되면 다음 실행에서 처리된다
            self.run.log(f"[{ch_name}] 라이브·예정 영상이라 나중에 처리: {short(title)}", "skip")
            return

        if not forced and self.stale(meta["publishedAt"]):
            self.skip_stale(vid, ch_name, title, meta)
            return

        if status == "failed" and attempts >= config.MAX_TOTAL_ATTEMPTS and not forced:
            self.run.log(f"[{ch_name}] {attempts}회 실패한 영상이라 자동 재시도 안 함: {short(title)}", "skip")
            return

        if self.yt == "defer":
            self.defer(vid, meta, ch_name, title)
            return

        group = self.group_of(ch_id)
        limit = None if required else self.limit_reason(group, ch_id)
        if limit:
            self.defer(vid, meta, ch_name, title, limit)
            return
        self.take(group, ch_id)

        result = None
        if self.yt == "use":
            try:
                lang = (self.channels.get(ch_id) or {}).get("language")  # 채널에 적어 둔 말하는 언어 (예: ko)
                yt_info, result = captions.fetch_with_info(vid, language=lang)
                wait = None if result else captions.live_wait_reason(yt_info)
                if wait:
                    # 실패로 세지 않고 대기로 둔다. 다음 실행에서 자동 자막이 생겼으면 그걸 받고, 없으면 Gemini로
                    store.set_video(vid, **meta, status="pending", deferDay=self.day_key)
                    self.run.log(f"[{ch_name}] {wait}이라 다음 실행에서 다시, 대기로 둠: {short(title)}", "skip")
                    self.run.count("pending")
                    self.waits += 1
                    return
            except captions.Blocked as e:
                self.on_blocked(e)
                self.defer(vid, meta, ch_name, title)
                return
            except captions.Unavailable as e:
                # 삭제 · 국가 제한처럼 다시 해도 안 되는 영상은 실패로 되풀이하지 않고 건너뜀으로 둔다
                store.set_video(vid, **meta, status="skipped", error=reasons.describe(e))
                self.run.log(f"[{ch_name}] {e}이라 건너뜀: {short(title)} → 대처: {reasons.advice(e)}", "skip",
                             detail=f"영상 {vid} ({meta.get('url', '')})\n{reasons.text(e)}")
                self.run.count("skipped")
                return
            except captions.CaptionsError as e:
                self.fail(vid, meta, attempts, ch_name, title, e)
                return
            if result:
                self.on_captions_ok()

        try:
            if result:
                text, source, lang = result["text"], f"captions-{result['kind']}", result["language"]
                how = f"{'자막' if result['kind'] == 'manual' else '자동 자막'} ({lang})"
            else:
                minutes = info["durationSec"] // 60
                if info["durationSec"] > config.MAX_LONG_MIN * 60 and not forced:
                    # 60분이 넘으면 30분씩 나눠 받아쓰지만, 3시간이 넘으면 Gemini 호출이 너무 많아 건너뜀
                    store.set_video(vid, **meta, status="skipped",
                                    error=f"자막 없음, 영상 {minutes}분이 나눠 받아쓰기 상한 {config.MAX_LONG_MIN}분 초과"
                                          f" → 건너뜀 (꼭 필요하면 웹에서 재수집하면 상한 없이 나눠 받아씀)")
                    self.run.log(f"[{ch_name}] 자막이 없고 {minutes}분이라 건너뜀 "
                                 f"(나눠 받아쓰기 상한 {config.MAX_LONG_MIN}분, 웹 재수집이면 받아씀): {short(title)}", "skip")
                    self.run.count("skipped")
                    return
                split = f", {config.SEGMENT_MIN}분씩 나눠서" if info["durationSec"] > config.MAX_DURATION_MIN * 60 else ""
                if self.yt == "use":
                    self.run.log(f"[{ch_name}] 자막이 없어 Gemini로 받아쓰기{split}: {short(title)}")
                else:
                    self.run.log(f"[{ch_name}] 받아쓰기 시작{split}: {short(title)}")
                text = transcribe.transcribe(url, log=self.run.log, duration_sec=info["durationSec"])
                source, lang, how = "gemini", None, f"Gemini 받아쓰기 {transcribe.last_model}"
            header = (f"제목: {title}\n채널: {ch_name}\n주소: {url}\n"
                      f"게시일: {info.get('publishedAt', '')}\n출처: {how}")
            path = store.save_transcript(vid, ch_name, title, text, header, source, lang)
        except QuotaExceeded:
            store.set_video(vid, **meta, status="pending")
            self.run.count("pending")
            raise
        except transcribe.Busy as e:
            # Gemini 쪽 혼잡은 실패 횟수(5회 상한)에 넣지 않는다. 다음 실행에서 다시 (필수가 아니면 그날 묶음까지)
            store.set_video(vid, **meta, status="pending", deferDay=self.day_key, error=reasons.describe(e)[:1000])
            self.run.log(f"[{ch_name}] Gemini 혼잡(503)이라 다음 실행에서 다시, 대기로 둠: {short(title)}", "skip",
                         detail=f"영상 {vid} ({url})\n{reasons.text(e)}")
            self.run.count("pending")
            self.waits += 1
            return
        except Exception as e:  # TranscribeError와 업로드 실패 모두 실패로 기록한다 (v1은 업로드 실패를 무시했음)
            self.fail(vid, meta, attempts, ch_name, title, e)
            return

        store.set_video(vid, **meta, status="done", attempts=attempts + 1, error=None,
                        source=source, language=lang, storagePath=path, processedAt=store.now_utc(),
                        summaryStatus="pending" if config.SUMMARIZE else None)
        self.run.log(f"[{ch_name}] 완료 ({how}): {short(title)}", "success")
        self.run.count("done")
        self.count_today(ch_id, info.get("publishedAt"))
        if config.SUMMARIZE:
            self.summarize_video(vid, meta, text)

    def count_today(self, ch_id, published):
        """오늘 게시된 영상을 받으면 채널의 doneDay · doneCount를 올린다 (dailyLimit 판단용)."""
        ch = self.channels.get(ch_id)
        if ch is None or not published:
            return
        if datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(config.KST).strftime("%Y-%m-%d") != self.today:
            return
        n = done_today(ch, self.today) + 1
        ch.update(doneDay=self.today, doneCount=n)
        store.update_channel(ch_id, doneDay=self.today, doneCount=n)
        if finished_today(ch, self.today):
            self.run.log(f"[{ch.get('name', '')}] 오늘 영상 {n}개를 받아 오늘 수집을 마칩니다")

    def quiet_log(self, msg, *secret):
        """summarize에 넘기는 log. 본문이 든 부분(secret)은 Firestore 로그의 detail에만 남긴다.
        저장소가 공개라 실행 로그(print)에는 요약 줄을 찍지 않는다 (11-18, worker/private.py)"""
        self.run.log(msg, detail=" ".join(map(str, secret)) or None)

    def summarize_video(self, vid, meta, text):
        """자막 원문 → summaries/{영상ID}. 실패하면 summaryStatus를 pending으로 두고 다음 실행에서 다시 한다."""
        ch_name, title = meta.get("channelName", ""), meta.get("title", vid)
        self.summarized.add(vid)
        if self.llm_day_limit:
            store.set_video(vid, summaryStatus="pending")
            return False
        try:
            data = summarize.summarize(title, ch_name, text, log=self.quiet_log)
        except summarize.SummarizeError as e:
            store.set_video(vid, summaryStatus="pending", summaryError=reasons.describe(e)[:300])
            self.run.log(f"[{ch_name}] 요약 실패: {short(title)} - {reasons.describe(e)}", "error",
                         detail=f"영상 {vid}\n{reasons.text(e)}")
            if notify.is_day_limit(reasons.text(e)):
                self.llm_day_limit = True
                self.run.log("Gemini 요약 하루 한도를 다 써서 이번 실행에서는 더 요약하지 않습니다", "error")
                notify.gemini_day_limit("요약", len(store.pending_summary_ids(500)))
            else:
                self.summary_failed += 1
                self.summary_cause = reasons.explain(e)
            return False
        try:
            store.save_summary(vid, data, meta)
        except Exception as e:  # Firestore 쓰기 실패: 알리고, 요약은 다음 실행에서 다시
            self.run.log(f"[{ch_name}] 요약 저장 실패: {short(title)} - {reasons.describe(e)}", "error",
                         detail=f"영상 {vid}\n{reasons.text(e)}")
            notify.save_failed(reasons.explain(e))
            try:
                store.set_video(vid, summaryStatus="pending", summaryError=reasons.describe(e)[:300])
            except Exception:
                pass
            return False
        store.set_video(vid, summaryStatus="done", summaryError=None)
        cats = ", ".join(data["categories"]) or "분류 없음"
        picks = f", 종목 의견 {len(data['picks'])}건" if data["picks"] else ""
        self.run.log(f"[{ch_name}] 요약 저장 ({cats}{picks}): {short(title)}")
        return True

    def process_pending_summaries(self):
        """앞선 실행에서 요약하지 못한 영상을 몇 개씩 다시 요약한다."""
        if not config.SUMMARIZE:
            return
        ids = [v for v in store.pending_summary_ids(config.MAX_SUMMARY_RETRIES_PER_RUN) if v not in self.summarized]
        if not ids:
            return
        self.run.log(f"밀린 요약 {len(ids)}개 다시 시도")
        for vid in ids:
            doc = store.get_video(vid) or {}
            text = store.get_transcript_text(vid)
            if not text:
                store.set_video(vid, summaryStatus="failed", summaryError="저장된 자막이 없음")
                continue
            meta = {k: doc.get(k) for k in ("channelId", "channelName", "title", "url", "publishedAt", "durationSec")}
            self.summarize_video(vid, meta, text)

    def defer(self, vid, meta, ch_name, title, why=None):
        if not why:
            self.yt_deferred += 1   # VPN · 유튜브 차단으로 못 받음 (상한 · 몫으로 미룬 것은 정상이라 세지 않음)
        store.set_video(vid, **meta, status="pending", deferDay=self.day_key)
        self.run.log(f"[{ch_name}] {f'{why} 도달, ' if why else '자막 받기 보류, '}대기로 둠: {short(title)}", "skip")
        self.run.count("pending")

    def stale(self, published):
        """올라온 지 STALE_HOURS가 지났으면 True (뉴스라 늦게 받으면 정보 가치가 없음)"""
        if not published:
            return False
        age = self.now - datetime.fromisoformat(published.replace("Z", "+00:00"))
        return age > timedelta(hours=config.STALE_HOURS)

    def skip_stale(self, vid, ch_name, title, meta=None):
        store.set_video(vid, **(meta or {}), status="skipped", deferDay=None, error=reasons.STALE)
        self.run.log(f"[{ch_name}] 건너뜀: {short(title)} - {reasons.STALE}", "skip")
        self.run.count("skipped")
        self.handled.add(vid)

    def fail(self, vid, meta, attempts, ch_name, title, err):
        attempts += 1
        self.fetch_failed += 1
        store.set_video(vid, **meta, status="failed", attempts=attempts, error=reasons.describe(err)[:1000])
        self.run.log(f"[{ch_name}] 실패 ({attempts}회째): {short(title)} - {reasons.describe(err)}", "error",
                     detail=f"영상 {vid} ({meta.get('url', '')})\n{reasons.text(err)}")
        self.run.count("failed")

    def on_blocked(self, err):
        streak, until = cooldown_after_block(self.now, self.yt_state)
        store.set_youtube_state(blockedUntil=until, blockStreak=streak, lastBlockAt=self.now)
        self.yt_state.update(blockedUntil=until, blockStreak=streak, lastBlockAt=self.now)
        self.yt = "defer"
        self.defer_reason = f"유튜브 차단으로 {until:%m-%d %H:%M}까지 쉬는 중"
        self.blocked_until = until
        self.run.log(f"유튜브가 요청을 막았습니다 (연속 {streak}번째). {until:%m-%d %H:%M}까지 자막 받기를 쉬고, "
                     f"남은 영상은 대기로 둡니다. - {reasons.explain(err)}", "error", detail=reasons.text(err))

    def alert_digest(self, channels):
        """회차 끝 Slack 알림 (notify.run_digest): 수집 0건 · 필수 채널 빠짐 · 받기 실패 많음 · 요약 못 함"""
        done = self.run.counts.get("done", 0)
        tried = self.taken - self.waits          # 실제로 받기를 시도한 영상 (라이브 직후 · Gemini 혼잡 대기는 뺌)
        targets = tried + self.yt_deferred       # + VPN · 차단으로 못 받은 영상
        zero = None
        if targets and not done:
            cause = (self.defer_reason or "자막 받기 보류") if self.yt_deferred else "받기 모두 실패"
            zero = (targets, cause)
        failed = (self.fetch_failed, tried) if tried >= 2 and self.fetch_failed * 2 > tried else None
        summary = (self.summary_failed, self.summary_cause) if self.summary_failed else None
        missing = notify.required_missing(channels, self.now, self.today)
        notify.run_digest(self.now, zero=zero, missing=missing, captions_failed=failed, summary_failed=summary)

    def on_captions_ok(self):
        if self.yt_state.get("blockStreak"):
            store.set_youtube_state(blockStreak=0)
            self.yt_state["blockStreak"] = 0


def backfill_title_ko(log, batch=40):
    """titleKo(웹에 보이는 한국어 제목)가 생기기 전(10-23)에 만든 요약의 제목만 번역해 채운다. 다 채우면 더 하지 않음.
    유튜브 요청 없음 (Gemini만)."""
    if not config.SUMMARIZE:
        return
    rows = store.summaries_without_title_ko()
    if not rows:
        return
    done = 0
    for i in range(0, len(rows), batch):
        part = dict(rows[i:i + batch])
        try:
            ko = summarize.translate_titles(part, log=log)
        except summarize.SummarizeError as e:
            log(f"제목 번역 실패: {reasons.describe(e)}", "error", detail=reasons.text(e))
            break
        # 번역이 빠진 것은 원래 제목으로 채운다. 비워 두면 할 일 확인이 매번 '수집합니다'가 된다
        for vid, title in part.items():
            store.set_summary_fields(vid, titleKo=ko.get(vid) or title or vid)
        done += len(ko)
    log(f"지난 요약 제목 {done}/{len(rows)}개 한국어로 채움", "info")


def check():
    """VPN 연결 전에 이번 실행에 할 일이 있는지만 본다 (GitHub Actions 출력 need=true/false).

    필수 채널을 30분마다 확인하므로, 할 일이 없는 실행은 VPN · 유튜브 요청 없이 바로 끝낸다.
    """
    now = datetime.now(config.KST)
    collect_all = os.environ.get("COLLECT_ALL", "").lower() == "true"
    due = due_channels(store.enabled_channels(), now, collect_all)
    pending = len(store.pending_video_ids())
    jobs = store.has_pending_jobs()
    # 한국어 제목이 빠진 요약이 있으면 수집 단계까지 간다 (이 단계엔 Gemini 키가 없어 여기서 번역하지 못함)
    titles = len(store.summaries_without_title_ko()) if config.SUMMARIZE else 0
    need = bool(due or pending or jobs or titles)
    print(f"예약 시각이 지난 채널 {len(due)}개 ({', '.join(ch.get('name', '') for _, ch in due) or '없음'}), "
          f"대기 영상 {pending}개, 웹 요청 {'있음' if jobs else '없음'}"
          + (f", 한국어 제목 없는 요약 {titles}개" if titles else "")
          + f" → {'수집합니다' if need else '할 일 없음'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"need={'true' if need else 'false'}\n")


def main():
    store.init()
    if "--check" in sys.argv:
        return check()
    now = datetime.now(config.KST)

    jobs = store.take_pending_jobs()
    collect_all = (os.environ.get("COLLECT_ALL", "").lower() == "true"
                   or any(j.get("type") == "collect_all" for _, j in jobs))
    channels = store.enabled_channels()
    required = {cid for cid, ch in channels if ch.get("required")}
    due = due_channels(channels, now, collect_all)
    has_pending = bool(store.pending_video_ids())
    yt_state = store.youtube_state()
    yt, reason = captions_mode(now, yt_state)

    if not due and (not has_pending or yt == "defer"):
        store.finish_jobs([jid for jid, _ in jobs])
        print("예약 시각이 지난 채널이 없고, 처리할 수 있는 대기 영상도 없어 종료합니다."
              + (f" ({reason})" if has_pending else ""))
        backfill_title_ko(lambda msg, level="info": print(msg))
        return

    run = store.Run(config.RUN_TRIGGER)
    run.log(f"작업 시작: 채널 {len(due)}개" + (" (전체 수집 요청)" if collect_all else ""))
    if yt == "defer":
        run.log(f"자막 받기 보류: {reason}. 새 영상은 대기로 둡니다.", "skip")
    collector = Collector(run, now, yt_state, required, dict(channels))
    status, note = "done", None
    try:
        # 순서: 필수 채널(대기 → 새 영상) → 나머지(대기 → 새 영상). 대기 영상이 새 영상에 계속 밀리지 않게 한다
        # 나머지 채널끼리는 실행마다 순서를 섞는다 (같은 순서로 요청하지 않게, 상한에 걸려도 늘 같은 채널만 밀리지 않게)
        others = [c for c in due if c[0] not in required]
        random.shuffle(others)
        for req in (True, False):
            collector.process_pending(required=req)
            for cid, ch in (due if req else others):
                if (cid in required) != req:
                    continue
                try:
                    collector.process_channel(cid, ch)
                except youtube.YouTubeError as e:
                    run.log(f"[{ch.get('name')}] 채널 조회 실패: {reasons.describe(e)}", "error", detail=reasons.text(e))
        collector.process_pending_summaries()
        backfill_title_ko(run.log)
        run.log(f"모든 작업을 마쳤습니다. 받기 시도 {collector.taken}개, 오늘 누적 {collector.day_count}개 "
                f"({', '.join(f'{g} {n}' for g, n in sorted(collector.day_counts.items())) or '없음'}), "
                f"유튜브 요청 {captions.request_count()}번"
                + (f" ({captions.request_breakdown()})" if captions.request_count() else "")
                + (f", 기본 설정으로 다시 시도 {captions.fallback_count()}번" if captions.fallback_count() else ""),
                "success")
        if collector.blocked_until:
            status, note = "blocked", f"유튜브 차단, {collector.blocked_until:%m-%d %H:%M}까지 쉼"
    except QuotaExceeded as e:
        status, note = "quota", "Gemini 한도 도달"
        run.log("Gemini 일일 한도에 도달했습니다. 남은 영상은 다음 실행에서 처리합니다.", "error", detail=reasons.text(e))
        notify.gemini_day_limit("받아쓰기")
    except Exception as e:
        run.log(f"예기치 못한 오류로 중단: {reasons.describe(e)}", "error", detail=traceback.format_exc())
        notify.crashed(reasons.explain(e))
        run.finish("error", f"예기치 못한 오류: {reasons.describe(e)}", ytRequests=captions.request_count())
        store.finish_jobs([jid for jid, _ in jobs])
        raise
    try:
        collector.alert_digest(channels)
    except Exception as e:  # 알림 때문에 실행 기록이 안 닫히면 안 된다
        print(f"회차 끝 알림 확인 실패: {type(e).__name__}")
    store.finish_jobs([jid for jid, _ in jobs])
    run.finish(status, note, ytRequests=captions.request_count(), ytFallbacks=captions.fallback_count(),
               dayCounts=dict(collector.day_counts))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 실행 기록을 만들기 전(Firestore 접속 등)에 죽은 경우. main 안에서 이미 알렸으면 다시 보내지 않는다
        if not notify.crash_sent:
            notify.crashed(reasons.explain(e))
        raise
    finally:
        # 이번 실행에서 쓰거나 고친 요약의 묶음을 Storage 파일로 발행 (블로그가 읽음, 11-16). 실패해도 실행은 그대로
        feed.publish_touched()

#!/usr/bin/env python3
"""抓取层：Google News RSS + 专业信源 RSS -> logs/last_raw_items.json"""
import json, re, sys, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser, requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
OUT_PATH = BASE / "logs" / "last_raw_items.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
GN_TEMPLATE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en&when:{h}h"
PER_SOURCE_MAX = 30  # 每个信源最多保留条数，防止聚合源挤占专家源与智库条目


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_google_urls(config):
    """装配 Google News 关键词 RSS URL（when 参数即抓取小时窗口）"""
    hours = config.get("fetch_hours", config["window_hours"])
    return [GN_TEMPLATE.format(q=urllib.parse.quote(q), h=hours)
            for q in config["google_news_queries"]]


def fetch_feed(url):
    """抓取单个 RSS，失败返回空 feed（不中断整体）"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as exc:  # 网络失败/解析失败都容错
        print(f"[warn] 抓取失败 {url}: {exc}", file=sys.stderr)
        return None


def extract_items(feed, source):
    """从 feedparser 结果提取标准条目；时间解析失败则丢弃该条目"""
    out = []
    for entry in feed.get("entries", []):
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        if not ts:
            continue
        published = datetime(*ts[:6], tzinfo=timezone.utc)
        out.append({
            "title": (entry.get("title") or "").strip(),
            "url": (entry.get("link") or "").strip(),
            "source": source,
            "published_utc": published.isoformat(),
            "summary": re.sub(r"<[^>]+>", "", entry.get("summary") or "").strip()[:400],
        })
    return out


def filter_window(items, now_utc, hours):
    """仅保留窗口内的条目；时间无法解析的条目丢弃"""
    start = now_utc - timedelta(hours=hours)
    kept = []
    for it in items:
        try:
            ts = datetime.fromisoformat(it["published_utc"])
        except ValueError:
            continue
        if start <= ts <= now_utc:
            kept.append(it)
    return kept


def norm_title(title):
    """标题归一化：小写、去标点与空白"""
    return re.sub(r"[\W_]+", "", title.lower())


def dedupe(items):
    """同标题保留第一条（各 RSS 源混排时先到先得）"""
    seen, out = set(), []
    for it in items:
        key = norm_title(it["title"])
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def cap_per_source(items, max_per_source=PER_SOURCE_MAX):
    """per-source cap：各信源仅保留最新 max_per_source 条，保证信源多样性"""
    from collections import defaultdict
    by_source = defaultdict(list)
    for it in items:
        by_source[it["source"]].append(it)
    capped = []
    for src, src_items in by_source.items():
        src_items.sort(key=lambda x: x["published_utc"], reverse=True)
        capped.extend(src_items[:max_per_source])
    return capped


def fetch_all_items(config, now_utc):
    """抓取全部信源并标准化"""
    items = []
    for url in build_google_urls(config):
        feed = fetch_feed(url)
        if feed is not None:
            items.extend(extract_items(feed, "Google News聚合"))
    for url in config["expert_feeds"]:
        feed = fetch_feed(url)
        if feed is not None:
            src = feed.get("feed", {}).get("title") or url.split("/")[2]
            items.extend(extract_items(feed, src))
    return items


def main():
    config = load_config()
    now = datetime.now(timezone.utc)
    raw = fetch_all_items(config, now)
    fetch_hours = config.get("fetch_hours", config["window_hours"])
    kept = dedupe(filter_window(raw, now, fetch_hours))
    kept = cap_per_source(kept, PER_SOURCE_MAX)
    kept.sort(key=lambda x: x["published_utc"], reverse=True)
    kept = kept[: config["max_items"]]
    OUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at_utc": now.isoformat(), "items": kept}, f, ensure_ascii=False, indent=1)
    print(f"抓取完成：原始{len(raw)}条 -> 窗口内去重后{len(kept)}条")
    if not kept:
        sys.exit(2)  # 退出码2 = 无条目（run_daily.sh 据此告警）


if __name__ == "__main__":
    main()

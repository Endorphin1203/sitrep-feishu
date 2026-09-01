"""fetch_news.py 单元测试（使用本地 fixture，不联网）"""
import json, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import fetch_news

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

def parse_fixture(path):
    """用 feedparser 解析本地 XML fixture，等价于 fetch_news 的解析逻辑入口"""
    import feedparser
    return feedparser.parse(str(BASE / "tests" / "fixtures" / path))

class TestDedupe(unittest.TestCase):
    def test_same_title_kept_once(self):
        items = [
            {"title": "US Navy tests new drone", "url": "https://a.com/1", "source": "A", "published_utc": "", "summary": ""},
            {"title": "US Navy tests new drone!", "url": "https://b.com/2", "source": "B", "published_utc": "", "summary": ""},
        ]
        out = fetch_news.dedupe(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.com/1")

class TestFilterWindow(unittest.TestCase):
    def test_older_than_5h_dropped(self):
        items = [
            {"title": "new", "published_utc": (NOW - timedelta(hours=1)).isoformat()},
            {"title": "old", "published_utc": (NOW - timedelta(hours=6)).isoformat()},
            {"title": "bad time", "published_utc": "not-a-time"},
        ]
        out = fetch_news.filter_window(items, NOW, 5)
        self.assertEqual([i["title"] for i in out], ["new"])

class TestParseFeed(unittest.TestCase):
    def test_parse_rss_extracts_fields(self):
        feed = parse_fixture("sample_rss.xml")
        items = fetch_news.extract_items(feed, "测试媒体")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Pentagon announces laser deployment")
        self.assertEqual(items[0]["source"], "测试媒体")
        self.assertEqual(items[0]["url"], "https://example.com/1")
        self.assertEqual(items[0]["published_utc"], "2026-08-31T08:00:00+00:00")

class TestBuildGoogleUrls(unittest.TestCase):
    def test_uses_fetch_hours(self):
        cfg = {"fetch_hours": 24, "window_hours": 5,
               "google_news_queries": ["test query"]}
        urls = fetch_news.build_google_urls(cfg)
        self.assertEqual(len(urls), 1)
        self.assertIn("when:24h", urls[0])
        self.assertIn("test%20query", urls[0])

    def test_fallback_to_window_hours(self):
        cfg = {"window_hours": 5, "google_news_queries": ["q"]}
        urls = fetch_news.build_google_urls(cfg)
        self.assertIn("when:5h", urls[0])

class TestCapPerSource(unittest.TestCase):
    def test_caps_same_source_to_30_keeps_others(self):
        """同源40条截到最新30条，异源10条全部保留"""
        items = []
        for i in range(40):
            items.append({
                "title": f"聚合新闻{i}", "url": f"https://agg.com/{i}",
                "source": "Google News聚合",
                "published_utc": (NOW - timedelta(minutes=i)).isoformat(),
                "summary": "",
            })
        for i in range(10):
            items.append({
                "title": f"智库文章{i}", "url": f"https://thinktank.com/{i}",
                "source": "War on the Rocks",
                "published_utc": (NOW - timedelta(minutes=i)).isoformat(),
                "summary": "",
            })
        out = fetch_news.cap_per_source(items, max_per_source=30)
        agg = [i for i in out if i["source"] == "Google News聚合"]
        expert = [i for i in out if i["source"] == "War on the Rocks"]
        self.assertEqual(len(out), 40)          # 30 + 10
        self.assertEqual(len(agg), 30)          # 同源被截到30条
        self.assertEqual(len(expert), 10)       # 异源全部保留
        # 同源保留的是最新的30条（时间最新的优先）
        oldest_kept = min(i["published_utc"] for i in agg)
        dropped = max(i["published_utc"] for i in items
                      if i["source"] == "Google News聚合"
                      and i["url"] not in {x["url"] for x in agg})
        self.assertGreaterEqual(oldest_kept, dropped)

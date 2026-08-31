"""generate_report.py 单元测试（mock DeepSeek API，不联网）"""
import json, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import generate_report as gen

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE_ITEMS = [
    {"title": "US strikes Iran", "url": "https://x.com/1", "source": "CNN", "published_utc": "2026-08-31T10:00:00+00:00", "summary": "美军打击伊朗设施"},
]

class TestValidate(unittest.TestCase):
    def test_good_report_passes(self):
        obj = {"window": {"start_utc": "a", "end_utc": "b"}, "verdict": "v",
               "sections": [{"section": "一、作战行动", "items": [
                   {"title": "t", "source": "s", "url": "u", "summary": "ss", "tag": "❗"}]}]}
        self.assertEqual(gen.validate_report(obj), [])

    def test_missing_sections_rejected(self):
        self.assertTrue(gen.validate_report({"verdict": "v"}))

    def test_item_missing_url_rejected(self):
        obj = {"sections": [{"section": "x", "items": [{"title": "t", "source": "s", "summary": "s", "tag": ""}]}], "verdict": "v"}
        self.assertTrue(gen.validate_report(obj))

    def test_validate_non_dict_section_returns_problem(self):
        """sections 含非 dict 元素时返回问题列表而非抛 AttributeError"""
        obj = {"window": {"start_utc": "a", "end_utc": "b"}, "verdict": "v",
               "sections": [{"section": "x", "items": []}, "坏元素"]}
        self.assertTrue(gen.validate_report(obj))

    def test_validate_non_list_items_returns_problem(self):
        """某节 items 非 list 时返回问题列表而非抛 AttributeError"""
        obj = {"window": {"start_utc": "a", "end_utc": "b"}, "verdict": "v",
               "sections": [{"section": "x", "items": "坏items"}]}
        self.assertTrue(gen.validate_report(obj))

    def test_validate_missing_window_rejected(self):
        """产物契约要求 window.start_utc/end_utc，缺失应被拒绝"""
        obj = {"verdict": "v", "sections": [{"section": "x", "items": []}]}
        self.assertTrue(gen.validate_report(obj))

    def test_validate_bad_window_rejected(self):
        """window 非 dict 或 start_utc/end_utc 非字符串应被拒绝"""
        obj1 = {"window": "坏window", "verdict": "v", "sections": []}
        obj2 = {"window": {"start_utc": 123, "end_utc": "b"}, "verdict": "v", "sections": []}
        self.assertTrue(gen.validate_report(obj1))
        self.assertTrue(gen.validate_report(obj2))

class TestParseReport(unittest.TestCase):
    def test_parse_strips_markdown_fence(self):
        text = '```json\n{"sections": [], "verdict": "ok"}\n```'
        obj = gen.parse_report(text)
        self.assertEqual(obj["verdict"], "ok")

    def test_parse_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            gen.parse_report("不是JSON")

class TestCallDeepseek(unittest.TestCase):
    def test_extracts_content(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        with mock.patch.object(gen.requests, "post", return_value=resp) as mp:
            self.assertEqual(gen.call_deepseek("p", "key"), "{}")
            self.assertEqual(mp.call_args[1]["timeout"], 120)

    def test_402_raises_with_balance_hint(self):
        resp = mock.Mock()
        resp.status_code = 402
        resp.text = "Insufficient Balance"
        with mock.patch.object(gen.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as cm:
                gen.call_deepseek("p", "key")
            self.assertIn("余额", str(cm.exception))

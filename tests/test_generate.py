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
    def _good_item(self):
        return {"cn_title": "美军打击伊朗拉腊克岛设施", "source": "Reuters",
                "published_edt": "2026-08-31 20:15", "title_en": "US strikes Iran's Larak Island",
                "title_cn": "美军打击伊朗拉腊克岛", "summary": "美军轰炸两处IRGC设施。",
                "china_impact": "暂无直接关联", "military_ref": "观察美伊冲突消耗美军战备", "url": "https://x.com/1"}

    def _good_report(self):
        secs = []
        for name in ["一、严格5小时窗口内", "二、当天00:00至窗口起点的重要军事动态",
                     "三、当天最值得关注的美国涉华军事舆论与战略分析",
                     "四、当天智库分析文章", "五、来源清单"]:
            secs.append({"section": name, "items": [self._good_item()]})
        return {"window": {"start_utc": "a", "end_utc": "b"}, "day_start_utc": "c",
                "sections": secs, "sources": [{"name": "Reuters", "url": "https://reuters.com"}],
                "verdict": "v"}

    def test_good_report_passes(self):
        self.assertEqual(gen.validate_report(self._good_report()), [])

    def test_missing_field_rejected(self):
        obj = self._good_report()
        del obj["sections"][0]["items"][0]["china_impact"]
        self.assertTrue(gen.validate_report(obj))

    def test_missing_sources_rejected(self):
        obj = self._good_report()
        del obj["sources"]
        self.assertTrue(gen.validate_report(obj))

    def test_wrong_section_count_rejected(self):
        obj = self._good_report()
        obj["sections"] = obj["sections"][:4]
        self.assertTrue(gen.validate_report(obj))

    def test_non_dict_item_rejected(self):
        obj = self._good_report()
        obj["sections"][0]["items"] = ["not-a-dict"]
        self.assertTrue(gen.validate_report(obj))


class TestComputeAnchors(unittest.TestCase):
    def test_anchors_values(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)  # 美东9月1日00:30
        a = gen.compute_anchors(now)
        self.assertEqual(a["D_edt"], "2026-08-31 00:00")
        self.assertEqual(a["T_edt"], "2026-09-01 00:30")
        self.assertEqual(a["S_edt"], "2026-08-31 19:30")

class TestBuildPrompt(unittest.TestCase):
    def test_returns_prompt_system_with_anchors(self):
        now = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
        prompt, system = gen.build_prompt(SAMPLE_ITEMS, now)
        self.assertIsInstance(prompt, str)
        self.assertIsInstance(system, str)
        self.assertIn("S=2026-08-31 19:30", prompt)
        self.assertIn("T=2026-09-01 00:30", prompt)
        self.assertIn("D=2026-08-31 00:00", prompt)
        self.assertIn("US strikes Iran", prompt)
        # system 提示词含锚点值，且 JSON 示例花括号原样保留（format 陷阱回归测试）
        self.assertIn("2026-08-31 19:30", system)
        self.assertIn('{"window"', system)
        self.assertIn('"sources":[{"name":"","url":""}]', system)

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
            self.assertEqual(gen.call_deepseek("p", "key", "sys"), "{}")
            self.assertEqual(mp.call_args[1]["timeout"], 180)

    def test_402_raises_with_balance_hint(self):
        resp = mock.Mock()
        resp.status_code = 402
        resp.text = "Insufficient Balance"
        with mock.patch.object(gen.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as cm:
                gen.call_deepseek("p", "key", "sys")
            self.assertIn("余额", str(cm.exception))


class TestRebalanceSections(unittest.TestCase):
    """程序化兜底：模型时间切分偶发越界时，按 published_edt 修正第一/二部分归属"""

    def _mk(self, published_edt):
        return {"cn_title": "x", "source": "s", "published_edt": published_edt,
                "title_en": "t", "title_cn": "t", "summary": "s",
                "china_impact": "无", "military_ref": "无", "url": "u"}

    def _report(self, sec1_items, sec2_items):
        # 窗口：23:10Z—04:10Z = EDT 19:10—00:10；D=S所在日(8月31日)00:00 EDT
        return {"window": {"start_utc": "2026-08-31T23:10:00Z", "end_utc": "2026-09-01T04:10:00Z"},
                "day_start_utc": "2026-09-01T04:00:00Z",
                "sections": [
                    {"section": gen.SECTION_NAMES[0], "items": list(sec1_items)},
                    {"section": gen.SECTION_NAMES[1], "items": list(sec2_items)},
                    {"section": gen.SECTION_NAMES[2], "items": []},
                    {"section": gen.SECTION_NAMES[3], "items": []},
                    {"section": gen.SECTION_NAMES[4], "items": []}],
                "sources": [], "verdict": "v"}

    def test_out_of_window_moved_to_sec2(self):
        # 18:24 EDT < 窗口起点19:10，应从第一部分移入第二部分
        obj = self._report([self._mk("2026-08-31 18:24")], [])
        obj, stats = gen.rebalance_sections(obj)
        self.assertEqual(len(obj["sections"][0]["items"]), 0)
        self.assertEqual(len(obj["sections"][1]["items"]), 1)
        self.assertEqual(stats["moved_to_sec2"], 1)

    def test_in_window_from_sec2_moved_to_sec1(self):
        # 20:00 EDT 在窗口内，应从第二部分移入第一部分
        obj = self._report([], [self._mk("2026-08-31 20:00")])
        obj, stats = gen.rebalance_sections(obj)
        self.assertEqual(len(obj["sections"][0]["items"]), 1)
        self.assertEqual(len(obj["sections"][1]["items"]), 0)
        self.assertEqual(stats["moved_to_sec1"], 1)

    def test_before_day_start_removed(self):
        # 8月30日 23:00 EDT 早于当天00:00（8月31日00:00），应移除
        obj = self._report([self._mk("2026-08-30 23:00")], [])
        obj, stats = gen.rebalance_sections(obj)
        self.assertEqual(len(obj["sections"][0]["items"]), 0)
        self.assertEqual(len(obj["sections"][1]["items"]), 0)
        self.assertEqual(stats["removed"], 1)

    def test_ok_items_untouched(self):
        obj = self._report([self._mk("2026-08-31 20:00")], [self._mk("2026-08-31 18:00")])
        obj, stats = gen.rebalance_sections(obj)
        self.assertEqual(len(obj["sections"][0]["items"]), 1)
        self.assertEqual(len(obj["sections"][1]["items"]), 1)
        self.assertEqual(stats["moved_to_sec1"], 0)
        self.assertEqual(stats["moved_to_sec2"], 0)
        self.assertEqual(stats["removed"], 0)

    def test_unparseable_time_kept_in_place(self):
        obj = self._report([self._mk("时间未知")], [])
        obj, stats = gen.rebalance_sections(obj)
        self.assertEqual(len(obj["sections"][0]["items"]), 1)


class TestComputeWindow(unittest.TestCase):
    def test_window_and_day_start(self):
        from datetime import timedelta
        now = datetime(2026, 9, 1, 4, 10, 43, tzinfo=timezone.utc)  # 美东凌晨00:10
        w = gen.compute_window(now)
        self.assertEqual(w["window"]["end_utc"], now.isoformat())
        self.assertEqual(w["window"]["start_utc"], (now - timedelta(hours=5)).isoformat())
        # S=23:10Z → 美东19:10（8月31日）→ S所在日00:00 EDT = 04:00Z
        self.assertIn("2026-08-31T04:00:00", w["day_start_utc"])


class TestParseUtc(unittest.TestCase):
    def test_zulu_suffix(self):
        dt = gen._parse_utc("2026-08-31T23:10:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_naive_assumed_utc(self):
        dt = gen._parse_utc("2026-08-31 23:10:00")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 23)

    def test_invalid_returns_none(self):
        self.assertIsNone(gen._parse_utc("不是时间"))

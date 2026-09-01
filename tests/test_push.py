"""push_feishu.py 单元测试（mock 网络）"""
import io, json, unittest, warnings
from pathlib import Path
from unittest import mock

warnings.filterwarnings("ignore", module="urllib3")  # 屏蔽环境噪声（NotOpenSSLWarning）

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import push_feishu as push

def make_item(i):
    return {"cn_title": f"美军新闻标题{i}", "source": "Reuters",
            "published_edt": "2026-08-31 20:15", "title_en": f"US news title {i}",
            "title_cn": f"美军新闻标题{i}", "summary": f"概述{i}",
            "china_impact": "暂无直接关联", "military_ref": f"警示{i}",
            "url": f"https://x.com/{i}"}

def make_report(n_items):
    secs = [{"section": "一、严格5小时窗口内", "items": [make_item(i) for i in range(n_items)]},
            {"section": "二、当天00:00至窗口起点的重要军事动态", "items": []},
            {"section": "三、当天最值得关注的美国涉华军事舆论与战略分析", "items": []},
            {"section": "四、当天智库分析文章", "items": []},
            {"section": "五、来源清单", "items": []}]
    return {"window": {"start_utc": "2026-08-31T22:30:00+00:00", "end_utc": "2026-09-01T03:30:00+00:00"},
            "day_start_utc": "2026-09-01T04:00:00+00:00",
            "sections": secs,
            "sources": [{"name": "Reuters", "url": "https://www.reuters.com"}],
            "verdict": "研判"}

class TestBuildPostMessage(unittest.TestCase):
    def test_structure_and_title(self):
        report = make_report(2)
        msg = push.build_post_message(report, 1, 1)
        self.assertEqual(msg["msg_type"], "post")
        post = msg["content"]["post"]["zh_cn"]
        # 标题行新格式：截至 EDT ... 对应北京时间 ...
        self.assertIn("截至 美国东部时间（EDT）", post["title"])
        self.assertIn("本轮严格5小时窗口为", post["title"])
        self.assertIn("对应北京时间", post["title"])
        # 内容：节标题 + 2条目段 + 研判段 + 来源清单段（标题+1条）
        self.assertEqual(len(post["content"]), 6)
        # 条目段：编号行含cn_title、字段行齐全
        item_text = "".join(b["text"] for b in post["content"][1] if b["tag"] == "text")
        self.assertIn("1. 美军新闻标题0", item_text)
        self.assertIn("来源：Reuters", item_text)
        self.assertIn("发布时间：美国东部时间2026-08-31 20:15", item_text)
        self.assertIn("标题：US news title 0", item_text)
        self.assertIn("中文标题：美军新闻标题0", item_text)
        self.assertIn("概述：概述0", item_text)
        self.assertIn("对中国的直接影响：暂无直接关联", item_text)
        self.assertIn("军事借鉴警示：警示0", item_text)
        link = [b for b in post["content"][1] if b["tag"] == "a"]
        self.assertEqual(link[0]["href"], "https://x.com/0")
        # 来源清单段
        src_text = "".join(b["text"] for b in post["content"][-2] if b["tag"] == "text")
        self.assertIn("Reuters", src_text)

class TestWindowTitle(unittest.TestCase):
    def test_title_times(self):
        report = make_report(1)
        t = push._window_title(report, 1, 1)
        # start 22:30 UTC = 18:30 EDT；end 03:30 UTC = 前一日23:30 EDT（9月1日03:30UTC=8月31日23:30EDT）
        self.assertIn("截至 美国东部时间（EDT）08月31日 23:30", t)
        self.assertIn("08月31日 18:30—08月31日 23:30 EDT", t)
        # 北京时间：22:30 UTC = 次日06:30 BJT；03:30 UTC = 11:30 BJT
        self.assertIn("对应北京时间 9月1日06:30—9月1日11:30", t)

class TestFmtToleratesZulu(unittest.TestCase):
    """模型输出的 window.start_utc/end_utc 可能为 Z 后缀 ISO（如 2026-08-31T21:15:00Z），
    Python 3.9 fromisoformat 不识别，fmt_edt/fmt_bj 必须容错（replace Z -> +00:00）且解析失败返回「?」"""
    def test_fmt_edt_zulu(self):
        self.assertEqual(push.fmt_edt("2026-08-31T21:15:00Z"), "08月31日 17:15")

    def test_fmt_bj_zulu(self):
        self.assertEqual(push.fmt_bj("2026-08-31T21:15:00Z"), "9月1日05:15")

    def test_fmt_invalid_returns_question_mark(self):
        self.assertEqual(push.fmt_edt("not-a-date"), "?")
        self.assertEqual(push.fmt_bj(""), "?")

class TestSplitParts(unittest.TestCase):
    def test_small_report_single_part(self):
        report = make_report(3)
        parts = push.split_parts(report, limit=18000)
        self.assertEqual(len(parts), 1)

    def test_large_report_splits(self):
        report = make_report(50)
        parts = push.split_parts(report, limit=2500)  # 人为压低阈值强制拆分
        self.assertGreater(len(parts), 1)

    def test_split_keeps_items_intact(self):
        """超限拆分按条目边界进行：每条新闻完整保留、每部分不超限"""
        report = make_report(50)
        parts = push.split_parts(report, limit=2500)  # 人为压低阈值强制拆分
        texts = [b["text"] for p in parts
                 for para in p["content"]["post"]["zh_cn"]["content"]
                 for b in para if b["tag"] == "text"]
        for i in range(50):
            self.assertTrue(any(f"{i+1}. 美军新闻标题{i}" in t for t in texts),
                            f"第{i}条新闻在拆分后丢失")
        for p in parts:
            size = len(json.dumps(p, ensure_ascii=False).encode("utf-8"))
            self.assertLessEqual(size, 2500)

class TestSend(unittest.TestCase):
    def test_success_first_try(self):
        resp = mock.Mock(status_code=200); resp.json.return_value = {"code": 0}
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            self.assertTrue(push.send("http://hook", {"msg_type": "text", "content": {}}))
        self.assertEqual(mp.call_count, 1)

    def test_retries_then_fails(self):
        resp = mock.Mock(status_code=200); resp.json.side_effect = ValueError("bad json")
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            with mock.patch("time.sleep"):
                self.assertFalse(push.send("http://hook", {}))
        self.assertEqual(mp.call_count, 3)

    def test_code_19001_returns_false(self):
        """飞书错误码 19001（webhook token 无效/事件格式无效）必须视为失败并重试"""
        resp = mock.Mock(status_code=200); resp.json.return_value = {"code": 19001}
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            with mock.patch("time.sleep"):  # 跳过指数退避等待
                self.assertFalse(push.send("http://hook", {}))
        self.assertEqual(mp.call_count, 3)  # 重试3次后仍返回 False

    def test_webhook_http_error_no_retry_on_4xx(self):
        resp = mock.Mock(); resp.status_code = 400
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            with mock.patch("time.sleep"):
                with mock.patch.object(push.sys, "stderr", io.StringIO()):
                    self.assertFalse(push.send("http://hook", {}))
        self.assertEqual(mp.call_count, 1)

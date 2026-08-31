"""push_feishu.py 单元测试（mock 网络）"""
import io, json, unittest, warnings
from pathlib import Path
from unittest import mock

warnings.filterwarnings("ignore", module="urllib3")  # 屏蔽环境噪声（NotOpenSSLWarning）

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import push_feishu as push

def make_report(n_items):
    secs = [{"section": "一、作战行动与部署", "items": [
        {"title": f"新闻标题{i}", "source": "CNN", "url": f"https://x.com/{i}",
         "summary": f"要点{i}", "tag": "❗" if i == 0 else "⚠️"}
        for i in range(n_items)]}]
    return {"window": {"start_utc": "2026-08-31T00:00:00+00:00", "end_utc": "2026-08-31T05:00:00+00:00"},
            "sections": secs, "verdict": "研判"}

class TestBuildPostMessage(unittest.TestCase):
    def test_structure_and_title(self):
        report = make_report(2)
        msg = push.build_post_message(report, 1, 1)
        self.assertEqual(msg["msg_type"], "post")
        post = msg["content"]["post"]["zh_cn"]
        self.assertIn("美军新闻简报", post["title"])
        self.assertEqual(len(post["content"]), 4)  # 1节2条 => 节标题段 + 2条段 + 1段研判
        # 首段为节标题
        head = post["content"][0][0]
        self.assertEqual(head["tag"], "text")
        self.assertIn("一、作战行动与部署", head["text"])
        # 条目段含标题、tag 与超链接
        first = post["content"][1][0]
        self.assertEqual(first["tag"], "text")
        self.assertIn("新闻标题0", first["text"])
        self.assertIn("❗", first["text"])
        link = [b for b in post["content"][1] if b["tag"] == "a"]
        self.assertEqual(link[0]["href"], "https://x.com/0")

    def test_tag_normalize_short_forms(self):
        # 终审修复：模型输出简写 tag（⚠️/❗/🔬）应归一化为规范值
        report = make_report(3)
        msg = push.build_post_message(report, 1, 1)
        post = msg["content"]["post"]["zh_cn"]
        # 条目1 tag=❗ → 规范化为"❗对华直接影响"；条目2/3 tag=⚠️ → "⚠️警示"
        self.assertIn("❗对华直接影响", post["content"][1][0]["text"])
        self.assertIn("⚠️警示", post["content"][2][0]["text"])
        # 空 tag 保持不变（无 tag 字段时无该后缀）
        item_no_tag = {"title": "无标注", "source": "S", "url": "https://x.com/n", "summary": "s", "tag": ""}
        para = push._item_paragraph(item_no_tag)
        self.assertNotIn("警示", para[0]["text"])

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
            self.assertTrue(any(f"新闻标题{i}（CNN）" in t for t in texts),
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

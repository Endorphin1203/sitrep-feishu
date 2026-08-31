"""listener.py 过滤链单元测试（dict 事件，不依赖 SDK）"""
import json, sys, unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import listener

def make_event(chat_id="oc_test1", chat_type="group", message_type="text",
               text="@_user_1 /news", sender="ou_user1"):
    return {"event": {"sender": {"sender_id": {"open_id": sender}},
                      "message": {"chat_id": chat_id, "chat_type": chat_type,
                                  "message_type": message_type,
                                  "content": json.dumps({"text": text})}}}

CONFIG = {"allowed_chat_ids": ["oc_test1"], "dedup_seconds": 300}

class TestExtractFields(unittest.TestCase):
    def test_extract_normal_event(self):
        f = listener.extract_fields(make_event())
        self.assertEqual(f["chat_id"], "oc_test1")
        self.assertEqual(f["chat_type"], "group")
        self.assertEqual(f["message_type"], "text")
        self.assertEqual(f["text"], "@_user_1 /news")
        self.assertEqual(f["sender_id"], "ou_user1")

    def test_extract_bad_content_returns_empty_text(self):
        ev = make_event()
        ev["event"]["message"]["content"] = "不是JSON"
        self.assertEqual(listener.extract_fields(ev)["text"], "")

class TestStripMentions(unittest.TestCase):
    def test_strips_single_mention(self):
        self.assertEqual(listener.strip_mentions("@_user_1 /news"), "/news")

    def test_strips_multiple_mentions(self):
        self.assertEqual(listener.strip_mentions("@_user_1 @_user_2  /news"), "/news")

class TestIsCommand(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(listener.is_command("/news"))

    def test_extra_args_rejected(self):
        self.assertFalse(listener.is_command("/news now"))

    def test_whitespace_and_prefix_rejected(self):
        self.assertFalse(listener.is_command(" /news"))   # 已在strip_mentions后strip
        self.assertFalse(listener.is_command("news"))

class TestChatAllowed(unittest.TestCase):
    def test_whitelist(self):
        self.assertTrue(listener.chat_allowed("oc_test1", ["oc_test1", "oc_x"]))
        self.assertFalse(listener.chat_allowed("oc_other", ["oc_test1"]))
        self.assertFalse(listener.chat_allowed(None, ["oc_test1"]))

class TestDedupOk(unittest.TestCase):
    def test_first_pass_then_blocked_within_window(self):
        state = {}
        now = 1000.0
        self.assertTrue(listener.dedup_ok("oc1", "ou1", state, now, 300))
        self.assertFalse(listener.dedup_ok("oc1", "ou1", state, now + 100, 300))
        # 窗口外恢复
        self.assertTrue(listener.dedup_ok("oc1", "ou1", state, now + 301, 300))

    def test_different_user_not_blocked(self):
        state = {}
        now = 1000.0
        listener.dedup_ok("oc1", "ou1", state, now, 300)
        self.assertTrue(listener.dedup_ok("oc1", "ou2", state, now + 1, 300))

class TestShouldHandle(unittest.TestCase):
    def test_valid_event_passes(self):
        ok, why = listener.should_handle(make_event(), CONFIG, {}, 1000.0)
        self.assertTrue(ok, why)

    def test_p2p_rejected(self):
        ok, _ = listener.should_handle(make_event(chat_type="p2p"), CONFIG, {}, 1000.0)
        self.assertFalse(ok)

    def test_non_text_rejected(self):
        ok, _ = listener.should_handle(make_event(message_type="image"), CONFIG, {}, 1000.0)
        self.assertFalse(ok)

    def test_not_command_rejected(self):
        ok, _ = listener.should_handle(make_event(text="hello"), CONFIG, {}, 1000.0)
        self.assertFalse(ok)

    def test_non_whitelist_chat_rejected(self):
        ok, _ = listener.should_handle(make_event(chat_id="oc_other"), CONFIG, {}, 1000.0)
        self.assertFalse(ok)

    def test_dedup_second_trigger_rejected(self):
        state = {}
        now = 1000.0
        self.assertTrue(listener.should_handle(make_event(), CONFIG, state, now)[0])
        self.assertFalse(listener.should_handle(make_event(), CONFIG, state, now + 10)[0])

class TestLockBusy(unittest.TestCase):
    def test_free_then_busy(self):
        import fcntl, os
        # 清场
        try:
            os.unlink(listener.LOCK_PATH)
        except FileNotFoundError:
            pass
        self.assertFalse(listener.lock_busy())
        fd = os.open(listener.LOCK_PATH, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertTrue(listener.lock_busy())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

class TestSendConfirm(unittest.TestCase):
    def test_send_ok(self):
        from unittest import mock
        client = mock.Mock()
        resp = mock.Mock()
        resp.success.return_value = True
        client.im.v1.message.create.return_value = resp
        self.assertTrue(listener.send_confirm(client, "oc_test1", "收到"))
        client.im.v1.message.create.assert_called_once()

    def test_send_with_none_client_returns_false(self):
        self.assertFalse(listener.send_confirm(None, "oc_test1", "x"))

class TestSpawnRun(unittest.TestCase):
    def test_popen_called_with_run_script(self):
        from unittest import mock
        with mock.patch.object(listener.subprocess, "Popen") as mp:
            listener.spawn_run()
        args = mp.call_args[0][0]
        self.assertEqual(args[0], "bash")
        self.assertTrue(str(args[1]).endswith("run_daily.sh"))

class TestMakeHandler(unittest.TestCase):
    def test_handler_full_flow(self):
        from unittest import mock
        client = mock.Mock()
        resp = mock.Mock()
        resp.success.return_value = True
        client.im.v1.message.create.return_value = resp
        client_ref = {"rest": client}
        state = {}
        handler = listener.make_handler(CONFIG, state, client_ref)
        with mock.patch.object(listener, "lock_busy", return_value=False), \
             mock.patch.object(listener, "spawn_run") as mp_spawn:
            handler(make_event())  # 合法事件
        mp_spawn.assert_called_once()
        client.im.v1.message.create.assert_called_once()

    def test_handler_busy_lock_replies_busy_message(self):
        from unittest import mock
        client = mock.Mock()
        resp = mock.Mock()
        resp.success.return_value = True
        client.im.v1.message.create.return_value = resp
        client_ref = {"rest": client}
        handler = listener.make_handler(CONFIG, {}, client_ref)
        with mock.patch.object(listener, "lock_busy", return_value=True), \
             mock.patch.object(listener, "spawn_run") as mp_spawn:
            handler(make_event())
        mp_spawn.assert_not_called()
        # 确认消息应为锁占用文案
        sent_content = client.im.v1.message.create.call_args[0][0].request_body.content
        self.assertIn("已有任务运行中", sent_content)

    def test_handler_ignored_event_no_action(self):
        from unittest import mock
        client = mock.Mock()
        client_ref = {"rest": client}
        handler = listener.make_handler(CONFIG, {}, client_ref)
        with mock.patch.object(listener, "lock_busy") as mp_lock, \
             mock.patch.object(listener, "spawn_run") as mp_spawn:
            handler(make_event(text="闲聊内容"))
        mp_lock.assert_not_called()
        mp_spawn.assert_not_called()
        client.im.v1.message.create.assert_not_called()

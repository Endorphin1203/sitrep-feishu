# 飞书群 /news 触发即时新闻简报 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增飞书群"@机器人 /news"手动触发入口：本机 WebSocket 长连接监听群消息，触发现有新闻汇集流程并推送到群，与每日定时机制并存。

**Architecture:** 常驻 listener.py（lark-oapi WS 客户端）过滤群消息事件 → flock 探针预检 → 应用身份回确认消息 → spawn 现有 run_daily.sh（内部 flock 互斥）→ 简报由现有 webhook 机器人推送。launchd KeepAlive 托管监听进程。

**Tech Stack:** lark-oapi（飞书官方 Python SDK，WS 长连接）、fcntl flock、现有 venv（Python 3.9）、launchd。测试用标准库 unittest。

## Global Constraints

- 项目真实目录：`/Users/a1-6/feishu-milnews/`（`/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews` 是其软链；下文相对路径以真实目录为准）
- 非 git 仓库，不做 git init、不提交；每个任务的"验证"步骤即为完成标记
- 运行 Python 一律 `.venv/bin/python3`
- 密钥：listener_config.json 含 app_secret，权限必须 600；日志/终端不得打印 app_secret 明文
- 过滤规则：仅 `im.message.receive_v1` + chat_type=="group" + message_type=="text" + 剥@后精确等于 `/news` + chat_id 白名单 + 5分钟防抖
- 锁：`/tmp/milnews.lock`，flock 非阻塞；run_daily.sh 拿不到锁 exit 0 静默退出；listener 触发前用探针预检
- 确认消息文案（应用身份发群）：
  - 正常："收到，正在汇集近5小时新闻…"
  - 锁占用："已有任务运行中，请稍后再试"
- 用户已提供 App ID：`cli_aa1f8076bb789cde`、App Secret（控制器下发给 Task 1/3，不写入计划文档）
- 所有代码与注释使用中文

---

### Task 1: 依赖与配置脚手架

**Files:**
- Modify: `requirements.txt`（增加 lark-oapi）
- Create: `listener_config.json`（chmod 600）

**Interfaces:**
- Produces: `listener_config.json` 结构（后续任务依赖）：
  ```json
  {
    "app_id": "cli_aa1f8076bb789cde",
    "app_secret": "<由控制器下发>",
    "allowed_chat_ids": [],
    "dedup_seconds": 300
  }
  ```

- [ ] **Step 1: requirements.txt 增加 lark-oapi**

```
requests>=2.31
feedparser>=6.0
lark-oapi>=1.4.0
```

- [ ] **Step 2: 安装依赖到 venv**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/pip install -q "lark-oapi>=1.4.0" && .venv/bin/python3 -c "import lark_oapi; print('lark-oapi OK', lark_oapi.__version__ if hasattr(lark_oapi,'__version__') else '')"
```

Expected: 输出 `lark-oapi OK`

- [ ] **Step 3: 创建 listener_config.json 并收紧权限**

用文件编辑工具写入（app_secret 由控制器 dispatch 时下发，不得打印到终端）：

```json
{
  "app_id": "cli_aa1f8076bb789cde",
  "app_secret": "<真实secret>",
  "allowed_chat_ids": [],
  "dedup_seconds": 300
}
```

```bash
chmod 600 listener_config.json && stat -f "%Sp %N" listener_config.json
```

Expected: `-rw-------`

- [ ] **Step 4: 验证**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -c "
import json
cfg = json.load(open('listener_config.json'))
assert cfg['app_id'].startswith('cli_'), cfg['app_id']
assert len(cfg['app_secret']) > 10
assert cfg['dedup_seconds'] == 300
print('配置OK，secret长度', len(cfg['app_secret']))"
```

Expected: `配置OK，secret长度 32`

---

### Task 2: listener.py 过滤链纯函数（TDD）

**Files:**
- Create: `tests/test_listener.py`
- Create: `listener.py`（本任务只实现过滤链部分，动作与主循环留待 Task 3）

**Interfaces:**
- Produces（供 Task 3 使用，均为纯函数）：
  - `load_config() -> dict`
  - `extract_fields(event) -> dict`：返回 `{"chat_id": str|None, "chat_type": str|None, "message_type": str|None, "text": str, "sender_id": str|None}`（同时兼容 SDK 对象与 dict 事件）
  - `strip_mentions(text) -> str`（剥 `@_user_N` 标记）
  - `is_command(text) -> bool`（strip 后精确等于 `/news`）
  - `chat_allowed(chat_id, allowed) -> bool`
  - `dedup_ok(chat_id, sender_id, state, now, seconds) -> bool`（state 为 dict，就地更新）
  - `should_handle(event, config, state, now) -> tuple[bool, str|None]`：完整过滤链，返回 (是否处理, 忽略原因或None)

- [ ] **Step 1: 写失败测试 tests/test_listener.py**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -m unittest tests.test_listener -v
```

Expected: FAIL（`ModuleNotFoundError: No module named 'listener'`）

- [ ] **Step 3: 实现 listener.py 过滤链部分**

```python
#!/usr/bin/env python3
"""飞书群 /news 指令监听：过滤 -> 锁探针 -> 确认 -> 触发 run_daily.sh"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "listener_config.json"
RUN_SCRIPT = BASE / "run_daily.sh"
LOCK_PATH = "/tmp/milnews.lock"
LOG_PATH = BASE / "logs" / "listener.log"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get(obj, *keys, default=None):
    """嵌套取值：_get(d,'a','b') -> d['a']['b']；对象属性与dict键均可"""
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
        if cur is None:
            return default
    return cur


def extract_fields(event):
    """从 SDK 对象或 dict 事件中提取统一字段；content 解析失败则 text 为空串"""
    message = _get(event, "event", "message", default={}) or {}
    sender = _get(event, "event", "sender", default={}) or {}
    content = _get(message, "content", default="") or ""
    text = ""
    try:
        parsed = json.loads(content)
        text = parsed.get("text", "") if isinstance(parsed, dict) else ""
    except (ValueError, TypeError):
        pass
    return {
        "chat_id": _get(message, "chat_id"),
        "chat_type": _get(message, "chat_type"),
        "message_type": _get(message, "message_type"),
        "text": text,
        "sender_id": _get(sender, "sender_id", "open_id"),
    }


def strip_mentions(text):
    """剥除 @_user_N 提及标记"""
    import re
    return re.sub(r"@_user_\d+", "", text).strip()


def is_command(text):
    """严格等于 /news"""
    return text == "/news"


def chat_allowed(chat_id, allowed):
    return bool(chat_id) and chat_id in allowed


def dedup_ok(chat_id, sender_id, state, now, seconds):
    """防抖：同一(群,发送者)在seconds内仅放行一次；state就地更新"""
    key = (chat_id, sender_id)
    last = state.get(key)
    if last is not None and now - last < seconds:
        return False
    state[key] = now
    return True


def should_handle(event, config, state, now):
    """完整过滤链。返回 (是否处理, 忽略原因或None)"""
    f = extract_fields(event)
    if f["chat_type"] != "group":
        return False, "非群聊"
    if f["message_type"] != "text":
        return False, "非文本消息"
    if not is_command(strip_mentions(f["text"])):
        return False, "非/news指令"
    if not chat_allowed(f["chat_id"], config.get("allowed_chat_ids", [])):
        return False, "群不在白名单"
    if not dedup_ok(f["chat_id"], f["sender_id"], state, now,
                    config.get("dedup_seconds", 300)):
        return False, "防抖期内"
    return True, None


if __name__ == "__main__":
    # Task 3 实现主循环前，直接运行仅打印提示
    print("listener 主循环在 Task 3 实现")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -m unittest tests.test_listener -v
```

Expected: 15 个测试全部 PASS

---

### Task 3: listener.py 动作与主循环（TDD）

**Files:**
- Modify: `listener.py`（在 Task 2 基础上追加）
- Modify: `tests/test_listener.py`（追加测试类）

**Interfaces:**
- Consumes: Task 2 的全部纯函数；Task 1 的 listener_config.json
- Produces:
  - `lock_busy() -> bool`（flock 非阻塞探针 `/tmp/milnews.lock`）
  - `send_confirm(client, chat_id, text) -> bool`（im.message.create，send_as_bot，receive_id_type="chat_id"；client 为 None 时返回 False）
  - `spawn_run() -> None`（`subprocess.Popen(["bash", str(RUN_SCRIPT)])`）
  - `make_handler(config, state, client_ref) -> callable`（事件回调，组装过滤链+动作；client_ref 为单元素 dict，主循环创建 client 后注入）
  - `main()`：构建 `lark.EventDispatcherHandler`、`lark.ws.Client`（log_level=lark.LogLevel.INFO）、注入 client、`client.start()`

- [ ] **Step 1: 追加失败测试（tests/test_listener.py 末尾）**

```python
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
        client_ref = {"client": client}
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
        client_ref = {"client": client}
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
        client_ref = {"client": client}
        handler = listener.make_handler(CONFIG, {}, client_ref)
        with mock.patch.object(listener, "lock_busy") as mp_lock, \
             mock.patch.object(listener, "spawn_run") as mp_spawn:
            handler(make_event(text="闲聊内容"))
        mp_lock.assert_not_called()
        mp_spawn.assert_not_called()
        client.im.v1.message.create.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -m unittest tests.test_listener -v
```

Expected: FAIL（`AttributeError: module 'listener' has no attribute 'lock_busy'`）

- [ ] **Step 3: 实现动作与主循环（追加到 listener.py）**

```python
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 P2ImMessageReceiveV1)

BUSY_TEXT = "已有任务运行中，请稍后再试"
CONFIRM_TEXT = "收到，正在汇集近5小时新闻…"


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def lock_busy():
    """flock 非阻塞探针：锁空闲返回 False，被占用返回 True"""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def send_confirm(client, chat_id, text):
    """以应用身份向群发送 text 确认消息"""
    if client is None:
        return False
    try:
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(chat_id)
                             .msg_type("text")
                             .content(json.dumps({"text": text}))
                             .build())
               .build())
        resp = client.im.v1.message.create(req)
        if not resp.success():
            log(f"[warn] 确认消息失败: code={resp.code} msg={resp.msg}")
            return False
        return True
    except Exception as exc:
        log(f"[warn] 确认消息异常: {exc}")
        return False


def spawn_run():
    """分离执行 run_daily.sh（简报由 webhook 机器人推送）"""
    subprocess.Popen(["bash", str(RUN_SCRIPT)],
                     cwd=str(BASE),
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     start_new_session=True)


def make_handler(config, state, client_ref):
    """构造 im.message.receive_v1 事件回调"""
    def on_message(data: P2ImMessageReceiveV1):
        try:
            now = time.time()
            ok, why = should_handle(data, config, state, now)
            if not ok:
                log(f"[忽略] {why}")
                return
            f = extract_fields(data)
            log(f"[触发] chat={f['chat_id']} sender={f['sender_id']}")
            client = client_ref.get("client") if isinstance(client_ref, dict) else client_ref
            if lock_busy():
                send_confirm(client, f["chat_id"], BUSY_TEXT)
                log("[拒绝] 已有任务运行中")
                return
            send_confirm(client, f["chat_id"], CONFIRM_TEXT)
            spawn_run()
        except Exception as exc:
            log(f"[error] {exc}")
    return on_message


def main():
    config = load_config()
    if not config.get("allowed_chat_ids"):
        log("[warn] 白名单为空：任何群消息都不会触发；请先在联调时填入 chat_id")
    state = {}
    client_ref = {}
    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(make_handler(config, state, client_ref))
    client = lark.ws.Client(config["app_id"], config["app_secret"],
                            event_handler=builder.build(),
                            log_level=lark.LogLevel.INFO)
    client_ref["client"] = client
    log("listener 启动，等待飞书事件…")
    client.start()


if __name__ == "__main__":
    main()
```

注意：Task 2 已写的 `if __name__ == "__main__":` 段被本任务替换。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -m unittest tests.test_listener -v
```

Expected: 全部 PASS（原15 + 新8 = 23 个）

---

### Task 4: run_daily.sh 加 flock 锁

**Files:**
- Modify: `run_daily.sh`（在 `set -u` 之后、`cd` 之前插入锁段）
- Create: `tests/test_lock.sh`

**Interfaces:**
- Consumes: `/tmp/milnews.lock` 路径约定（与 listener.lock_busy 一致）
- Produces: 定时与手动触发共用互斥；锁占用时静默 exit 0

- [ ] **Step 1: 修改 run_daily.sh**

在 `set -u` 行之后、`cd "$(dirname "$0")" || exit 1` 之前插入：

```bash
# 互斥锁：定时与 /news 手动触发共用；锁占用则本次静默退出
LOCK=/tmp/milnews.lock
exec 9>"$LOCK"
flock -n 9 || { echo "已有任务运行中，本次触发退出"; exit 0; }
```

- [ ] **Step 2: 写失败测试 tests/test_lock.sh**

```bash
#!/bin/bash
# 测试：锁占用时第二个实例应快速静默退出（exit 0），且不产生新日志
set -u
cd "$(dirname "$0")/.." || exit 99
LOCK=/tmp/milnews.lock
# 先模拟持锁（不运行真实流程）
exec 8>"$LOCK"; flock -n 8 || { echo "SETUP FAIL"; exit 99; }
set +e
bash run_daily.sh
rc=$?
set -e
flock -u 8
echo "锁占用时退出码: $rc"
if [ "$rc" -ne 0 ]; then echo "FAIL: 预期0，实际$rc"; exit 1; fi
echo "PASS: 锁占用时静默退出"
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /Users/a1-6/feishu-milnews && bash tests/test_lock.sh
```

Expected: 若未加锁：`退出码: <非0或0且继续执行>`（脚本会真实跑流程，耗时长）——测试语义为验证互斥；未加锁时第二个实例会执行完整流程（FAIL 判定见输出）。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /Users/a1-6/feishu-milnews && bash tests/test_lock.sh
```

Expected: `锁占用时退出码: 0` 与 `PASS: 锁占用时静默退出`，且秒级返回（未执行完整流程）

- [ ] **Step 5: 全量回归（确认锁不影响正常路径）**

```bash
cd /Users/a1-6/feishu-milnews && .venv/bin/python3 -m unittest discover -s tests 2>&1 | tail -3
```

Expected: 全部 PASS（test_fetch/test_generate/test_push/test_listener 共 46 个）

---

### Task 5: launchd 监听任务

**Files:**
- Create: `launchd/com.a1-6.milnews-listener.plist`
- （复制到 ~/Library/LaunchAgents/ 并加载）

**Interfaces:**
- Consumes: listener.py 与 venv python 的绝对路径
- Produces: 常驻监听进程（KeepAlive 自动重启、RunAtLoad 开机自启）

- [ ] **Step 1: 写入 plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.a1-6.milnews-listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/a1-6/feishu-milnews/.venv/bin/python3</string>
        <string>/Users/a1-6/feishu-milnews/listener.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/a1-6/feishu-milnews</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/a1-6/feishu-milnews/logs/listener-launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/a1-6/feishu-milnews/logs/listener-launchd.err.log</string>
</dict>
</plist>
```

- [ ] **Step 2: 校验、安装、加载**

```bash
plutil -lint launchd/com.a1-6.milnews-listener.plist && \
cp launchd/com.a1-6.milnews-listener.plist ~/Library/LaunchAgents/ && \
launchctl bootout gui/$(id -u)/com.a1-6.milnews-listener 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.a1-6.milnews-listener.plist && \
sleep 3 && launchctl print gui/$(id -u)/com.a1-6.milnews-listener | grep -E "state|pid" | head -4
```

Expected: plutil OK；任务加载且 state = running（RunAtLoad）

- [ ] **Step 3: 验证启动日志与 KeepAlive**

```bash
sleep 2 && tail -5 logs/listener-launchd.out.log 2>/dev/null; echo "---KeepAlive验证：杀掉进程应自动重启---"
OLDPID=$(launchctl print gui/$(id -u)/com.a1-6.milnews-listener | awk '/pid =/{print $3; exit}')
kill $OLDPID; sleep 5
NEWPID=$(launchctl print gui/$(id -u)/com.a1-6.milnews-listener | awk '/pid =/{print $3; exit}')
echo "旧PID=$OLDPID 新PID=$NEWPID"
[ -n "$NEWPID" ] && [ "$NEWPID" != "$OLDPID" ] && echo "PASS: KeepAlive 自动重启生效"
```

Expected: 日志含"listener 启动，等待飞书事件…"；新 PID 存在且与旧 PID 不同

---

### Task 6: 端到端联调

**Files:** 无新增（必要时修改 listener_config.json 的白名单）

- [ ] **Step 1: 确认 WS 连接与事件可达**

查看 `logs/listener-launchd.out.log` 无报错；请在目标飞书群 **@机器人 发送任意文字**（如"测试"），然后：

```bash
sleep 2 && tail -5 /Users/a1-6/feishu-milnews/logs/listener.log
```

Expected: 日志出现 `[忽略] 群不在白名单` 之类的记录——说明事件已到达本机

- [ ] **Step 2: 从日志提取 chat_id 并写入白名单**

控制器从 listener.log 的忽略记录中提取 chat_id（`oc_` 开头），或临时把 listener.py 的忽略日志改为含 chat_id（已在 `[忽略]` 行有 why；若看不到 chat_id，可先读事件日志补打）。将 chat_id 填入 listener_config.json 的 `allowed_chat_ids`，然后重启监听：

```bash
launchctl kickstart -k gui/$(id -u)/com.a1-6.milnews-listener
```

- [ ] **Step 3: 群内 @机器人 /news 端到端验证**

您在群里发送 `@机器人 /news`。预期：
1. 秒级收到应用回复"收到，正在汇集近5小时新闻…"
2. 1-2 分钟内收到 webhook 机器人推送的简报（与定时简报样式一致）
3. `logs/listener.log` 出现 `[触发]` 行

- [ ] **Step 4: 防抖与错误文案验证**

- 连续两次发 `/news`：第二次应被防抖忽略（无新确认消息）
- 等待定时任务运行期间（07:30/12:00 后 1-2 分钟内）发 `/news`：应回复"已有任务运行中，请稍后再试"

- [ ] **Step 5: 稳定性观察**

连续运行 24 小时：listener 进程无崩溃（`launchctl print` state=running）；`logs/listener.log` 无 [error] 行；断网/睡眠恢复后 WS 自动重连（观察 launchd 日志）。与每日定时推送并存验证：07:30/12:00 定时简报正常到达。

---

## 计划自审记录

- **Spec 覆盖**：触发链（Task 3 handler）、过滤规则五项（Task 2 全部函数+测试）、锁探针与文案（Task 3 lock_busy + Task 4 flock + 测试断言文案）、防抖 5 分钟（dedup_ok/config）、launchd KeepAlive（Task 5）、联调 chat_id 白名单（Task 6 Step 1-2）、并发/稳定性验收（Task 6 Step 4-5）、密钥 600（Task 1）。
- **占位符**：无 TBD/TODO；app_secret 由控制器下发不写入计划（安全约定，非占位）。
- **类型一致性**：`extract_fields` 返回的字段名（chat_id/chat_type/message_type/text/sender_id）在 Task 2 定义、Task 3 handler 与全部测试中一致；`should_handle(event, config, state, now)` 签名在 Task 2 定义、Task 3 调用一致；锁路径 `/tmp/milnews.lock` 在 Task 3（lock_busy）与 Task 4（run_daily.sh）一致；确认文案常量 BUSY_TEXT/CONFIRM_TEXT 与 spec 一致。
- **越界检查**：Task 4 仅插入锁段不触碰其他逻辑；Task 3 替换 Task 2 的占位 `__main__` 已注明。

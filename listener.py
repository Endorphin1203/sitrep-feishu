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
                             .content(json.dumps({"text": text}, ensure_ascii=False))
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
            # REST 客户端（client.im.v1.*）与 WS 客户端分离：WS 客户端不暴露 REST 资源
            client = client_ref.get("rest") if isinstance(client_ref, dict) else client_ref
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
    # WS 客户端不暴露 REST 资源（无 client.im），REST 消息 API 需独立的 lark.Client
    rest_client = (lark.Client.builder()
                   .app_id(config["app_id"])
                   .app_secret(config["app_secret"])
                   .build())
    client_ref["rest"] = rest_client
    log("listener 启动，等待飞书事件…")
    client.start()


if __name__ == "__main__":
    main()

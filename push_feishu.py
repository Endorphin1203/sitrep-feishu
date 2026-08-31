#!/usr/bin/env python3
"""推送层：report.json -> 飞书 post 富文本（拆分+重试+告警）"""
import json, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
REPORT_PATH = BASE / "logs" / "last_report.json"
LIMIT = 18000  # 单条消息体字节上限（保守值）
US_EAST = ZoneInfo("America/New_York")


def load_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def fmt_us_time(iso):
    """UTC ISO -> 美东时间显示串"""
    dt = datetime.fromisoformat(iso).astimezone(US_EAST)
    return dt.strftime("%m月%d日 %H:%M")


def _window_title(report, part, total):
    w = report.get("window", {})
    start = fmt_us_time(w.get("start_utc", "")) if w.get("start_utc") else "?"
    end = fmt_us_time(w.get("end_utc", "")) if w.get("end_utc") else "?"
    title = f"美军新闻简报｜美东 {start}–{end}"
    return title + (f"（{part}/{total}）" if total > 1 else "")


TAG_NORMALIZE = {"⚠️": "⚠️警示", "❗": "❗对华直接影响", "🔬": "🔬技术借鉴"}


def _item_paragraph(it):
    """单条新闻 -> post 段落；仅接受 title/source/url/summary/tag 字段"""
    tag = TAG_NORMALIZE.get(it.get("tag") or "", it.get("tag") or "")
    text = f"▎{it['title']}（{it['source']}）"
    if tag:
        text += f" {tag}"
    if it.get("summary"):
        text += f"\n{it['summary']}"
    para = [{"tag": "text", "text": text}]
    if it.get("url"):
        para.append({"tag": "a", "text": "原文", "href": it["url"]})
    return para


def build_post_message(report, part, total):
    """report -> 单条飞书 post 消息体；part/total 用于拆分标识"""
    content = []
    for sec in report.get("sections", []):
        if not sec.get("items"):
            continue
        content.append([{"tag": "text", "text": f"【{sec['section']}】"}])
        for it in sec["items"]:
            content.append(_item_paragraph(it))
    if report.get("verdict"):
        content.append([{"tag": "text", "text": f"〔综合研判〕{report['verdict']}"}])
    return {"msg_type": "post",
            "content": {"post": {"zh_cn": {"title": _window_title(report, part, total),
                                           "content": content}}}}


def _msg_size(blocks, title):
    """估算消息体序列化体积（UTF-8 字节数）"""
    trial = {"msg_type": "post",
             "content": {"post": {"zh_cn": {"title": title, "content": blocks}}}}
    return len(json.dumps(trial, ensure_ascii=False).encode("utf-8"))


def split_parts(report, limit=LIMIT):
    """超限时按条目边界拆成多个 part（每条新闻完整保留在某个 part 内）"""
    full = build_post_message(report, 1, 1)
    if len(json.dumps(full, ensure_ascii=False).encode("utf-8")) <= limit:
        return [full]
    parts, current = [], []
    # 按最大后缀（99/99）预留标题体积，保证最终标题不会撑破限额
    probe = _window_title(report, 99, 99)
    for sec in report.get("sections", []):
        items = sec.get("items", [])
        if not items:
            continue
        header = [[{"tag": "text", "text": f"【{sec['section']}】"}]]
        for idx, it in enumerate(items):
            # 节标题与本节第一条同批；每条新闻不可再拆
            block = (header if idx == 0 else []) + [_item_paragraph(it)]
            if current and _msg_size(current + block, probe) > limit:
                parts.append(current)
                current = block
            else:
                current = current + block
    if report.get("verdict"):
        block = [[{"tag": "text", "text": f"〔综合研判〕{report['verdict']}"}]]
        if current and _msg_size(current + block, probe) > limit:
            parts.append(current)
            current = block
        else:
            current = current + block
    parts.append(current)
    total = len(parts)
    return [{"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": _window_title(report, i + 1, total), "content": p}}}}
        for i, p in enumerate(parts)]


def send(webhook, message):
    """单条发送；网络错误/响应异常重试3次（指数退避2/4/8秒）；HTTP 4xx 不重试"""
    for attempt in range(3):
        try:
            resp = requests.post(webhook, json=message, timeout=20)
            if resp.status_code != 200:
                print(f"[warn] webhook HTTP {resp.status_code}", file=sys.stderr)
                if 400 <= resp.status_code < 500:
                    return False  # 参数/密钥类错误重试无意义
            else:
                try:
                    if resp.json().get("code") == 0:  # 仅 code=0 视为成功（19001 等错误码一律失败重试）
                        return True
                except ValueError:
                    pass
        except Exception as exc:
            print(f"[warn] 第{attempt+1}次发送失败: {exc}", file=sys.stderr)
        time.sleep(2 * 2 ** attempt)  # 指数退避 2/4/8 秒
    return False


def send_alert(webhook, text):
    """失败告警（text 类型，独立于简报格式）"""
    return send(webhook, {"msg_type": "text", "content": {"text": text}})


def main():
    args = sys.argv[1:]
    webhook = load_config().get("feishu_webhook", "")
    if not webhook or "PASTE" in webhook:
        print("[error] config.json 未配置 feishu_webhook", file=sys.stderr)
        sys.exit(4)
    if args[:1] == ["--alert"] and len(args) == 2:
        ok = send_alert(webhook, f"【美军新闻简报系统】{args[1]}")
        sys.exit(0 if ok else 5)
    with open(REPORT_PATH, encoding="utf-8") as f:
        report = json.load(f)
    parts = split_parts(report)
    ok = True
    for i, part in enumerate(parts):
        if not send(webhook, part):
            print(f"[error] 第{i+1}条推送最终失败", file=sys.stderr)
            ok = False
        time.sleep(2)
    if not ok:
        send_alert(webhook, "今日简报部分内容推送失败，请检查日志")
        sys.exit(5)
    print(f"推送完成：{len(parts)}条消息")


if __name__ == "__main__":
    main()

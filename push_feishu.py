#!/usr/bin/env python3
"""推送层：report.json(V2) -> 飞书 post 富文本（拆分+重试+告警）"""
import json, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
REPORT_PATH = BASE / "logs" / "last_report.json"
LIMIT = 18000  # 单条消息体字节上限（保守值）
US_EAST = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")

TAG_NORMALIZE = {"⚠️": "⚠️警示", "❗": "❗对华直接影响", "🔬": "🔬技术借鉴"}


def load_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def _parse_utc(iso):
    """UTC ISO 字符串 -> 时区感知 datetime；容错 Z 后缀与非法输入（失败返回 None）

    模型输出的 window.start_utc/end_utc/day_start_utc 可能带 Z 后缀
    （如 2026-08-31T21:15:00Z），Python 3.9 的 fromisoformat 不识别，
    此处先 replace("Z", "+00:00") 再解析；解析失败兜底返回 None。"""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def fmt_edt(iso):
    """UTC ISO -> 美东时间显示串（MM月DD日 HH:MM）；解析失败返回「?」"""
    dt = _parse_utc(iso)
    if dt is None:
        return "?"
    return dt.astimezone(US_EAST).strftime("%m月%d日 %H:%M")


def fmt_bj(iso):
    """UTC ISO -> 北京时间显示串（M月D日HH:MM）；解析失败返回「?」"""
    dt = _parse_utc(iso)
    if dt is None:
        return "?"
    return dt.astimezone(BEIJING).strftime("%-m月%-d日%H:%M")


def _window_title(report, part, total):
    w = report.get("window", {})
    start, end = w.get("start_utc", ""), w.get("end_utc", "")
    title = f"截至 美国东部时间（EDT）{fmt_edt(end)}，本轮严格5小时窗口为 {fmt_edt(start)}—{fmt_edt(end)} EDT，对应北京时间 {fmt_bj(start)}—{fmt_bj(end)}"
    return title + (f"（{part}/{total}）" if total > 1 else "")


def _item_paragraph(idx, it):
    """V2 条目 -> post 段落：编号+9字段+原文链接"""
    lines = [
        f"{idx}. {it.get('cn_title', '')}",
        f"来源：{it.get('source', '')}",
        f"发布时间：美国东部时间{it.get('published_edt', '')}",
        f"标题：{it.get('title_en', '')}",
        f"中文标题：{it.get('title_cn', '')}",
        f"概述：{it.get('summary', '')}",
        f"对中国的直接影响：{it.get('china_impact', '')}",
        f"军事借鉴警示：{it.get('military_ref', '')}",
    ]
    para = [{"tag": "text", "text": "\n".join(lines)}]
    if it.get("url"):
        para.append({"tag": "a", "text": "原文", "href": it["url"]})
    return para


def _sources_paragraphs(sources):
    """来源清单：标题段 + 每条一个段落"""
    paras = [[{"tag": "text", "text": "【五、来源清单】"}]]
    for i, s in enumerate(sources or [], start=1):
        para = [{"tag": "text", "text": f"{i}. {s.get('name', '')}"}]
        if s.get("url"):
            para.append({"tag": "a", "text": s["url"], "href": s["url"]})
        paras.append(para)
    return paras


def build_post_message(report, part, total):
    """report(V2) -> 单条飞书 post 消息体；part/total 用于拆分标识"""
    content = []
    for sec in report.get("sections", []):
        if not sec.get("items"):
            continue
        content.append([{"tag": "text", "text": f"【{sec['section']}】"}])
        for i, it in enumerate(sec["items"], start=1):
            content.append(_item_paragraph(i, it))
    if report.get("sources"):
        content.extend(_sources_paragraphs(report["sources"]))
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
    """超限时按条目边界拆成多个 part（每条新闻完整保留在某个 part 内）；
    来源清单与研判放在最后一个 part"""
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
        for idx, it in enumerate(items, start=1):
            # 节标题与本节第一条同批；每条新闻不可再拆
            block = (header if idx == 1 else []) + [_item_paragraph(idx, it)]
            if current and _msg_size(current + block, probe) > limit:
                parts.append(current)
                current = block
            else:
                current = current + block
    if report.get("sources"):
        block = _sources_paragraphs(report["sources"])
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

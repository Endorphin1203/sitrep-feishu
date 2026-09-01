#!/usr/bin/env python3
"""生成层：raw_items + 提示词 -> DeepSeek API -> logs/last_report.json（V2五部分格式）"""
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
RAW_PATH = BASE / "logs" / "last_raw_items.json"
OUT_PATH = BASE / "logs" / "last_report.json"
API_URL = "https://api.deepseek.com/chat/completions"
US_EAST = ZoneInfo("America/New_York")

SECTION_NAMES = [
    "一、严格5小时窗口内",
    "二、当天00:00至窗口起点的重要军事动态",
    "三、当天最值得关注的美国涉华军事舆论与战略分析",
    "四、当天智库分析文章",
    "五、来源清单",
]
ITEM_FIELDS = ["cn_title", "source", "published_edt", "title_en", "title_cn",
               "summary", "china_impact", "military_ref", "url"]

SYSTEM_PROMPT = """你是军事战略情报分析助手。基于提供的新闻条目（24小时抓取），产出中文简报JSON。

时间锚点（美国东部时间EDT）：
- 严格窗口起点S={S}、终点T={T}（S=T-5小时）
- 当天起点D={D}（美东当天00:00）

sections 五部分（顺序固定，section 名必须与给定完全一致）：
1. "一、严格5小时窗口内"——发布时间在 S 与 T 之间的条目
2. "二、当天00:00至窗口起点的重要军事动态"——发布时间在 D 与 S 之间的条目
3. "三、当天最值得关注的美国涉华军事舆论与战略分析"——当天涉华主题条目（可跨窗口，与一二部分可有少量重叠）
4. "四、当天智库分析文章"——当天智库/研究机构/媒体深度分析类文章
5. "五、来源清单"——此部分 items 置空数组

每部分（第五部分除外）4-6条精选。筛选标准：对军队有战略价值、有借鉴参考意义、警示警惕类、对中国有直接影响。
输出量硬约束（防止超出API上限被截断）：summary 不超过2句；全篇JSON总长度控制在5000个token以内；条目较多时优先保留发布时间最新与战略价值最高的条目。
每条 item 9个字段：
- cn_title：中文短标题（20-30字，概括核心）
- source：来源媒体名
- published_edt：发布时间（美国东部时间，格式 YYYY-MM-DD HH:MM）
- title_en：新闻原英文标题（保留原文）
- title_cn：中文标题（英文标题的翻译）
- summary：概述（2-3句中文）
- china_impact：对中国的直接影响（无则写"暂无直接关联"）
- military_ref：军事借鉴警示（无则写"暂无"）
- url：原文链接（必须来自输入数据）

顶层 sources 字段：前四部分引用过的全部来源去重汇总，数组 [{"name":"媒体名","url":"来源首页链接"}]（url 可用输入链接的域名首页）。
verdict：100字以内综合研判。
时间切分规则：将条目发布时间（UTC）换算为EDT后按锚点归类。不得编造条目；所有事实只能来自输入数据。
输出严格为JSON对象：{"window":{"start_utc":"","end_utc":""},"day_start_utc":"","sections":[{"section":"一、严格5小时窗口内","items":[{"cn_title":"","source":"","published_edt":"","title_en":"","title_cn":"","summary":"","china_impact":"","military_ref":"","url":""}]}],"sources":[{"name":"","url":""}],"verdict":""}"""


def load_raw():
    with open(RAW_PATH, encoding="utf-8") as f:
        return json.load(f)


def compute_anchors(now_utc):
    """计算时间锚点的 EDT 表示：T=now、S=T-5h、D=窗口起点S所在美东日的00:00。

    注意：D 必须按 S 所在日计算而非 T 所在日——美东凌晨运行时 S 落在前一天，
    若 D 取 T 所在日00:00 会晚于 S，第二部分区间 [D,S) 失效。"""
    t_east = now_utc.astimezone(US_EAST)
    s_east = (now_utc - timedelta(hours=5)).astimezone(US_EAST)
    d_east = s_east.replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%d %H:%M"
    return {"T_edt": t_east.strftime(fmt), "S_edt": s_east.strftime(fmt),
            "D_edt": d_east.strftime(fmt)}


def build_prompt(items, now_utc):
    anchors = compute_anchors(now_utc)
    # 注：提示词含 JSON 示例花括号，str.format 会误解析，故用 replace 填充锚点占位符
    system = SYSTEM_PROMPT.replace("{S}", anchors["S_edt"]).replace("{T}", anchors["T_edt"]).replace("{D}", anchors["D_edt"])
    lines = [f"{i+1}. [{it['source']}] {it['title']}\n   时间(UTC):{it['published_utc']} 链接:{it['url']}\n   摘要:{it['summary']}"
             for i, it in enumerate(items)]
    body = "\n".join(lines) or "（无条目）"
    return (f"时间锚点：窗口起点S={anchors['S_edt']}，窗口终点T={anchors['T_edt']}，当天起点D={anchors['D_edt']}（均为EDT）。"
            f"条目发布时间为UTC，请自行换算EDT后归类。\n以下为24小时窗口内新闻条目：\n{body}"), system


def call_deepseek(prompt, api_key, system):
    payload = {"model": "deepseek-chat", "temperature": 0.3, "max_tokens": 8192,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}]}
    resp = requests.post(API_URL,
                         headers={"Authorization": f"Bearer {api_key}",
                                  "Content-Type": "application/json"},
                         json=payload, timeout=180)
    if resp.status_code == 402:
        raise RuntimeError("DeepSeek 余额不足(402)，请充值后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def parse_report(text):
    """剥离可能的 markdown 围栏后解析 JSON"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    return json.loads(text)


def _parse_utc(iso):
    """UTC ISO -> aware datetime；容错 Z 后缀与 naive 输入（契约是 UTC，naive 假定 UTC）；失败返回 None"""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_window(now_utc):
    """程序化计算 window 与 day_start_utc（模型输出的时间字段格式不可靠，一律覆写）。

    day_start_utc = 窗口起点 S 所在美东日的00:00 的 UTC 表示。"""
    s_edt = (now_utc - timedelta(hours=5)).astimezone(US_EAST)
    d_edt = s_edt.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"window": {"start_utc": (now_utc - timedelta(hours=5)).isoformat(),
                       "end_utc": now_utc.isoformat()},
            "day_start_utc": d_edt.astimezone(timezone.utc).isoformat()}


def _parse_edt(s):
    """'YYYY-MM-DD HH:MM' -> EDT datetime；解析失败返回 None"""
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=US_EAST)
    except (ValueError, TypeError):
        return None


def rebalance_sections(obj):
    """程序化兜底：按发布时间修正第一/二部分条目归属（模型切分偶发越界）。

    规则：第一条目必须 ∈ [S, T]（严格5小时窗口）；第二条目必须 ∈ [D, S)
    （当天00:00至窗口起点）；早于当天00:00的条目移除（24h抓取数据中的昨日残留，
    不在简报范围）。第三/四部分为主题分类，不做时间重排。
    返回 (obj, stats)。"""
    w = obj.get("window", {})
    start = _parse_utc(w.get("start_utc", ""))
    end = _parse_utc(w.get("end_utc", ""))
    stats = {"moved_to_sec1": 0, "moved_to_sec2": 0, "removed": 0}
    if start is None or end is None:
        return obj, stats
    s_edt = start.astimezone(US_EAST)
    t_edt = end.astimezone(US_EAST)
    # 当天起点 D：窗口起点 S 所在美东日的00:00（与 compute_anchors 一致；
    # 不依赖模型输出的 day_start_utc，凌晨运行时更可靠）
    d_edt = s_edt.replace(hour=0, minute=0, second=0, microsecond=0)
    sec1 = obj["sections"][0]
    sec2 = obj["sections"][1]
    orig1 = {id(it) for it in sec1.get("items", [])}
    orig2 = {id(it) for it in sec2.get("items", [])}
    new1, new2 = [], []
    for it in list(sec1.get("items", [])) + list(sec2.get("items", [])):
        t = _parse_edt(it.get("published_edt", ""))
        if t is None:
            (new1 if id(it) in orig1 else new2).append(it)
        elif s_edt <= t <= t_edt:
            new1.append(it)
        elif (d_edt is None or d_edt <= t) and t < s_edt:
            new2.append(it)
        else:
            stats["removed"] += 1
    stats["moved_to_sec1"] = sum(1 for it in new1 if id(it) not in orig1)
    stats["moved_to_sec2"] = sum(1 for it in new2 if id(it) not in orig2)
    sec1["items"] = new1
    sec2["items"] = new2
    return obj, stats


def validate_report(obj):
    """返回问题列表；空列表=合法（V2 schema）"""
    problems = []
    if not isinstance(obj, dict):
        return ["报告不是JSON对象"]
    if "verdict" not in obj or not isinstance(obj.get("verdict"), str):
        problems.append("缺少verdict")
    if not isinstance(obj.get("window"), dict) or not all(
            isinstance(obj["window"].get(k), str) for k in ("start_utc", "end_utc")):
        problems.append("window缺失或字段类型错误")
    if not isinstance(obj.get("day_start_utc"), str):
        problems.append("缺少day_start_utc")
    secs = obj.get("sections")
    if not isinstance(secs, list) or len(secs) != 5:
        problems.append(f"sections必须为5个，实际{len(secs) if isinstance(secs, list) else '非列表'}")
        return problems
    for i, sec in enumerate(secs):
        if not isinstance(sec, dict) or sec.get("section") != SECTION_NAMES[i]:
            problems.append(f"第{i+1}节section名不匹配: {sec.get('section') if isinstance(sec, dict) else '非dict'}")
        for it in sec.get("items", []) if isinstance(sec, dict) else []:
            if not isinstance(it, dict):
                problems.append(f"第{i+1}节存在非dict条目")
                continue
            for key in ITEM_FIELDS:
                if not isinstance(it.get(key), str):
                    problems.append(f"条目缺少字段{key}: {it.get('cn_title', '?')}")
    srcs = obj.get("sources")
    if not isinstance(srcs, list):
        problems.append("缺少sources数组")
    else:
        for s in srcs:
            if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(s.get("url"), str):
                problems.append(f"sources存在非法条目: {s}")
    return problems


def main():
    raw = load_raw()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or load_env_key()
    if not api_key or api_key.startswith("PASTE"):
        print("[error] .env 中未配置 DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(3)
    now = datetime.fromisoformat(raw["fetched_at_utc"])
    prompt, system = build_prompt(raw["items"], now)
    for attempt in (1, 2):
        try:
            obj = parse_report(call_deepseek(prompt, api_key, system))
            if not validate_report(obj):
                obj.update(compute_window(now))  # window/day_start 以程序计算为准，覆写模型输出
                obj, stats = rebalance_sections(obj)
                if any(stats.values()):
                    print(f"时间重排兜底: 移入第一部分{stats['moved_to_sec1']}条, "
                          f"移入第二部分{stats['moved_to_sec2']}条, 移除{stats['removed']}条")
                obj["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
                with open(OUT_PATH, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
                print(f"报告生成成功：{len(obj.get('sections', []))}节")
                return
        except Exception as exc:
            print(f"[warn] 第{attempt}次生成失败: {exc}", file=sys.stderr)
    sys.exit(3)


def load_env_key():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


if __name__ == "__main__":
    main()

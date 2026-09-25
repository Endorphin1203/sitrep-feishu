#!/usr/bin/env python3
"""生成层：raw_items + 提示词 -> DeepSeek API -> logs/last_report.json（V2五部分格式）"""
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = Path(__file__).resolve().parent
RAW_PATH = BASE / "logs" / "last_raw_items.json"
OUT_PATH = BASE / "logs" / "last_report.json"
API_URL = "https://api.deepseek.com/chat/completions"
US_EAST = ZoneInfo("America/New_York")

# 带自动重试的会话：v4-pro 思考时间长（1-3分钟），连接/读流中断（Response ended
# prematurely）时自动重试2次，覆盖代理网络下的中途断流
SESSION = requests.Session()
RETRY = Retry(total=2, connect=2, read=2, backoff_factor=2,
              status_forcelist=(500, 502, 503, 504))
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY))
SESSION.mount("http://", HTTPAdapter(max_retries=RETRY))

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

时间锚点（美国东部时间EDT）：见用户消息中的 S/T/D 定义——S=严格窗口起点、T=窗口终点（S=T-5小时）、D=当天起点（美东当天00:00）。本系统提示词保持完全静态以命中 API 前缀缓存，所有动态数值只出现在用户消息中。

sections 五部分（顺序固定，section 名必须与给定完全一致）：
1. "一、严格5小时窗口内"——发布时间在 S 与 T 之间的条目
2. "二、当天00:00至窗口起点的重要军事动态"——发布时间在 D 与 S 之间的条目
3. "三、当天最值得关注的美国涉华军事舆论与战略分析"——美国侧的涉华内容：美方官员/军方对华表态、美国对华政策与制裁动向、美智库涉华报告、美国媒体对华战略分析。**不收录中国自身动向（即使由美国媒体转述）：如中国修改法律、解放军训练部署、中国官员与情报机构表态、中国武器出口或使用等，一律排除**。此部分条目的 cn_title 应以美国侧视角撰写（如"美媒聚焦中国战时动员法修订"而非"中国修改法律"）。
4. "四、当天智库分析文章"——当天智库/研究机构/媒体深度分析类文章
5. "五、来源清单"——此部分 items 置空数组

来源池与可信度（筛选的输入基础，而非硬性白名单）：优先扫描四类来源——①美军与美国政府官方渠道（国防部及各军种官网、DARPA、DIU）；②专业防务媒体（Defense News、Breaking Defense、The War Zone、USNI News、Air & Space Forces Magazine、Stars and Stripes 等）；③主流新闻媒体（Reuters、AP 等）；④智库与研究机构（CSIS、RAND、Hudson、Atlantic Council、Brookings、CNAS 等）。可信度排序：官方与一手采访优先；同一事件被多源报道时，优先保留原始来源或专业度更高的来源。

每部分（第五部分除外）4-6条精选。筛选标准（六维，同时满足）：
1. 时间：严格限定在各部分给定的时间锚点内
2. 相关性：与美军、国防政策、军工、军力建设、作战概念、前沿技术、联盟体系、太空/网络/无人系统直接相关
3. 战略价值：可能影响作战能力、组织体系、装备建设、后勤、基地生存、指挥控制——排除普通人事任免、纪念活动、生活类军闻
4. 对中国影响：优先涉及印太、台海、第一岛链、关岛、日本、菲律宾、澳大利亚、太空、导弹防御、远程火力、无人作战
5. 借鉴或警示意义：美军从实战总结的经验教训——基地防御、战损维修、弹药库存、无人系统运用、AI指挥等
6. 来源可信度：按"来源池与可信度"排序取舍

筛选范围约束（必须严格遵守）：只收录"美国侧"的新闻——美军动态、美国国防政策、美国舆论、美国前沿军事技术、美国智库分析。不收录中国军事动向类条目（如解放军训练部署、中国防务政策与法律修改、中国情报机构表态、中国武器发展/出口/使用等中国自身动向），**即使它们出现在输入数据中、即使由美国媒体转述，也一律排除**。第三部分"美国涉华舆论与战略分析"收录的是美国侧的涉华内容（美方表态、美对华政策、美智库涉华报告），而非中国自身在做什么。
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
    # SYSTEM_PROMPT 完全静态（锚点只在用户消息中）——DeepSeek 前缀缓存可稳定命中系统提示词
    system = SYSTEM_PROMPT
    lines = [f"{i+1}. [{it['source']}] {it['title']}\n   时间(UTC):{it['published_utc']} 链接:{it['url']}\n   摘要:{it['summary']}"
             for i, it in enumerate(items)]
    body = "\n".join(lines) or "（无条目）"
    return (f"时间锚点：窗口起点S={anchors['S_edt']}，窗口终点T={anchors['T_edt']}，当天起点D={anchors['D_edt']}（均为EDT）。"
            f"条目发布时间为UTC，请自行换算EDT后归类。\n以下为24小时窗口内新闻条目：\n{body}"), system


def call_deepseek(prompt, api_key, system):
    payload = {"model": "deepseek-v4-pro", "temperature": 0.3, "max_tokens": 32768,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}]}
    resp = SESSION.post(API_URL,
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"},
                        json=payload, timeout=180)
    if resp.status_code == 402:
        raise RuntimeError("DeepSeek 余额不足(402)，请充值后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    content = (data["choices"][0]["message"].get("content") or "").strip()
    if not content:
        # 推理模型可能思考超限导致内容为空，带诊断信息抛出便于排查
        raise RuntimeError(f"模型返回空内容: finish_reason={data['choices'][0].get('finish_reason')} "
                           f"usage={data.get('usage')}")
    usage = data.get("usage", {})
    print(f"[usage] prompt={usage.get('prompt_tokens', '?')} "
          f"(缓存命中{usage.get('prompt_cache_hit_tokens', 0)}) "
          f"completion={usage.get('completion_tokens', '?')} "
          f"(思考{usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)})", file=sys.stderr)
    return content


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


CHINA_SIDE_RE = [
    re.compile(r"^(美媒[^中]{0,10})?(中国|解放军|北京|中方|中共)"),
    re.compile(r"中国制.{0,6}(导弹|武器|无人机|装备)"),
    re.compile(r"习近平|Xi Jinping|习主席", re.IGNORECASE),
]


def filter_china_items(obj):
    """硬过滤：移除内容主体为中国自身动向的条目（提示词软约束的确定性兜底）。

    判定基于 cn_title+title_en 的文本模式：剥离"美媒："前缀后以中国/解放军/
    北京/中方/中共开头（中国为主体），或含"中国制XX武器"（中国武器出口/使用）。
    返回 (obj, removed_count)。"""
    removed = 0
    for sec in obj.get("sections", []):
        kept = []
        for it in sec.get("items", []):
            text = f"{it.get('cn_title', '')} {it.get('title_en', '')}"
            if any(r.search(text) for r in CHINA_SIDE_RE):
                removed += 1
            else:
                kept.append(it)
        sec["items"] = kept
    return obj, removed


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
                obj, china_removed = filter_china_items(obj)
                if china_removed:
                    print(f"中国动向硬过滤: 移除{china_removed}条")
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

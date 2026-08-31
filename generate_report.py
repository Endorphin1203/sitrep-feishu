#!/usr/bin/env python3
"""生成层：raw_items + 提示词 -> DeepSeek API -> logs/last_report.json"""
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
RAW_PATH = BASE / "logs" / "last_raw_items.json"
OUT_PATH = BASE / "logs" / "last_report.json"
API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """你是军事战略情报分析助手。基于提供的近5小时新闻条目，产出中文简报JSON，要求：
1. sections 按五类组织（顺序固定）：一、作战行动与部署 / 二、前沿军事技术 / 三、国防与AI政策 / 四、涉美军舆论与警示 / 五、智库分析
2. 筛选标准：对军队有战略价值、有借鉴参考意义、警示警惕类、对中国有直接影响
3. 每条 item 含：title（保留英文原标题）、source（来源媒体名）、url（原文链接，必须来自输入数据）、summary（中文要点，≤2句）、tag（取值仅限：❗对华直接影响/⚠️警示/🔬技术借鉴/空字符串）
4. verdict：100字以内综合研判
5. 无相关内容的类别输出空数组；不得编造条目；所有事实只能来自输入数据
输出严格为JSON对象：{"window":{"start_utc":"","end_utc":""},"sections":[{"section":"一、作战行动与部署","items":[{"title":"","source":"","url":"","summary":"","tag":""}]}],"verdict":""}"""


def load_raw():
    with open(RAW_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_prompt(items, now_utc):
    start = now_utc - timedelta(hours=5)
    lines = [f"{i+1}. [{it['source']}] {it['title']}\n   时间:{it['published_utc']} 链接:{it['url']}\n   摘要:{it['summary']}"
             for i, it in enumerate(items)]
    body = "\n".join(lines) or "（无条目）"
    return (f"时间窗口：{start.isoformat()} 至 {now_utc.isoformat()}（UTC）。"
            f"以下为窗口内新闻条目：\n{body}")


def call_deepseek(prompt, api_key):
    payload = {"model": "deepseek-chat", "temperature": 0.3,
               "max_tokens": 8192,  # 默认4096会被截断（60条目简报超限），显式用满上限
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}]}
    resp = requests.post(API_URL,
                         headers={"Authorization": f"Bearer {api_key}",
                                  "Content-Type": "application/json"},
                         json=payload, timeout=120)
    if resp.status_code == 402:
        raise RuntimeError("DeepSeek 余额不足(402)，请充值后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def parse_report(text):
    """剥离可能的 markdown 围栏后解析 JSON"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    return json.loads(text)


def validate_report(obj):
    """返回问题列表；空列表=合法。合法即推送给飞书，不合法则重试"""
    problems = []
    if not isinstance(obj, dict):
        return ["报告不是JSON对象"]
    if "verdict" not in obj or not isinstance(obj.get("verdict"), str):
        problems.append("缺少verdict")
    win = obj.get("window")
    if not isinstance(win, dict):
        problems.append("缺少window对象")
    else:
        for key in ("start_utc", "end_utc"):
            if not isinstance(win.get(key), str):
                problems.append(f"window缺少字符串字段{key}")
    secs = obj.get("sections")
    if not isinstance(secs, list):
        problems.append("缺少sections数组")
        return problems
    for sec in secs:
        if not isinstance(sec, dict):
            problems.append("某节不是JSON对象")
            continue
        if not isinstance(sec.get("section"), str):
            problems.append("某节缺少section名")
        items = sec.get("items")
        if not isinstance(items, list):
            problems.append("某节缺少items数组")
            continue
        for it in items:
            if not isinstance(it, dict):
                problems.append("条目不是JSON对象")
                continue
            for key in ("title", "source", "url", "summary", "tag"):
                if not isinstance(it.get(key), str):
                    problems.append(f"条目缺少字段{key}: {it.get('title', '?')}")
    return problems


def main():
    raw = load_raw()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or load_env_key()
    if not api_key or api_key.startswith("PASTE"):
        print("[error] .env 中未配置 DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(3)
    prompt = build_prompt(raw["items"], datetime.fromisoformat(raw["fetched_at_utc"]))
    for attempt in (1, 2):
        try:
            obj = parse_report(call_deepseek(prompt, api_key))
            if not validate_report(obj):
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

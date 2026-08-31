# 飞书定时推送美军新闻简报 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建每天 07:30 / 12:00 自动抓取近5小时美军相关新闻、经 DeepSeek API 生成中文简报并推送飞书群的本机自动化系统。

**Architecture:** 三层独立 Python 脚本（fetch_news → generate_report → push_feishu）+ run_daily.sh 薄编排，launchd 双时刻调度。RSS 确定性抓取新闻，DeepSeek 只做汇总分析，飞书 post 富文本推送。

**Tech Stack:** macOS 系统 Python 3.9 + venv、requests、feedparser、DeepSeek API（openai 兼容 HTTP）、飞书群自定义机器人 webhook、launchd。测试用标准库 unittest（不引入 pytest）。

## Global Constraints

- 项目根目录：`/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews/`（下文所有相对路径以此为准）
- 本目录非 git 仓库，**不做 git init、不提交**；每个任务的"验证"步骤即为完成标记
- 运行 Python：`feishu-milnews/.venv/bin/python3`（venv 内安装 requests、feedparser）
- 密钥文件 `config.json` 与 `.env` 权限必须 `chmod 600`，绝不打印 webhook URL 与 API Key 全文
- 时间处理一律 UTC 存储、展示时换算美东（zoneinfo `America/New_York`，自动处理夏令时）
- 时间窗口：运行时刻往前 5 小时；抓取上限 60 条
- DeepSeek 调用：`https://api.deepseek.com/chat/completions`，model `deepseek-chat`，`response_format: {"type":"json_object"}`，temperature 0.3，timeout 120s
- 飞书消息：`msg_type: post` 富文本；单条消息体超 18000 字节按条目边界拆多条，间隔 2 秒；失败重试 3 次（指数退避 2/4/8 秒）
- 推送时刻（本机时区）：07:30 与 12:00；整体运行超时 10 分钟
- 所有代码与注释使用中文

---

### Task 1: 项目脚手架与配置模板

**Files:**
- Create: `feishu-milnews/requirements.txt`
- Create: `feishu-milnews/config.json`
- Create: `feishu-milnews/.env`
- Create: `feishu-milnews/logs/.gitkeep`（空目录占位）
- Create: `feishu-milnews/launchd/com.a1-6.milnews.plist`

**Interfaces:**
- Produces: `config.json` 结构（后续所有任务依赖）：
  - `google_news_queries`: 5 个关键词组字符串（见下）
  - `expert_feeds`: 专业信源 RSS URL 列表
  - `max_items`: 60
  - `window_hours`: 5
  - `feishu_webhook`: 飞书 webhook URL（由用户提供后填入）
  - `.env` 提供 `DEEPSEEK_API_KEY=xxx`

- [ ] **Step 1: 创建 requirements.txt**

```
requests>=2.31
feedparser>=6.0
```

- [ ] **Step 2: 创建 config.json 模板**

```json
{
  "google_news_queries": [
    "\"US military\" OR Pentagon OR \"US Army\" OR \"US Navy\"",
    "China military OR Taiwan OR \"South China Sea\"",
    "\"defense technology\" OR \"military technology\" OR drone OR hypersonic",
    "think tank OR CSIS OR RAND China military",
    "\"US military\" controversy OR criticism OR readiness"
  ],
  "expert_feeds": [
    "https://www.defensenews.com/arc/outboundfeeds/rss/",
    "https://breakingdefense.com/feed/",
    "https://www.militarytimes.com/arc/outboundfeeds/rss/",
    "https://www.twz.com/rss",
    "https://news.usni.org/feed",
    "https://warontherocks.com/feed/",
    "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20",
    "https://www.csis.org/rss.xml",
    "https://www.rand.org/latest/rss.xml"
  ],
  "max_items": 60,
  "window_hours": 5,
  "feishu_webhook": "PASTE_YOUR_FEISHU_WEBHOOK_URL_HERE"
}
```

- [ ] **Step 3: 创建 .env 模板并收紧权限**

```bash
mkdir -p feishu-milnews/logs feishu-milnews/launchd feishu-milnews/tests/fixtures
printf 'DEEPSEEK_API_KEY=PASTE_YOUR_KEY_HERE\n' > feishu-milnews/.env
chmod 600 feishu-milnews/.env feishu-milnews/config.json
touch feishu-milnews/logs/.gitkeep
```

- [ ] **Step 4: 创建 venv 并安装依赖**

```bash
cd feishu-milnews && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && .venv/bin/python3 -c "import requests, feedparser; print('依赖OK')"
```

Expected: 输出 `依赖OK`

- [ ] **Step 5: 向用户索取两个密钥并填入**

向用户请求：①飞书群自定义机器人 webhook URL；②DeepSeek API Key（并请确认余额>0）。分别替换 config.json 中 `PASTE_YOUR_FEISHU_WEBHOOK_URL_HERE` 与 .env 中 `PASTE_YOUR_KEY_HERE`。若用户暂未提供，用占位符继续后续任务，在 Task 7 前补齐。

- [ ] **Step 6: 验证**

```bash
ls -la feishu-milnews/ feishu-milnews/logs/ && stat -f "%Sp %N" feishu-milnews/config.json feishu-milnews/.env
```

Expected: 目录齐全；config.json 与 .env 权限为 `-rw-------`

---

### Task 2: fetch_news.py 抓取层

**Files:**
- Create: `feishu-milnews/fetch_news.py`
- Create: `feishu-milnews/tests/test_fetch.py`
- Create: `feishu-milnews/tests/fixtures/sample_rss.xml`

**Interfaces:**
- Consumes: `config.json` 的 `google_news_queries`、`expert_feeds`、`max_items`、`window_hours`
- Produces（供 Task 3 使用）：
  - 函数 `fetch_all_items(config, now_utc) -> list[dict]`：每条 `{"title","url","source","published_utc","summary"}`，`published_utc` 为 ISO8601 UTC 字符串
  - 函数 `filter_window(items, now_utc, hours) -> list[dict]`
  - 函数 `dedupe(items) -> list[dict]`
  - 主程序入口 `main()`：合并抓取 → 过滤 → 去重 → 时间倒序 → 截断 max_items → 写 `logs/last_raw_items.json`（含 `{"fetched_at_utc":..., "items":[...]}`）

- [ ] **Step 1: 写失败测试 tests/test_fetch.py**

```python
"""fetch_news.py 单元测试（使用本地 fixture，不联网）"""
import json, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import fetch_news

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

def parse_fixture(path):
    """用 feedparser 解析本地 XML fixture，等价于 fetch_news 的解析逻辑入口"""
    import feedparser
    return feedparser.parse(str(BASE / "tests" / "fixtures" / path))

class TestDedupe(unittest.TestCase):
    def test_same_title_kept_once(self):
        items = [
            {"title": "US Navy tests new drone", "url": "https://a.com/1", "source": "A", "published_utc": "", "summary": ""},
            {"title": "US Navy tests new drone!", "url": "https://b.com/2", "source": "B", "published_utc": "", "summary": ""},
        ]
        out = fetch_news.dedupe(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.com/1")

class TestFilterWindow(unittest.TestCase):
    def test_older_than_5h_dropped(self):
        items = [
            {"title": "new", "published_utc": (NOW - timedelta(hours=1)).isoformat()},
            {"title": "old", "published_utc": (NOW - timedelta(hours=6)).isoformat()},
            {"title": "bad time", "published_utc": "not-a-time"},
        ]
        out = fetch_news.filter_window(items, NOW, 5)
        self.assertEqual([i["title"] for i in out], ["new"])

class TestParseFeed(unittest.TestCase):
    def test_parse_rss_extracts_fields(self):
        feed = parse_fixture("sample_rss.xml")
        items = fetch_news.extract_items(feed, "测试媒体")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Pentagon announces laser deployment")
        self.assertEqual(items[0]["source"], "测试媒体")
        self.assertEqual(items[0]["url"], "https://example.com/1")
        self.assertEqual(items[0]["published_utc"], "2026-08-31T08:00:00+00:00")
```

- [ ] **Step 2: 创建 fixture tests/fixtures/sample_rss.xml**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Sample</title>
<item>
  <title>Pentagon announces laser deployment</title>
  <link>https://example.com/1</link>
  <description>An item about directed energy.</description>
  <pubDate>Sun, 31 Aug 2026 08:00:00 GMT</pubDate>
</item>
<item>
  <title>Carrier transits Singapore Strait</title>
  <link>https://example.com/2</link>
  <description>An item about naval moves.</description>
  <pubDate>Sun, 31 Aug 2026 07:00:00 GMT</pubDate>
</item>
</channel></rss>
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_fetch -v
```

Expected: FAIL（`ModuleNotFoundError: No module named 'fetch_news'`）

- [ ] **Step 4: 实现 fetch_news.py**

```python
#!/usr/bin/env python3
"""抓取层：Google News RSS + 专业信源 RSS -> logs/last_raw_items.json"""
import json, re, sys, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser, requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
OUT_PATH = BASE / "logs" / "last_raw_items.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
GN_TEMPLATE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en&when:{h}h"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_google_urls(config):
    """装配5个 Google News 关键词 RSS URL（when 参数即小时窗口）"""
    hours = config["window_hours"]
    return [GN_TEMPLATE.format(q=urllib.parse.quote(q), h=hours)
            for q in config["google_news_queries"]]


def fetch_feed(url):
    """抓取单个 RSS，失败返回空 feed（不中断整体）"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as exc:  # 网络失败/解析失败都容错
        print(f"[warn] 抓取失败 {url}: {exc}", file=sys.stderr)
        return None


def extract_items(feed, source):
    """从 feedparser 结果提取标准条目；时间解析失败则丢弃该条目"""
    out = []
    for entry in feed.get("entries", []):
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        if not ts:
            continue
        published = datetime(*ts[:6], tzinfo=timezone.utc)
        out.append({
            "title": (entry.get("title") or "").strip(),
            "url": (entry.get("link") or "").strip(),
            "source": source,
            "published_utc": published.isoformat(),
            "summary": re.sub(r"<[^>]+>", "", entry.get("summary") or "").strip()[:400],
        })
    return out


def filter_window(items, now_utc, hours):
    """仅保留窗口内的条目；时间无法解析的条目丢弃"""
    start = now_utc - timedelta(hours=hours)
    kept = []
    for it in items:
        try:
            ts = datetime.fromisoformat(it["published_utc"])
        except ValueError:
            continue
        if start <= ts <= now_utc:
            kept.append(it)
    return kept


def norm_title(title):
    """标题归一化：小写、去标点与空白"""
    return re.sub(r"[\W_]+", "", title.lower())


def dedupe(items):
    """同标题保留第一条（各 RSS 源混排时先到先得）"""
    seen, out = set(), []
    for it in items:
        key = norm_title(it["title"])
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def fetch_all_items(config, now_utc):
    """抓取全部信源并标准化"""
    items = []
    for url in build_google_urls(config):
        feed = fetch_feed(url)
        if feed is not None:
            items.extend(extract_items(feed, "Google News聚合"))
    for url in config["expert_feeds"]:
        feed = fetch_feed(url)
        if feed is not None:
            src = feed.get("feed", {}).get("title") or url.split("/")[2]
            items.extend(extract_items(feed, src))
    return items


def main():
    config = load_config()
    now = datetime.now(timezone.utc)
    raw = fetch_all_items(config, now)
    kept = dedupe(filter_window(raw, now, config["window_hours"]))
    kept.sort(key=lambda x: x["published_utc"], reverse=True)
    kept = kept[: config["max_items"]]
    OUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at_utc": now.isoformat(), "items": kept}, f, ensure_ascii=False, indent=1)
    print(f"抓取完成：原始{len(raw)}条 -> 窗口内去重后{len(kept)}条")
    if not kept:
        sys.exit(2)  # 退出码2 = 无条目（run_daily.sh 据此告警）


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_fetch -v
```

Expected: 3 个测试全部 PASS

- [ ] **Step 6: 真实验证抓取**

```bash
cd feishu-milnews && .venv/bin/python3 fetch_news.py; .venv/bin/python3 -c "
import json
d = json.load(open('logs/last_raw_items.json'))
print('条目数:', len(d['items']))
print('首条:', d['items'][0]['title'], '|', d['items'][0]['published_utc'])"
```

Expected: 输出条目数（应 > 0）与首条标题；`logs/last_raw_items.json` 已生成

---

### Task 3: generate_report.py 生成层

**Files:**
- Create: `feishu-milnews/generate_report.py`
- Create: `feishu-milnews/tests/test_generate.py`

**Interfaces:**
- Consumes: `logs/last_raw_items.json`（Task 2 产物）；`.env` 中 `DEEPSEEK_API_KEY`
- Produces（供 Task 4 使用）：
  - 函数 `build_prompt(items, now_utc) -> str`
  - 函数 `call_deepseek(prompt, api_key) -> str`（返回模型输出原文）
  - 函数 `parse_report(text) -> dict`（解析并校验 JSON）
  - 函数 `validate_report(obj) -> list[str]`（返回错误列表，空列表=合法）
  - 主程序入口 `main()`：写 `logs/last_report.json`，结构 `{"window":{"start_utc","end_utc"},"sections":[{"section","items":[{"title","source","url","summary","tag"}]}],"verdict","generated_at_utc"}`

- [ ] **Step 1: 写失败测试 tests/test_generate.py**

```python
"""generate_report.py 单元测试（mock DeepSeek API，不联网）"""
import json, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
import sys; sys.path.insert(0, str(BASE))
import generate_report as gen

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE_ITEMS = [
    {"title": "US strikes Iran", "url": "https://x.com/1", "source": "CNN", "published_utc": "2026-08-31T10:00:00+00:00", "summary": "美军打击伊朗设施"},
]

class TestValidate(unittest.TestCase):
    def test_good_report_passes(self):
        obj = {"window": {"start_utc": "a", "end_utc": "b"}, "verdict": "v",
               "sections": [{"section": "一、作战行动", "items": [
                   {"title": "t", "source": "s", "url": "u", "summary": "ss", "tag": "❗"}]}]}
        self.assertEqual(gen.validate_report(obj), [])

    def test_missing_sections_rejected(self):
        self.assertTrue(gen.validate_report({"verdict": "v"}))

    def test_item_missing_url_rejected(self):
        obj = {"sections": [{"section": "x", "items": [{"title": "t", "source": "s", "summary": "s", "tag": ""}]}], "verdict": "v"}
        self.assertTrue(gen.validate_report(obj))

class TestParseReport(unittest.TestCase):
    def test_parse_strips_markdown_fence(self):
        text = '```json\n{"sections": [], "verdict": "ok"}\n```'
        obj = gen.parse_report(text)
        self.assertEqual(obj["verdict"], "ok")

    def test_parse_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            gen.parse_report("不是JSON")

class TestCallDeepseek(unittest.TestCase):
    def test_extracts_content(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        with mock.patch.object(gen.requests, "post", return_value=resp) as mp:
            self.assertEqual(gen.call_deepseek("p", "key"), "{}")
            self.assertEqual(mp.call_args[1]["timeout"], 120)

    def test_402_raises_with_balance_hint(self):
        resp = mock.Mock()
        resp.status_code = 402
        resp.text = "Insufficient Balance"
        with mock.patch.object(gen.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as cm:
                gen.call_deepseek("p", "key")
            self.assertIn("余额", str(cm.exception))
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_generate -v
```

Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 generate_report.py**

```python
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
    secs = obj.get("sections")
    if not isinstance(secs, list):
        problems.append("缺少sections数组")
        return problems
    for sec in secs:
        if not isinstance(sec.get("section"), str):
            problems.append("某节缺少section名")
        for it in sec.get("items", []):
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_generate -v
```

Expected: 7 个测试全部 PASS

- [ ] **Step 5: 真实验证生成（需 Task 1 的 API Key 已配置且余额充足）**

```bash
cd feishu-milnews && .venv/bin/python3 generate_report.py && .venv/bin/python3 -c "
import json
d = json.load(open('logs/last_report.json'))
print('窗口:', d['window'])
print('节数:', len(d['sections']))
print('总条数:', sum(len(s['items']) for s in d['sections']))
print('研判:', d['verdict'][:60])"
```

Expected: 输出窗口、节数（5）、条数与研判开头；`logs/last_report.json` 已生成

---

### Task 4: push_feishu.py 推送层

**Files:**
- Create: `feishu-milnews/push_feishu.py`
- Create: `feishu-milnews/tests/test_push.py`

**Interfaces:**
- Consumes: `logs/last_report.json`（Task 3 产物）；`config.json` 的 `feishu_webhook`
- Produces:
  - 函数 `build_post_message(report, part, total) -> dict`（飞书 post 消息体，含标题行）
  - 函数 `split_parts(report, limit=18000) -> list[list[dict]]`（按条目边界拆分）
  - 函数 `send(webhook, message) -> bool`（单条发送+重试3次）
  - 函数 `send_alert(webhook, text) -> bool`（text 类型告警消息，供 run_daily.sh 失败路径调用）
  - 主程序入口 `main()`；命令行模式：`--alert "文本"` 走告警路径

- [ ] **Step 1: 写失败测试 tests/test_push.py**

```python
"""push_feishu.py 单元测试（mock 网络）"""
import json, unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(len(post["content"]), 3)  # 1节2条 => 2段 + 1段研判
        # 首段含加粗标题与超链接
        first = post["content"][0][0]
        self.assertEqual(first["tag"], "text")
        self.assertIn("新闻标题0", first["text"])
        self.assertIn("❗", first["text"])
        link = [b for b in post["content"][0] if b["tag"] == "a"]
        self.assertEqual(link[0]["href"], "https://x.com/0")

class TestSplitParts(unittest.TestCase):
    def test_small_report_single_part(self):
        report = make_report(3)
        parts = push.split_parts(report, limit=18000)
        self.assertEqual(len(parts), 1)

    def test_large_report_splits(self):
        report = make_report(50)
        parts = push.split_parts(report, limit=2500)  # 人为压低阈值强制拆分
        self.assertGreater(len(parts), 1)

class TestSend(unittest.TestCase):
    def test_success_first_try(self):
        resp = mock.Mock(); resp.json.return_value = {"code": 0}
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            self.assertTrue(push.send("http://hook", {"msg_type": "text", "content": {}}))
        self.assertEqual(mp.call_count, 1)

    def test_retries_then_fails(self):
        resp = mock.Mock(); resp.json.side_effect = ValueError("bad json")
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            with mock.patch("time.sleep"):
                self.assertFalse(push.send("http://hook", {}))
        self.assertEqual(mp.call_count, 3)

    def test_webhook_http_error_no_retry_on_4xx(self):
        resp = mock.Mock(); resp.status_code = 400
        with mock.patch.object(push.requests, "post", return_value=resp) as mp:
            with mock.patch("time.sleep"):
                self.assertFalse(push.send("http://hook", {}))
        self.assertEqual(mp.call_count, 1)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_push -v
```

Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 push_feishu.py**

```python
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


def _item_paragraph(it):
    """单条新闻 -> post 段落；仅接受 title/source/url/summary/tag 字段"""
    text = f"▎{it['title']}（{it['source']}）"
    if it.get("tag"):
        text += f" {it['tag']}"
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


def split_parts(report, limit=LIMIT):
    """超限时按条目边界拆成多个 part（每个 part 为 sections 子集+研判）"""
    full = build_post_message(report, 1, 1)
    if len(json.dumps(full, ensure_ascii=False)) <= limit:
        return [full]
    parts, current = [], []
    for sec in report.get("sections", []):
        block = [[{"tag": "text", "text": f"【{sec['section']}】"}]]
        for it in sec.get("items", []):
            block.append(_item_paragraph(it))
        trial = current + block
        trial_msg = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": "t", "content": trial}}}}
        if len(json.dumps(trial_msg, ensure_ascii=False)) > limit and current:
            parts.append(current)
            current = block
        else:
            current = trial
    if report.get("verdict"):
        current.append([{"tag": "text", "text": f"〔综合研判〕{report['verdict']}"}])
    parts.append(current)
    total = len(parts)
    return [{"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": _window_title(report, i + 1, total), "content": p}}}}
        for i, p in enumerate(parts)]


def send(webhook, message):
    """单条发送；网络错误/响应异常重试3次；HTTP 4xx 不重试"""
    for attempt in range(3):
        try:
            resp = requests.post(webhook, json=message, timeout=20)
            if resp.status_code != 200:
                print(f"[warn] webhook HTTP {resp.status_code}", file=sys.stderr)
                if 400 <= resp.status_code < 500:
                    return False  # 参数/密钥类错误重试无意义
            else:
                try:
                    if resp.json().get("code") in (0, 19001):
                        return True
                except ValueError:
                    pass
        except Exception as exc:
            print(f"[warn] 第{attempt+1}次发送失败: {exc}", file=sys.stderr)
        time.sleep(2 ** attempt)
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd feishu-milnews && .venv/bin/python3 -m unittest tests.test_push -v
```

Expected: 7 个测试全部 PASS

- [ ] **Step 5: 真实验证推送（需 webhook 已配置）**

```bash
cd feishu-milnews && .venv/bin/python3 push_feishu.py
```

Expected: 飞书群收到简报消息（含标题行、分节、超链接）；脚本输出"推送完成：N条消息"

---

### Task 5: run_daily.sh 编排入口

**Files:**
- Create: `feishu-milnews/run_daily.sh`
- Create: `feishu-milnews/tests/test_run_daily.sh`

**Interfaces:**
- Consumes: 三个 Python 脚本与 logs/ 目录
- Produces: 每日执行日志 `logs/run-YYYYMMDD-HHMM.log`；退出码 0=成功；非0=失败（launchd 仅观察退出码与日志）

- [ ] **Step 1: 写失败测试 tests/test_run_daily.sh**

```bash
#!/bin/bash
# 测试：抓取层失败时应发送告警并退出非0
set -u
cd "$(dirname "$0")/.." || exit 99
# 用不存在的 config 触发 fetch 失败
mkdir -p /tmp/milnews-test && cp config.json /tmp/milnews-test/config.bak
mv config.json config.json.bak
set +e
bash run_daily.sh >/tmp/milnews-test/out.log 2>&1
rc=$?
set -e
mv config.json.bak config.json
echo "退出码: $rc"
if [ "$rc" -eq 0 ]; then echo "FAIL: 应非0"; exit 1; fi
echo "PASS: 失败路径正确退出非0"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd feishu-milnews && bash tests/test_run_daily.sh
```

Expected: FAIL（`run_daily.sh` 不存在）

- [ ] **Step 3: 实现 run_daily.sh**

```bash
#!/bin/bash
# 编排入口：抓取 -> 生成 -> 推送；整体超时10分钟；每层失败即告警退出
set -u
cd "$(dirname "$0")" || exit 1
TS=$(date +%Y%m%d-%H%M)
mkdir -p logs
exec > >(tee -a "logs/run-$TS.log") 2>&1   # 日志同时落盘
export PY=".venv/bin/python3"
export WEBHOOK=$(PY=$PY python3 - <<'EOF'
import json
try:
    print(json.load(open('config.json'))['feishu_webhook'])
except Exception:
    print('')
EOF
)

alert() { $PY push_feishu.py --alert "$1" || true; }

# 整体超时：10分钟后未完成则自杀（由 launchd 下次触发兜底）
( sleep 600; kill $$ ) 2>/dev/null & WATCHDOG=$!
trap 'kill $WATCHDOG 2>/dev/null' EXIT

echo "===== 开始运行 $TS ====="

$PY fetch_news.py
RC=$?
if [ $RC -eq 2 ]; then
  echo "无窗口内新条目，跳过生成"; alert "本次窗口内未抓到新条目"; exit 0
elif [ $RC -ne 0 ]; then
  echo "抓取失败"; alert "新闻抓取失败，请检查网络"; exit 2
fi

$PY generate_report.py || { alert "DeepSeek 生成失败（可能余额不足或网络异常）"; exit 3; }
$PY push_feishu.py      || { alert "飞书推送失败，请检查 webhook 与日志"; exit 4; }

echo "===== 运行完成 ====="
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd feishu-milnews && bash tests/test_run_daily.sh
```

Expected: `退出码: 非0` 与 `PASS: 失败路径正确退出非0`

- [ ] **Step 5: 手动全流程验证**

```bash
cd feishu-milnews && bash run_daily.sh && tail -20 logs/run-*.log | tail -5
```

Expected: 三层顺序执行成功；飞书群收到简报；日志出现"===== 运行完成 ====="

---

### Task 6: launchd 定时安装

**Files:**
- Modify: `feishu-milnews/launchd/com.a1-6.milnews.plist`（Task 1 已创建空文件）

**Interfaces:**
- Consumes: `run_daily.sh` 绝对路径 `/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews/run_daily.sh`
- Produces: 已加载的 LaunchAgent（开机自启、每日 07:30 / 12:00 触发）

- [ ] **Step 1: 写入 plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.a1-6.milnews</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews/run_daily.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
        <dict>
            <key>Hour</key><integer>12</integer>
            <key>Minute</key><integer>0</integer>
        </dict>
    </array>
    <key>StandardOutPath</key>
    <string>/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/a1-6/Documents/ClaudeCode/2026-08-31/feishu-milnews/logs/launchd.err.log</string>
</dict>
</plist>
```

- [ ] **Step 2: 安装到 LaunchAgents 并加载**

```bash
cp feishu-milnews/launchd/com.a1-6.milnews.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.a1-6.milnews 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.a1-6.milnews.plist
launchctl print gui/$(id -u)/com.a1-6.milnews | head -15
```

Expected: `launchctl print` 显示任务已加载（state = waiting）

- [ ] **Step 3: kickstart 触发验证**

```bash
launchctl kickstart gui/$(id -u)/com.a1-6.milnews && sleep 3 && ls -lt feishu-milnews/logs/ | head -5
```

Expected: 出现新的 `run-YYYYMMDD-HHMM.log`（说明调度确实能拉起脚本）

---

### Task 7: 端到端验收

**Files:** 无新增（必要时修改 config.json 中失效的 expert_feeds）

- [ ] **Step 1: 检查专家信源清单有效性**

```bash
cd feishu-milnews && .venv/bin/python3 - <<'EOF'
import json, requests
cfg = json.load(open('config.json'))
for u in cfg['expert_feeds']:
    try:
        r = requests.get(u, timeout=15, headers={'User-Agent':'Mozilla/5.0'})
        print(('OK ' if r.status_code==200 else f'{r.status_code} '), u)
    except Exception as e:
        print('ERR', u, type(e).__name__)
EOF
```

Expected: 各源输出 `OK` 或明确状态码；**将任何非200源从 config.json 的 expert_feeds 中删除**（记录于日志）

- [ ] **Step 2: 核对飞书消息内容质量**

检查最近一次推送：①标题行时间窗口与美东时间一致；②每条含来源与"原文"链接；③无编造条目（抽2条与 logs/last_raw_items.json 对照）；④价值标注正确出现

- [ ] **Step 3: 验证错误告警路径**

```bash
cd feishu-milnews && .venv/bin/python3 push_feishu.py --alert "测试告警：这是一条系统自检消息"
```

Expected: 飞书群收到一条 text 告警"【美军新闻简报系统】测试告警：..."

- [ ] **Step 4: 观察两个定时点自动运行**

在下一个 07:30 与 12:00 后检查：①飞书群按时收到推送；②logs/ 出现对应 run 日志；③日志无异常报错。连续观察 2 天（4 次推送）确认稳定，随后向用户汇报验收结论。

---

## 计划自审记录

- **Spec 覆盖**：需求结论5项（webhook→Task1/4、双时刻→Task6、DeepSeek→Task3、本机开机→Task6、富文本拆分→Task4）；设计文档5.1–5.5 各层均有对应任务；错误处理表（抓取空→Task5退出码2、402→Task3/5、JSON非法→Task3重试、推送失败→Task4重试、超时→Task5看门狗）；测试验收7项→Task2/3/4/5各单测+Task7验收。
- **占位符**：config.json 与 .env 的 PASTE 占位符为设计内用户待填项（Task 1 Step 5 明确补齐时机），非计划占位。
- **类型一致性**：`raw_items` 条目字段（title/url/source/published_utc/summary）在 Task 2 产出、Task 3 消费一致；`report.json` 的 sections/items 字段（title/source/url/summary/tag）在 Task 3 产出、Task 4 消费一致；退出码约定（0成功/2无条目/3生成失败/4推送配置/5推送失败）在 Task 2/3/4/5 间一致。

# 飞书定时推送美军新闻简报 — 设计文档

日期：2026-08-31
状态：已与用户逐节确认（方案A / 各层细节均获批准）

## 1. 背景与目标

用户此前人工完成"搜集近5小时美军相关新闻（美军动态、国防政策、涉美军舆论、前沿技术、智库分析），注明来源与战略价值，输出中文简报与来源清单"的任务。现希望将该任务自动化：

- 每天定时（北京时间 07:30 与 12:00）自动生成近5小时新闻简报
- 推送到飞书群（自定义机器人 Webhook，富文本消息）
- 内容标准与人工版一致：对军队有战略价值、借鉴参考意义、警示警惕类、对中国有直接影响

## 2. 需求结论（头脑风暴澄清结果）

| 决策点 | 结论 |
|---|---|
| 飞书接入方式 | 群自定义机器人 Webhook |
| 推送时刻 | 每天北京时间 07:30 与 12:00（各覆盖前5小时窗口，换算美东：前日 14:30–19:30 与前日晚间 19:00–24:00） |
| 内容生成 | 调用 DeepSeek API（openai 兼容接口，model: deepseek-chat） |
| 运行环境 | 本机 Mac（保持开机），launchd 调度 |
| 消息格式 | 飞书 post 富文本卡片，超长自动拆分多条 |

## 3. 总体架构（方案A）

三层独立脚本 + 薄编排，均由 launchd 触发：

```
launchd（07:30 / 12:00 触发）
   └─> run_daily.sh
        ├─ 1. python3 fetch_news.py      # 抓取层：RSS → raw_items.json
        ├─ 2. python3 generate_report.py # 生成层：DeepSeek API → report.json
        └─ 3. python3 push_feishu.py     # 推送层：report.json → 飞书富文本消息
```

核心决策：
1. 新闻获取用 RSS（Google News RSS `when:5h` + 专业信源 RSS），不让 DeepSeek 承担联网搜索能力（其基础 API 可能无联网工具）。
2. DeepSeek 只做"汇总分析"：原始条目进、结构化 JSON 出。
3. 每层可独立运行与测试；任一层失败不影响其他层留档与调试。

## 4. 目录结构

```
feishu-milnews/
├── config.json            # webhook、关键词组、信源列表、条数上限等
├── fetch_news.py          # 抓取层
├── generate_report.py     # 生成层
├── push_feishu.py         # 推送层
├── run_daily.sh           # 编排入口：顺序执行 + 日志 + 失败告警
├── launchd/
│   └── com.a1-6.milnews.plist
├── logs/                  # 运行日志 + 最近一次产物留档
├── .env                   # DEEPSEEK_API_KEY（chmod 600）
└── requirements.txt       # requests、feedparser
```

## 5. 各层细节

### 5.1 抓取层（fetch_news.py）

Google News RSS，5 个关键词组，URL 模板：
`https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en&when:5h`

| 组 | 查询词 | 覆盖域 |
|---|---|---|
| A | `"US military" OR Pentagon OR "US Army" OR "US Navy"` | 美军动态/政策 |
| B | `China military OR Taiwan OR "South China Sea"` | 对华直接影响 |
| C | `"defense technology" OR "military technology" OR drone OR hypersonic` | 前沿技术 |
| D | `think tank OR CSIS OR RAND China military` | 智库分析 |
| E | `"US military" controversy OR criticism OR readiness` | 舆论/警示 |

专业信源 RSS 固定清单（按 pubDate 过滤近5小时，兜底防漏）：
Defense News、Breaking Defense、Military Times、The War Zone、USNI News、War on the Rocks、defense.gov 官方新闻稿、CSIS、RAND。实施时逐一验证 RSS 端点，失效的剔除（在 config.json 中维护生效清单）。

处理规则：合并 → 标题归一化去重（同事件多转载保留最权威源）→ 时间过滤 → 每条提取（标题/链接/来源/时间/摘要）→ 时间倒序 → 上限60条 → 写入 raw_items.json。

### 5.2 生成层（generate_report.py）

- DeepSeek API：`https://api.deepseek.com`，openai 兼容调用，model `deepseek-chat`，`response_format: {"type":"json_object"}`，temperature 0.3
- 系统提示词核心要求：
  - 按五类组织：一、作战行动与部署；二、前沿军事技术；三、国防与AI政策；四、涉美军舆论与警示；五、智库分析
  - 筛选标准：战略价值 / 借鉴参考 / 警示警惕 / 对华直接影响
  - 每条含：标题（保留英文原名）、来源媒体名、要点（≤2句）、价值标注（❗对华直接影响/⚠️警示/🔬技术借鉴/无）
  - 结尾"综合研判"≤100字
  - 无内容的类别输出空数组；不得编造条目；事实只能来自输入数据
- 输出 JSON schema：

```json
{
  "window": {"start_utc": "...", "end_utc": "..."},
  "sections": [
    {"section": "一、作战行动与部署", "items": [
      {"title": "...", "source": "...", "url": "...", "summary": "...", "tag": "❗"}
    ]}
  ],
  "verdict": "..."
}
```

### 5.3 推送层（push_feishu.py）

- 飞书 post 富文本：`msg_type: post`；标题行"美军新闻简报｜美东时间 X月X日 XX:XX–XX:XX"；每条新闻一段：加粗标题 + 来源 + 要点 + 价值标注 + 超链接
- 拆分：消息体超约18KB 按条目边界拆多条，每条间隔2秒，各带"第N/N条"标识
- 容错：单条失败重试3次（指数退避）；全部失败发送 text 告警"今日简报生成失败：<原因>"；webhook 本身失效仅记日志
- 可选：飞书机器人加签校验（config 可选 secret，默认关闭）

### 5.4 调度层（launchd）

- plist：`~/Library/LaunchAgents/com.a1-6.milnews.plist`
- `StartCalendarInterval` 两个时刻：`{Hour:7, Minute:30}` 与 `{Hour:12, Minute:0}`（本机时区，即北京时间）
- `StandardOutPath`/`StandardErrorPath` → logs/
- 加载：`launchctl bootstrap gui/$(id -u) <plist>`；手动测试：`launchctl kickstart gui/$(id -u)/com.a1-6.milnews`

### 5.5 日志与密钥

- 日志：`logs/run-YYYYMMDD-HHMM.log`；留档：`logs/last_raw_items.json`、`logs/last_report.json`
- 密钥：`.env`（DEEPSEEK_API_KEY）与 config.json（webhook）均 chmod 600

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| RSS 抓取全失败/条目为空 | 记日志，跳过生成，向飞书发 text 告警"抓取失败/无新条目" |
| DeepSeek API 报错（含余额不足402） | 记日志，向飞书发告警；产物留档便于人工查看 |
| 生成的 JSON 非法 | 重试1次；仍失败则告警并留原始返回 |
| 飞书推送失败 | 重试3次；仍失败则告警（webhook 失效时仅日志） |
| 单次运行超时 | run_daily.sh 设置整体超时（如10分钟），超时杀进程并记日志 |

## 7. 测试与验收（实施时逐项执行）

1. fetch_news.py 单跑：条目数量合理、时间均在5小时窗口内
2. generate_report.py 单跑：JSON 合法、无编造条目（抽查条目与输入对照）
3. push_feishu.py 用样例报告跑：飞书群收到测试消息，富文本排版与链接正常
4. run_daily.sh 全流程手动跑一次
5. launchctl kickstart 触发验证调度
6. 观察下一个定时点自动推送；连续观察至少2天（4次推送）确认稳定

## 8. 范围外（YAGNI）

- 不做飞书互动卡片（按钮/折叠）
- 不做多群推送、不做个人推送
- 不做历史回溯查询、不做网页版
- 不做新闻全文抓取（仅用 RSS 摘要）
- 不做云上部署（GitHub Actions 等）

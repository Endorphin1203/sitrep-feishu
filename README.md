# SITREP-Feishu

美军相关新闻态势简报系统：定时 + 群内指令双机制，自动汇集近 5 小时（美东时间窗口）军事新闻并推送到飞书群。

SITREP = Situation Report（态势报告）。

## 功能

- **每日定时推送**：北京时间每天 07:30 与 12:00 各推一次（launchd 调度，分别覆盖美东前日 14:30–19:30 与前日晚间 19:00–24:00）
- **群内手动触发**：飞书群发送 `@机器人 /news`，即时汇集并推送
- **内容分类**：作战行动与部署 / 前沿军事技术 / 国防与AI政策 / 涉美军舆论与警示 / 智库分析
- **价值标注**：❗对华直接影响 / ⚠️警示 / 🔬技术借鉴
- **来源**：Google News 关键词 RSS（when:5h 窗口）+ 8 个专业信源（Defense News、USNI、CSIS 等），每条含原文链接
- **可靠性**：三层独立脚本 + flock 互斥锁 + 失败飞书告警 + listener 健康检查兜底

## 架构

```
launchd 定时（07:30 / 12:00）────────────┐
                                          ├─> run_daily.sh（flock 互斥 + 看门狗）
飞书群 @机器人 /news ─> listener.py（WS长连接）─┘      │
                                        ├─ 1. fetch_news.py      抓取层：RSS → raw_items.json
                                        ├─ 2. generate_report.py 生成层：DeepSeek API → report.json
                                        └─ 3. push_feishu.py     推送层：report → 飞书 post 富文本
```

- 简报统一由群自定义机器人（webhook）推送；自建应用机器人负责监听 `/news` 指令并回复确认
- 定时与手动触发经同一 run_daily.sh 入口，flock 非阻塞锁互斥

## 目录结构

```
sitrep-feishu/
├── fetch_news.py          # 抓取层：Google News RSS(when:5h) + 专家信源，去重/过滤/排序
├── generate_report.py     # 生成层：DeepSeek API（deepseek-chat, json_object）→ 结构化中文简报
├── push_feishu.py         # 推送层：飞书 post 富文本（超长拆分、重试、--alert 告警）
├── listener.py            # 飞书事件监听（lark-oapi WS 长连接）：过滤 /news 指令并触发
├── run_daily.sh           # 编排入口：锁 → 健康检查 → 三层顺序执行 → 日志落盘
├── launchd/
│   ├── com.a1-6.milnews.plist            # 定时任务（07:30/12:00）
│   └── com.a1-6.milnews-listener.plist   # 监听进程（KeepAlive+RunAtLoad）
├── tests/                 # unittest 测试（46 个，mock 网络，不联网）
├── config.example.json    # 配置模板（复制为 config.json 并填入 webhook）
└── docs/                  # 设计文档与实施计划
```

## 部署

### 1. 环境

- macOS（launchd 调度）；Python 3.9+（venv）
- 飞书群自定义机器人 webhook（推送简报用）
- 飞书开放平台企业自建应用（监听 `/news` 指令用，长连接模式）
- DeepSeek API Key

### 2. 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 配置

```bash
cp config.example.json config.json
# 编辑 config.json 填入 feishu_webhook
printf 'DEEPSEEK_API_KEY=sk-xxx\n' > .env
# 创建 listener_config.json（自建应用凭证 + 白名单群）
chmod 600 config.json .env listener_config.json
```

`listener_config.json` 结构：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx",
  "allowed_chat_ids": ["oc_xxx"],
  "dedup_seconds": 300
}
```

### 4. 飞书平台配置（listener 用）

1. 飞书开放平台创建企业自建应用，开启"机器人"能力
2. 权限：`im:message`、`im:message.group_at_msg`、`im:message:send_as_bot`、`im:chat:readonly`（获取群列表用）
3. 事件订阅：**长连接模式**，添加事件 `接收消息 im.message.receive_v1`
4. 创建版本并发布（企业版需管理员审批）
5. 把机器人加进目标群；群 ID 可用 `im/v1/chats` API 获取或从事件日志提取

### 5. launchd 安装

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.a1-6.milnews.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.a1-6.milnews-listener.plist
```

## 测试

```bash
.venv/bin/python3 -m unittest discover -s tests
bash tests/test_run_daily.sh   # 编排失败路径
bash tests/test_lock.sh        # 互斥锁
```

## 已知限制与注意

- **macOS 无 flock(1) 命令**：互斥锁用 python3 fcntl 实现（run_daily.sh 锁段）
- **launchd 读取 ~/Documents 受 TCC 限制**：项目真实目录须在 Documents 之外（如 ~/feishu-milnews）
- **macOS 26.x launchd 节流**：RunAtLoad/KeepAlive 可能被系统挂起（pended nondemand spawn）；run_daily.sh 内置 listener 健康检查兜底（未运行则 kickstart），最坏停机不超过两个定时点间隔
- **DeepSeek 输出**：tag 可能输出简写（⚠️），推送层已做归一化映射
- 密钥文件严禁提交；`.gitignore` 已排除 config.json / listener_config.json / .env / logs

## 维护

- 日志：`logs/run-*.log`（每日流程）、`logs/listener.log`（事件与触发）、`logs/listener-launchd.out/err.log`（监听进程）
- 手动触发完整流程：`bash run_daily.sh`
- 发告警测试：`.venv/bin/python3 push_feishu.py --alert "测试"`

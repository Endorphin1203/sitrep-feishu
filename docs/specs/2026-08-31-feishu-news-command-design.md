# 飞书群 /news 触发即时新闻简报 — 设计文档

日期：2026-08-31
状态：设计已与用户逐节确认；用户已完成飞书平台侧配置并提供应用凭证

## 1. 背景与目标

已交付系统：每天 07:30 / 12:00 定时推送美军相关近5小时新闻简报到飞书群（webhook 自定义机器人）。现新增第二种触发机制：**群成员在飞书群发送 @机器人 /news，触发一次即时的新闻汇集与推送**，与定时机制并存互不影响。

## 2. 需求结论（澄清结果）

| 决策点 | 结论 |
|---|---|
| 接入方式 | 飞书开放平台企业自建应用 + **长连接（WebSocket）模式**事件订阅（无需公网 IP） |
| 触发方式 | 群成员 @机器人 发送 `/news`（标准权限 group_at_msg；不支持无@触发） |
| 简报呈现 | 由现有 webhook 机器人推送到群（样式与定时推送完全一致），应用机器人只发简短确认消息 |
| 响应范围 | 仅白名单群（chat_id 列表），私聊与其他群一律忽略 |
| 并发控制 | flock 非阻塞锁放在 run_daily.sh 内，定时/手动共用同一入口互斥 |
| 防抖 | 同一用户 5 分钟内重复 /news 仅响应第一次 |

## 3. 总体架构（方案A）

```
群成员 "@机器人 /news"
  → 飞书服务器 WS 推送事件
  → 本机 listener.py（lark-oapi SDK 长连接，自动 ack/重连）
  → 过滤：im.message.receive_v1 + text 消息 + 剥@后等于 /news + 白名单群 + 防抖
  → 应用身份回复"收到，正在汇集近5小时新闻…"
  → spawn: bash run_daily.sh（flock 非阻塞；锁占用则回"已有任务运行中"）
  → 简报经现有 webhook 机器人推送到群（现有链路，含失败告警）
```

三条核心决策：
1. **简报仍由 webhook 机器人发出**——/news 只是扳机，应用机器人只回确认，简报来源单一、样式一致。
2. **锁在 run_daily.sh 内（flock -n）**——定时与手动共用同一入口，一处互斥。
3. **listener 只做三件事**：鉴权过滤、触发脚本、回确认消息。

## 4. 组件清单

```
feishu-milnews/（真实位置 ~/feishu-milnews）
├── listener.py              # 新增：WS 事件监听进程（常驻）
├── listener_config.json     # 新增：app_id/app_secret/allowed_chat_ids/防抖时长（chmod 600）
├── launchd/com.a1-6.milnews-listener.plist  # 新增：KeepAlive+RunAtLoad 常驻托管
├── requirements.txt         # 修改：增加 lark-oapi
├── run_daily.sh             # 修改：开头加 flock 非阻塞锁
└── tests/test_listener.py   # 新增：过滤逻辑单元测试
```

## 5. 各组件细节

### 5.1 listener.py

- 用 lark-oapi SDK 的 `lark.ws.Client`（长连接客户端）启动事件循环，SDK 内置心跳与断线重连
- 事件处理（`im.message.receive_v1`）：
  - 过滤链：事件类型 → message_type=="text" → 剥 @_user_N 提及 → strip == "/news" → chat_id ∈ 白名单 → 同一 (chat_id, open_id) 5 分钟防抖
  - 通过后：先用 **flock 探针预检** `/tmp/milnews.lock`（`LOCK_EX|LOCK_NB` 尝试即释放）——锁被占用则直接回"已有任务运行中，请稍后再试"并结束；锁空闲则调用 `im.message.create`（send_as_bot）回确认消息"收到，正在汇集近5小时新闻…"，再 `subprocess.Popen(["bash", run_daily.sh])` 分离执行
  - 任何异常记日志并忽略（不阻塞事件循环）；日志 `logs/listener.log` 一行/事件
- 事件回调必须快速返回（SDK 自动 ack），业务全在子进程
- 探针与 run_daily 内部 flock 之间存在极小 race 窗口（毫秒级），最坏结果是本次无简报（可重发 /news），可接受

### 5.2 listener_config.json 结构

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx",
  "allowed_chat_ids": ["oc_xxx"],
  "dedup_seconds": 300
}
```
权限 600。chat_id 在联调时从收到的事件中确认后填入。

### 5.3 run_daily.sh 锁改造（最小改动）

在 `set -u` 之后加：

```bash
LOCK="/tmp/milnews.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "已有任务运行中，本次触发退出"
  exit 0
fi
```

### 5.4 launchd 监听任务

`com.a1-6.milnews-listener`：ProgramArguments=`/Users/a1-6/feishu-milnews/.venv/bin/python3` + `listener.py`；WorkingDirectory=项目目录；`KeepAlive=true`；`RunAtLoad=true`；日志 `logs/listener-launchd.out/err.log`。

### 5.5 密钥与安全

- app_secret 只存 listener_config.json（600）；日志不得打印
- 白名单群外消息、私聊、非 /news 文本一律静默忽略
- 凭证：用户已提供（App ID / App Secret 由控制器在下发时传入，不写入本 spec）

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| WS 断线 | SDK 自动重连（指数退避） |
| /news 时已有任务运行 | listener 锁探针预检发现占用 → 应用回复"已有任务运行中，请稍后再试" |
| run_daily.sh 失败 | 现有 webhook 告警链路兜底；listener 只记录日志 |
| 确认消息发送失败 | 记日志忽略（简报推送不依赖它） |
| listener 进程崩溃 | launchd KeepAlive 自动重启 |
| 非白名单/非文本/非 /news | 静默忽略 |

## 7. 测试与验收

1. 单元测试：过滤链（事件类型/文本/剥@精确匹配/白名单/防抖）mock 验证
2. 手动集成：群里 @机器人 /news → 秒回确认 → 1-2 分钟内简报到达（webhook 发出）→ 样式与定时一致
3. 并发：定时运行期间发 /news → 回"已有任务运行中"，无双推
4. 稳定性：listener 连续 24h 无崩溃；断网恢复自动重连
5. 与定时机制并存验证：定时任务不受 listener 影响（共用 run_daily.sh 与 flock）

## 8. 范围外（YAGNI）

- 不支持无 @ 触发（需高级权限 group_msg，暂不申请）
- 不支持其他指令（如 /news 参数、停止指令）
- 不做卡片消息交互
- 不做多机器人/多渠道

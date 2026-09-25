#!/bin/bash
# 编排入口：抓取 -> 生成 -> 推送；整体超时10分钟；每层失败即告警退出
set -u
# 互斥锁：定时与 /news 手动触发共用；锁占用则本次静默退出
# 注：macOS 无 flock(1) 命令，改用 python3 fcntl（与 listener.py lock_busy 同一 flock 域）
LOCK=/tmp/milnews.lock
exec 9>"$LOCK"
python3 -c 'import fcntl,sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' || { echo "已有任务运行中，本次触发退出"; exit 0; }
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

# listener 健康检查兜底：OS 节流下 KeepAlive 不可靠，未运行则拉起
if ! launchctl print gui/$(id -u)/com.a1-6.milnews-listener 2>/dev/null | grep -q "state = running"; then
  launchctl kickstart gui/$(id -u)/com.a1-6.milnews-listener 2>/dev/null || true
  echo "[watchdog] listener 未运行，已尝试拉起"
fi

echo "===== 开始运行 $TS ====="

$PY fetch_news.py
RC=$?
if [ $RC -eq 2 ]; then
  # 网络自愈兜底：开机后代理未就绪等场景下 SSL/DNS 失败导致0条，
  # 等待90秒后重试一轮（看门狗10分钟内仍有余量）
  echo "无窗口内新条目（疑似网络未就绪），等待90秒后重试…"
  sleep 90
  $PY fetch_news.py
  RC=$?
  if [ $RC -eq 2 ]; then
    echo "重试后仍无新条目，跳过生成"; alert "本次窗口内未抓到新条目"; exit 0
  elif [ $RC -ne 0 ]; then
    echo "重试后抓取失败"; alert "新闻抓取失败，请检查网络"; exit 2
  fi
elif [ $RC -ne 0 ]; then
  echo "抓取失败"; alert "新闻抓取失败，请检查网络"; exit 2
fi

$PY generate_report.py || {
  # 网络自愈兜底：开机后网络长时间未就绪/长请求断流时等待90秒重试一轮
  echo "生成失败，等待90秒后重试…"
  sleep 90
  $PY generate_report.py || { alert "DeepSeek 生成失败（可能余额不足或网络异常）"; exit 3; }
}
$PY push_feishu.py      || {
  echo "推送失败，等待90秒后重试…"
  sleep 90
  $PY push_feishu.py    || { alert "飞书推送失败，请检查 webhook 与日志"; exit 4; }
}

echo "===== 运行完成 ====="

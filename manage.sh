#!/bin/bash
# SITREP-Feishu 运维管理脚本
# 用法: manage.sh {status|start|stop|disable|enable|push|restart-listener}
#   status            查看两个任务状态与最近运行日志
#   start             启动定时任务与监听进程（已加载则跳过）
#   stop              临时停止（本次登录内有效；重启Mac后自动恢复）
#   disable           永久停用（stop + 移走 plist，重启后不再自动运行）
#   enable            恢复启用（移回 plist + start）
#   push              手动触发一次新闻简报
#   restart-listener  重启飞书事件监听进程
set -u

LISTENER="com.a1-6.milnews-listener"
SCHEDULER="com.a1-6.milnews"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DISABLED_DIR="$HOME/launchagents-disabled"
DIR="$(cd "$(dirname "$0")" && pwd)"

gui_state() {
    local label="$1"
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        local state
        state=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null | awk '/state =/{sub(/.*state = /, ""); print; exit}')
        echo "$label: 已加载（${state}）"
    else
        echo "$label: 未加载"
    fi
}

cmd_status() {
    echo "===== SITREP-Feishu 状态 ====="
    gui_state "$LISTENER"
    gui_state "$SCHEDULER"
    echo "--- 最近一次运行 ---"
    local latest
    latest=$(ls -t "$DIR"/logs/run-*.log 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
        basename "$latest"
        tail -2 "$latest"
    else
        echo "（暂无运行日志）"
    fi
}

cmd_start() {
    for label in "$SCHEDULER" "$LISTENER"; do
        if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
            echo "$label 已加载，跳过"
        elif [ -f "$AGENTS_DIR/$label.plist" ]; then
            launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$label.plist" && echo "$label 启动成功"
        else
            echo "$label 启动失败：$AGENTS_DIR/$label.plist 不存在"
        fi
    done
}

cmd_stop() {
    for label in "$LISTENER" "$SCHEDULER"; do
        if launchctl bootout "gui/$(id -u)/$label" 2>/dev/null; then
            echo "$label 已停止"
        else
            echo "$label 未在运行"
        fi
    done
}

cmd_disable() {
    cmd_stop
    mkdir -p "$DISABLED_DIR"
    for label in "$LISTENER" "$SCHEDULER"; do
        if [ -f "$AGENTS_DIR/$label.plist" ]; then
            mv "$AGENTS_DIR/$label.plist" "$DISABLED_DIR/" && echo "$label 已永久停用（plist 移至 ${DISABLED_DIR}）"
        fi
    done
    echo "提示：重启Mac后也不会自动运行；恢复请用 manage.sh enable"
}

cmd_enable() {
    mkdir -p "$AGENTS_DIR"
    for label in "$SCHEDULER" "$LISTENER"; do
        if [ -f "$DISABLED_DIR/$label.plist" ] && [ ! -f "$AGENTS_DIR/$label.plist" ]; then
            mv "$DISABLED_DIR/$label.plist" "$AGENTS_DIR/" && echo "$label 已恢复（plist 移回 LaunchAgents）"
        fi
    done
    cmd_start
}

cmd_push() {
    echo "手动触发一次简报（约2-4分钟）..."
    bash "$DIR/run_daily.sh"
}

cmd_restart_listener() {
    if launchctl kickstart -k "gui/$(id -u)/$LISTENER" 2>/dev/null; then
        echo "listener 已重启"
    else
        launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$LISTENER.plist" && echo "listener 已启动"
    fi
}

case "${1:-}" in
    status) cmd_status ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    disable) cmd_disable ;;
    enable) cmd_enable ;;
    push) cmd_push ;;
    restart-listener) cmd_restart_listener ;;
    *) echo "用法: manage.sh {status|start|stop|disable|enable|push|restart-listener}"; exit 1 ;;
esac

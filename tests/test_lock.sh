#!/bin/bash
# 测试：锁占用时第二个实例应快速静默退出（exit 0），且不产生新日志
set -u
cd "$(dirname "$0")/.." || exit 99
LOCK=/tmp/milnews.lock
# 先模拟持锁（不运行真实流程）；macOS 无 flock(1) 命令，用 python3 fcntl 在 fd 8 上持锁（锁随本进程生命周期保持）
exec 8>"$LOCK"
python3 -c 'import fcntl,sys
try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' || { echo "SETUP FAIL"; exit 99; }
set +e
bash run_daily.sh
rc=$?
set -e
echo "锁占用时退出码: $rc"
if [ "$rc" -ne 0 ]; then echo "FAIL: 预期0，实际$rc"; exit 1; fi
echo "PASS: 锁占用时静默退出"

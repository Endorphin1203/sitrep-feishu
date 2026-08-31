#!/bin/bash
# 测试：抓取层失败时应发送告警并退出非0
set -u
cd "$(dirname "$0")/.." || exit 99
# 前置检查：入口脚本必须存在且有执行权限，避免把“脚本缺失/语法错误”(bash 返回127)误判为抓取失败
[ -x run_daily.sh ] || { echo "FAIL: run_daily.sh 不存在或无执行权限"; exit 2; }
# 中断（如 Ctrl-C）时恢复 config.json，避免遗留 config.json.bak；|| true 防止 set -e 下恢复失败污染退出码
trap 'mv config.json.bak config.json 2>/dev/null || true' EXIT
# 用不存在的 config 触发 fetch 失败
mkdir -p /tmp/milnews-test && cp config.json /tmp/milnews-test/config.bak
mv config.json config.json.bak
set +e
bash run_daily.sh >/tmp/milnews-test/out.log 2>&1
rc=$?
set -e
mv config.json.bak config.json
echo "退出码: $rc"
if [ "$rc" -ne 2 ]; then echo "FAIL: 预期退出码2(抓取失败)，实际 $rc"; exit 1; fi
echo "PASS: 失败路径正确退出非0"

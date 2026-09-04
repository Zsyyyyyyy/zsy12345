#!/usr/bin/env bash
#
# 优雅重启：把「先跑迁移、再发代码」固化成一条命令
#
# 用法：
#   ./deploy.sh                 迁移 + 优雅重启
#   ./deploy.sh --skip-migrate  只重启，不跑迁移
#   ./deploy.sh --status        只看服务状态
#
# 为什么这样排序：
#   迁移（Expand）必须跑在代码发布之前。加列是向后兼容的——老代码不认识新列也能正常读写，
#   所以先加列不会造成中断；反过来先发读新列的代码、后加列，中间那段窗口期接口会直接
#   报 Unknown column，这才是真正的「有感」。
#
# 优雅关闭：
#   uvicorn 收到 SIGTERM 后停止接收新连接，正在处理的请求继续跑完，
#   最长等待 --timeout-graceful-shutdown 秒。配合 curl 健康检查，
#   把重启中断从「进程启停的几秒」压缩到「连接切换的毫秒级」。
#
# 想做到真正的零中断（毫秒都没有），需要起两个实例 + Nginx 做负载均衡切流，
#   单实例物理上做不到。本脚本的中断窗口 ≈ 旧进程退出到新进程 accept 之间的时间。

set -euo pipefail

cd "$(dirname "$0")"

APP_MODULE="app.main:app"
HOST="127.0.0.1"
PORT="${PORT:-8000}"
GRACEFUL_TIMEOUT=20          # 等待存量请求处理完的秒数
HEALTH_PATH="/"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/app.log"
PY="./venv/bin/python"

# curl 走 --noproxy：本机回环地址不要走 HTTP 代理，否则会被代理拦成 502
CURL="curl --noproxy * -sf"

mkdir -p "$LOG_DIR"

pid_on_port() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

wait_healthy() {
  local i
  for i in $(seq 1 40); do
    if $CURL "http://${HOST}:${PORT}${HEALTH_PATH}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

case "${1:-}" in
  --status)
    PID=$(pid_on_port)
    if [ -n "$PID" ]; then
      echo "运行中：PID=$PID  端口=$PORT"
      echo "健康：$($CURL -o /dev/null -w 'HTTP %{http_code}' "http://${HOST}:${PORT}${HEALTH_PATH}")"
    else
      echo "未运行（端口 $PORT 无监听）"
    fi
    exit 0
    ;;
  --skip-migrate)
    echo "· 跳过迁移（--skip-migrate）"
    ;;
  "")
    echo "▶ 步骤 1/3  执行数据库迁移"
    $PY migrate.py up
    ;;
  *)
    echo "未知参数：$1"
    echo "用法：./deploy.sh [--skip-migrate | --status]"
    exit 1
    ;;
esac

echo
echo "▶ 步骤 2/3  优雅停止旧进程"
OLD_PID=$(pid_on_port)
if [ -n "$OLD_PID" ]; then
  echo "  旧进程 PID=$OLD_PID，发送 SIGTERM（最长等待 ${GRACEFUL_TIMEOUT}s 处理完存量请求）"
  kill -TERM "$OLD_PID" 2>/dev/null || true
  for _ in $(seq 1 $((GRACEFUL_TIMEOUT * 4))); do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then break; fi
    sleep 0.25
  done
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "  宽限期结束仍未退出，强制 SIGKILL"
    kill -KILL "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  echo "  ✓ 旧进程已停止"
else
  echo "  端口 $PORT 无监听进程，跳过"
fi

echo
echo "▶ 步骤 3/3  启动新进程"
nohup "$PY" -m uvicorn "$APP_MODULE" \
  --host "$HOST" \
  --port "$PORT" \
  --timeout-graceful-shutdown "$GRACEFUL_TIMEOUT" \
  >> "$LOG_FILE" 2>&1 &

NEW_PID=$!
echo "  新进程 PID=$NEW_PID，等待健康检查"
if wait_healthy; then
  echo
  echo "✓ 部署完成：http://${HOST}:${PORT}"
  echo "  日志：$LOG_FILE"
else
  echo
  echo "✗ 健康检查未通过，请查看日志："
  tail -30 "$LOG_FILE"
  exit 1
fi

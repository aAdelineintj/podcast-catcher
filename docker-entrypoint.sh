#!/bin/bash
# 在同一容器内启动 FastAPI（内网 8000）与 Next.js（对外 PORT）。
#
# ── 容错策略：两个进程的重要性不对等 ──────────────────────────────
#   前端是公网入口。它挂了容器就没有存在意义 → 整体退出，交给平台重启。
#   后端只是前端的内部依赖。它挂了接口短暂不可用、页面仍在
#     → 由看门狗拉起，容器继续服务。
#
#   此前用 `wait -n` 把两者等同对待：任一进程退出就整个容器退出，
#   于是后端的一次崩溃会把前端一起拖下线（前端本身完全健康）。
#
#   为什么后端重启后前端不用跟着重启：next.config.ts 的 rewrites 是
#   「每个请求到达时才解析目标」的运行时转发，不是启动时建连。
#   后端恢复监听后转发会自动恢复，无需重启 Next.js。
#
#   代价：后端重启期间 GET /health 会返回 500（代理目标不存在）。
#   平台的健康检查打的是 `/`（由前端提供），不受影响。
set -u

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${PORT:-3000}"
BACKEND_ORIGIN="http://127.0.0.1:${BACKEND_PORT}"
export BACKEND_ORIGIN

# 后端崩溃后的重启退避：1s 起、每次翻倍、封顶 10s。
# 若某次存活超过 BACKEND_STABLE_SECONDS，视为「跑稳过」，下次从 1s 重新计。
# 这样偶发崩溃能秒级恢复，持续性崩溃（如配置错误）又不会刷爆日志。
BACKEND_STABLE_SECONDS=60
BACKEND_MAX_DELAY=10

BACKEND_PID=""
FRONTEND_PID=""
BACKEND_STARTED_AT=0
BACKEND_DELAY=1

echo "[entrypoint] backend  -> 0.0.0.0:${BACKEND_PORT}（看门狗守护，崩溃自动重启）"
echo "[entrypoint] frontend -> 0.0.0.0:${FRONTEND_PORT} (proxy -> ${BACKEND_ORIGIN})"

# python:3.13-slim 里没有 curl/wget，用 socket 做 TCP 探针
PROBE='import socket,sys;s=socket.socket();s.settimeout(1)
try:
    s.connect(("127.0.0.1",int(sys.argv[1])));sys.exit(0)
except Exception:
    sys.exit(1)'

backend_alive() {
  [ -n "${BACKEND_PID}" ] && kill -0 "${BACKEND_PID}" 2>/dev/null
}

frontend_alive() {
  [ -n "${FRONTEND_PID}" ] && kill -0 "${FRONTEND_PID}" 2>/dev/null
}

# 用子 shell 包住 cd，避免污染主 shell 的工作目录（前端要在 /app 下起）
start_backend() {
  (
    cd /app/backend || exit 1
    export PORT="${BACKEND_PORT}"
    exec python main.py
  ) &
  BACKEND_PID=$!
  BACKEND_STARTED_AT=$(date +%s)
}

# 等端口就绪再起前端，避免平台健康检查期间 /health 返回 500。
# 超时或后端已退出都不阻塞 —— 前端照起，代理会随后端恢复自动生效。
wait_for_backend() {
  local i=0
  while [ "${i}" -lt 60 ]; do
    if python -c "${PROBE}" "${BACKEND_PORT}" 2>/dev/null; then
      echo "[entrypoint] backend 已就绪（${i}00ms）"
      return 0
    fi
    if ! backend_alive; then
      echo "[entrypoint] backend 启动后即退出，交由看门狗重试"
      return 1
    fi
    i=$((i + 1))
    sleep 0.5
  done
  echo "[entrypoint] backend 就绪等待超时（30s），先起前端"
  return 1
}

start_backend
wait_for_backend || true

cd /app || exit 1
PORT="${FRONTEND_PORT}" ./node_modules/.bin/next start -H 0.0.0.0 -p "${FRONTEND_PORT}" &
FRONTEND_PID=$!

shutdown() {
  echo "[entrypoint] 收到退出信号，正在停止子进程…"
  kill -TERM "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# ── 监督循环 ──────────────────────────────────────────────────────
# 用 1s 轮询而非 `wait -n`：两个进程的处置逻辑可以分开写、一眼看懂，
# 且 1s 一次 kill -0 的开销可以忽略。
while true; do
  # 前端是公网入口：它没了，容器留着也没用 → 整体退出交给平台重启
  if ! frontend_alive; then
    echo "[entrypoint] 前端进程已退出（公网入口不可用），容器随之退出"
    kill -TERM "${BACKEND_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 1
  fi

  # 后端崩了：退避后拉起，容器继续服务
  if ! backend_alive; then
    elapsed=$(( $(date +%s) - BACKEND_STARTED_AT ))
    [ "${elapsed}" -ge "${BACKEND_STABLE_SECONDS}" ] && BACKEND_DELAY=1

    echo "[entrypoint] backend 已退出（存活 ${elapsed}s），${BACKEND_DELAY}s 后重启"
    sleep "${BACKEND_DELAY}"

    BACKEND_DELAY=$(( BACKEND_DELAY * 2 ))
    [ "${BACKEND_DELAY}" -gt "${BACKEND_MAX_DELAY}" ] && BACKEND_DELAY="${BACKEND_MAX_DELAY}"

    start_backend
    wait_for_backend || true
  fi

  sleep 1
done

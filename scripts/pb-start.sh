#!/bin/sh
# PocketBay 启动钩子（由 package.json 的 "start" 调用）。
#
# 同一个容器里起两个进程：
#   - FastAPI  绑 127.0.0.1:8000（内网，不对外）
#   - Next.js  绑 0.0.0.0:$PORT（对外，平台注入 PORT）
# Next.js 通过 next.config.ts 的 rewrites 把接口路径反代给 FastAPI。
#
# ── 容错策略（与 docker-entrypoint.sh 保持一致）────────────────────
#   前端是公网入口 → 它挂了整体退出，交给平台重启。
#   后端只是内部依赖 → 由看门狗拉起，容器继续服务。
#
#   此前是 `exec ./node_modules/.bin/next start` 直接替换掉 shell，
#   后端只是无人看管的后台子进程：后端崩了没人拉起，容器照旧对外服务，
#   前端能开但接口全挂。
#
#   为什么后端重启后前端不用跟着重启：next.config.ts 的 rewrites 是
#   「每个请求到达时才解析目标」的运行时转发，后端恢复监听后自动恢复。
#
#   代价：后端重启期间 GET /health 返回 500（代理目标不存在）。
#   平台健康检查打的是 `/`（前端提供），不受影响。
#
# 注意：本脚本跑在 Alpine 的 /bin/sh（BusyBox ash）下，
# 不能用 bash 专有语法（数组、`wait -n`、`local` 之外的 bash 内建等）。
set -u

PYROOT=/app/.pysys
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${PORT:-3000}"

export PYTHONPATH="${PYROOT}/site"
export PYTHONHOME="${PYROOT}/usr"
export LD_LIBRARY_PATH="${PYROOT}/usr/lib:${PYROOT}/site${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
# next.config.ts 的 rewrites 读这个变量
export BACKEND_ORIGIN="http://127.0.0.1:${BACKEND_PORT}"

# 后端崩溃后的重启退避：1s 起、每次翻倍、封顶 10s。
# 若某次存活超过 BACKEND_STABLE_SECONDS，视为「跑稳过」，下次从 1s 重新计。
BACKEND_STABLE_SECONDS=60
BACKEND_MAX_DELAY=10

BACKEND_PID=""
FRONTEND_PID=""
BACKEND_STARTED_AT=0
BACKEND_DELAY=1

if [ ! -x "${PYROOT}/usr/bin/python3" ]; then
  echo "[pb-start] 错误：找不到自带 Python（${PYROOT}/usr/bin/python3），构建阶段可能失败"
fi

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
    exec "${PYROOT}/usr/bin/python3" main.py
  ) &
  BACKEND_PID=$!
  BACKEND_STARTED_AT=$(date +%s)
}

# 等后端就绪再起前端，避免平台健康检查期间 /health 返回 500。
# 超时或后端已退出都不阻塞 —— 前端照起，代理会随后端恢复自动生效。
wait_for_backend() {
  i=0
  while [ "${i}" -lt 60 ]; do
    if wget -q -O /dev/null "http://127.0.0.1:${BACKEND_PORT}/health" 2>/dev/null; then
      echo "[pb-start] backend 已就绪（${i}00ms）"
      return 0
    fi
    if ! backend_alive; then
      echo "[pb-start] backend 启动后即退出，交由看门狗重试"
      return 1
    fi
    i=$((i + 1))
    sleep 0.5
  done
  echo "[pb-start] backend 就绪等待超时（30s），先起前端"
  return 1
}

shutdown() {
  echo "[pb-start] 收到退出信号，正在停止子进程…"
  kill -TERM "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

echo "[pb-start] backend  -> 127.0.0.1:${BACKEND_PORT}（看门狗守护，崩溃自动重启）"
start_backend
wait_for_backend || true

cd /app || exit 1
echo "[pb-start] frontend -> 0.0.0.0:${FRONTEND_PORT}  (proxy -> ${BACKEND_ORIGIN})"
./node_modules/.bin/next start -H 0.0.0.0 -p "${FRONTEND_PORT}" &
FRONTEND_PID=$!

# ── 监督循环 ──────────────────────────────────────────────────────
while true; do
  # 前端是公网入口：它没了，容器留着也没用 → 整体退出交给平台重启
  if ! frontend_alive; then
    echo "[pb-start] 前端进程已退出（公网入口不可用），容器随之退出"
    kill -TERM "${BACKEND_PID:-}" 2>/dev/null || true
    exit 1
  fi

  # 后端崩了：退避后拉起，容器继续服务
  if ! backend_alive; then
    elapsed=$(( $(date +%s) - BACKEND_STARTED_AT ))
    [ "${elapsed}" -ge "${BACKEND_STABLE_SECONDS}" ] && BACKEND_DELAY=1

    echo "[pb-start] backend 已退出（存活 ${elapsed}s），${BACKEND_DELAY}s 后重启"
    sleep "${BACKEND_DELAY}"

    BACKEND_DELAY=$(( BACKEND_DELAY * 2 ))
    [ "${BACKEND_DELAY}" -gt "${BACKEND_MAX_DELAY}" ] && BACKEND_DELAY="${BACKEND_MAX_DELAY}"

    start_backend
    wait_for_backend || true
  fi

  sleep 1
done

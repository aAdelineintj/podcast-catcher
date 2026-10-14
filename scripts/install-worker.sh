#!/bin/sh
# 把本机 worker 装成 launchd 常驻服务：开机自启 + 崩溃自动重启。
#
# ── 为什么需要它 ──
# 整个架构是「云端只存数据和调度，算力留在本机」。worker 是唯一跑在
# 你电脑上的那一环 —— 它没启动，上传的音频就会永远停在「排队中」，
# 而界面上没有任何线索说明「你的电脑上那个进程没开着」。
#
# ── 用法 ──
#   sh scripts/install-worker.sh            安装并立即启动
#   sh scripts/install-worker.sh status     查看运行状态与最近日志
#   sh scripts/install-worker.sh logs       实时跟踪日志（Ctrl-C 退出）
#   sh scripts/install-worker.sh uninstall  卸载（停止 + 删除 plist）
#
# ── 一个必须记住的坑 ──
# launchd 启动进程时的 PATH 极短（只有 /usr/bin:/bin:/usr/sbin:/sbin），
# **不含 /opt/homebrew/bin**。而 ffmpeg 装在那里 —— 少了它，音频压缩会被
# worker 静默跳过（不报错，只是 Storage 占用偏高）。所以下面的 plist 里
# 显式写死了 PATH。

set -eu

LABEL="com.podcastcatcher.worker"
PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON="$PROJECT_ROOT/venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
STDOUT_LOG="$LOG_DIR/podcast-worker.log"
STDERR_LOG="$LOG_DIR/podcast-worker.error.log"
DOMAIN="gui/$(id -u)"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

do_install() {
  # ── 前置检查：宁可在这里说清楚，也别让 launchd 反复重启一个配错的进程 ──
  if [ ! -x "$PYTHON" ]; then
    echo "✗ 找不到可执行的 $PYTHON" >&2
    echo "  先建虚拟环境：cd \"$PROJECT_ROOT\" && python3 -m venv venv && venv/bin/pip install -r backend/requirements.txt" >&2
    exit 1
  fi

  if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "✗ 找不到 $PROJECT_ROOT/.env" >&2
    echo "  先复制 .env.example 为 .env 并填好 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / PASSPHRASE / AUTH_SECRET" >&2
    exit 1
  fi

  for key in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
    if ! grep -q "^${key}=" "$PROJECT_ROOT/.env"; then
      echo "✗ .env 里缺少 $key" >&2
      exit 1
    fi
  done

  # 先跑一次配置校验。worker 自己也会校验，但在这里跑能让错误直接显示在终端，
  # 而不是只落进日志文件里 —— 配错时最常见的现象就是「装了但什么都没发生」。
  echo "→ 校验配置…"
  if ! (cd "$PROJECT_ROOT/backend" && "$PYTHON" -c "
import sys
sys.path.insert(0, '.')
from worker import config
try:
    config.validate()
except config.ConfigError as exc:
    print(f'配置错误：{exc}', file=sys.stderr)
    sys.exit(1)
print('配置 OK：' + config.describe())
"); then
    echo "✗ 配置校验未通过，已中止安装" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"

  # 生成 plist。几个要点写在这里而不是 plist 内部：
  #   · PATH 必须含 /opt/homebrew/bin —— ffmpeg 在那里，少了它音频压缩会被静默跳过
  #   · PYTHONUNBUFFERED=1 —— 否则日志攒满一块才落盘，排查时看不到实时进度
  #   · KeepAlive + ThrottleInterval=15 —— 崩溃自动拉起，但不会疯狂重启刷爆日志
  #   · 下面这段 heredoc 里不放 XML 注释 —— 曾经怀疑注释会让 bootstrap 报
  #     `5: Input/output error`，但 2026-09-20 用无注释的极简 plist 实测**同样报 EIO**，
  #     所以那个归因是错的（真正原因是执行上下文，见下面的 bootstrap 段）。
  #     仍然不放注释，纯粹是因为注释在这里没有价值，理由写在 shell 侧更清楚。
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>worker.worker</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$PROJECT_ROOT/backend</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>15</integer>

  <key>StandardOutPath</key>
  <string>$STDOUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$STDERR_LOG</string>
</dict>
</plist>
PLIST_EOF

  # 已加载过就先卸载，避免 bootstrap 报「service already loaded」
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

  # bootstrap 必须在**用户的 GUI 登录会话**里执行。
  # 从守护进程 / IDE 内置终端 / 远程会话里跑，launchd 会直接拒绝，而且报的是
  # 一个完全指不到原因的 `5: Input/output error`。2026-09-20 实测：连
  # `/bin/sleep 3600` 这种极简 plist 也一样报 EIO，`launchctl setenv` 则明说
  # 「Not privileged to set domain environment」—— 即问题不在 plist 内容，
  # 而在当前进程不属于该域。所以这里自己兜住错误，把「该去哪儿跑」说出来。
  if ! bootstrap_out=$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1); then
    echo "✗ 加载 launchd 服务失败：" >&2
    echo "  $bootstrap_out" >&2
    echo >&2
    echo "  最常见的原因：当前进程不在你的 GUI 登录会话里。" >&2
    echo "  请打开「终端」App（不是 IDE 内置终端、不是远程会话）重新执行：" >&2
    echo "      sh $0" >&2
    exit 1
  fi

  launchctl kickstart -k "$DOMAIN/$LABEL"

  echo "✓ 已安装并启动：$LABEL"
  echo
  echo "  日志：$STDOUT_LOG"
  echo "  状态：sh $0 status"
  echo "  卸载：sh $0 uninstall"
  echo
  echo "  提示：日志不会自动轮转。跑久了想清理就 rm 掉上面那个文件再 kickstart。"
}

do_status() {
  echo "── launchd ──────────────────────────────"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
      | grep -E "^\s*(state|pid|last exit code|program) " || true
  else
    echo "未安装（先跑 sh $0 install）"
    return 0
  fi

  echo
  echo "── 最近日志 ─────────────────────────────"
  if [ -f "$STDOUT_LOG" ]; then
    tail -n 20 "$STDOUT_LOG"
  else
    echo "暂无日志"
  fi

  echo
  echo "── 心跳（前端就是靠它判断 worker 在线）──"
  # 这一步会真去 Supabase 读一次 worker_heartbeat。它比看进程状态更有意义：
  # 进程活着但连不上数据库时，前端仍然显示「离线」。
  (cd "$PROJECT_ROOT/backend" && "$PYTHON" -m worker.e2e_cloud check 2>&1 | tail -n 8) || true
}

do_logs() {
  [ -f "$STDOUT_LOG" ] || { echo "还没有日志文件：$STDOUT_LOG"; exit 0; }
  echo "跟踪 $STDOUT_LOG（Ctrl-C 退出）"
  tail -f "$STDOUT_LOG"
}

do_uninstall() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ 已卸载：$LABEL（日志文件保留在 $LOG_DIR，需要就自己删）"
}

case "${1:-install}" in
  install)   do_install ;;
  status)    do_status ;;
  logs)      do_logs ;;
  uninstall) do_uninstall ;;
  *)         usage ;;
esac

"""worker 的配置读取与校验。

与 `main.py` 的差别：worker 是**常驻进程**，配置错了应该在启动时就失败，
而不是等跑任务时才抛异常 —— 否则用户看到的现象是「任务永远停在排队中」，
排查时还得先怀疑前端、再怀疑数据库，最后才想到 worker 自己没配对。

所以这里提供 `validate()`：缺什么直接点名，进程立即退出。
"""

from __future__ import annotations

import os
import socket

from dotenv import load_dotenv

# 仓库根目录（backend/ 的上一级）。与 main.py / pipeline.py 保持同一套 .env 加载顺序。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))
load_dotenv()

# ── Supabase ──────────────────────────────────────────────────────────
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET") or "podcast-audio"

# ── worker 身份与节奏 ─────────────────────────────────────────────────
# WORKER_ID 会写进 podcasts.locked_by 与 worker_heartbeat.worker_id，
# 多台机器同时跑时必须各不相同（默认用 主机名-PID 就够）。
WORKER_ID = os.getenv("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"

# 空轮询间隔。太短会白白烧 Supabase 免费版的请求数，太长则上传后要等很久才开工。
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "5"))
# 心跳间隔。它同时承担「Supabase 保活」（免费项目 7 天无活动休眠）的作用，
# 不要为了省请求把它调得很长。
WORKER_HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", "5"))

# ── 本地推理与临时文件 ────────────────────────────────────────────────
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
# 音频中转目录。下载回来的原始文件放这里，处理完即删。
WORKER_TMP_DIR = os.getenv("WORKER_TMP_DIR") or os.path.join(_REPO_ROOT, "data", "worker_tmp")

# 转写完成后把音频压到 64kbps 单声道再回传，体积约为原来的 15%。
# Supabase 免费版只有 1GB Storage，不压的话只够存 10~20 集。
# 本机 ffmpeg 若不在 PATH（launchd 的 PATH 很短，见 plist 注释），自动跳过而不是报错。
AUDIO_COMPRESS = os.getenv("AUDIO_COMPRESS", "1").strip().lower() not in ("0", "false", "no", "")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "64k")

# 单集最多重试次数。与 schema 里 claim_next_podcast 的 `attempts < 3` 必须一致，
# 否则会出现「数据库允许认领、但 worker 认为自己不该再试」的僵局。
MAX_ATTEMPTS = 3


class ConfigError(RuntimeError):
    pass


def validate() -> None:
    """启动时校验必需配置，缺失就抛错退出。"""
    missing: list[str] = []

    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    elif not SUPABASE_URL.startswith("https://"):
        # Supabase 只提供 https。写成 http:// 或漏了协议头，症状是连接被拒/超时，
        # 很容易被误判成「网络问题」，所以在启动时就点出来。
        raise ConfigError(
            f"SUPABASE_URL 必须以 https:// 开头，当前是 {SUPABASE_URL!r}\n"
            f"    正确形式：https://<project-ref>.supabase.co"
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    elif SUPABASE_SERVICE_ROLE_KEY.startswith("sb_publishable") or len(SUPABASE_SERVICE_ROLE_KEY) < 40:
        # 用错 key 是高频错误：publishable/anon key 受 RLS 限制，而本项目 RLS 是零策略，
        # 用它连上去「什么都能连、但什么都读不到」，极难排查。这里提前拦住。
        raise ConfigError(
            "SUPABASE_SERVICE_ROLE_KEY 看起来不是 service_role key。\n"
            "    本项目 RLS 为零策略，用 anon / publishable key 会读不到任何数据。\n"
            "    请在 Supabase Dashboard → Project Settings → API 里复制 service_role 那个（以 eyJ 开头的 JWT）。"
        )

    if missing:
        raise ConfigError(
            "缺少必需的环境变量：" + "、".join(missing) + "\n"
            "    请写进仓库根的 .env（本地）或 Vercel/launchd 的环境（常驻）。\n"
            "    参考 .env.example。"
        )


def describe() -> str:
    """给启动日志用的一行摘要（不打印任何密钥）。"""
    return (
        f"worker_id={WORKER_ID} model={WHISPER_MODEL} "
        f"bucket={SUPABASE_STORAGE_BUCKET} "
        f"poll={WORKER_POLL_SECONDS:g}s heartbeat={WORKER_HEARTBEAT_SECONDS:g}s "
        f"compress={'on' if AUDIO_COMPRESS else 'off'}"
    )

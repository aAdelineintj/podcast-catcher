"""云端任务执行器：轮询 Supabase → 本地 Whisper 转写 → 百度翻译 → 回写。

**为什么任务要跑在本机**：Whisper 需要 CPU/GPU，Vercel 的 Serverless 跑不动也跑不起。
所以架构是「云端只存数据和调度，算力留在本机」——这也是整个方案能完全免费的原因。

**启动方式**（必须用 `-m`，因为要 import 同级的 config / supabase_client）：

    cd backend && ../venv/bin/python -m worker.worker

**与本地 FastAPI 的关系**：两条路径共用 `pipeline.py` 的切句与翻译口径，
共用 `whisper_asr.py` 的转写实现。worker 里**不重复实现任何管线逻辑** ——
一旦这里出现第二份「切句规则」，本地和线上就会给出两套字幕。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import traceback

# worker/ 是 backend/ 的子目录，而 backend/ 不是一个包（没有 __init__.py），
# 所以 `from .. import pipeline` 走不通。直接把 backend/ 塞进 sys.path 最省事。
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import pipeline  # noqa: E402  （必须在 sys.path 调整之后导入）
import podcast_asr  # noqa: E402
import whisper_asr  # noqa: E402

from . import config  # noqa: E402
from .supabase_client import SupabaseClient  # noqa: E402

# 进度区间与本地 main.py 完全一致，前端才能用同一套 stage/progress 渲染
PROGRESS_TRANSCRIBE_START = 5
PROGRESS_TRANSCRIBE_END = 20
PROGRESS_TRANSLATE_END = 95

# 每翻译 20 句回写一次 segments。与 main.py 的刷盘阈值一致：
# worker 中途断电时，已翻译的部分不会白跑。
TRANSLATE_FLUSH_EVERY = 20


# ══════════════════════════════════════════════════════════════════════
#  状态回写
# ══════════════════════════════════════════════════════════════════════

def patch_podcast(client: SupabaseClient, podcast_id: str, **values) -> None:
    client.update("podcasts", {"id": f"eq.{podcast_id}"}, values)


def upsert_segments(client: SupabaseClient, podcast_id: str, segments: list[dict]) -> None:
    rows = [
        {
            "podcast_id": podcast_id,
            "idx": index,
            "start_label": segment["start"],
            "start_seconds": segment["start_seconds"],
            "end_seconds": segment.get("end_seconds"),
            "original": segment["original"],
            "detected_lang": segment.get("detected_lang"),
            "detection_reason": segment.get("detection_reason"),
            "translation": segment.get("translation") or None,
            "translation_status": segment.get("translation_status") or "pending",
        }
        for index, segment in enumerate(segments)
    ]
    client.upsert("segments", rows, on_conflict="podcast_id,idx")


# ══════════════════════════════════════════════════════════════════════
#  音频压缩
# ══════════════════════════════════════════════════════════════════════

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def compress_audio(src_path: str, dst_path: str) -> bool:
    """把音频压成 64kbps 单声道 mp3。成功返回 True。

    为什么值得做：Supabase 免费版只有 1GB Storage，而播客原始 mp3 常 20~100MB，
    不压只够存 10~20 集；压到 64kbps 单声道后体积约为原来的 15%，能存 6 倍以上。

    **失败不抛异常**：转写和字幕已经写进库了，压缩只是省空间，
    不该因为一个可选步骤把整个任务判成 failed。
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-ac", "1",                       # 单声道
        "-b:a", config.AUDIO_BITRATE,     # 默认 64k
        "-f", "mp3",
        dst_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"    ⚠ 压缩失败（{type(exc).__name__}: {exc}），保留原始音频")
        return False

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:300]
        print(f"    ⚠ 压缩失败（ffmpeg 退出码 {result.returncode}）：{detail}")
        return False
    return os.path.exists(dst_path) and os.path.getsize(dst_path) > 0


def compress_and_replace(
    client: SupabaseClient, podcast_id: str, audio_path: str, work_dir: str
) -> None:
    """压缩音频并回传，把 podcasts.audio_path 指向新对象。

    压缩产物统一是 mp3，所以新对象路径固定为 `<id>.mp3`；
    如果原对象是别的扩展名（m4a / wav），上传成功后要把原对象删掉，
    否则 Storage 里会同时躺着两个文件，白占一份空间。
    """
    if not config.AUDIO_COMPRESS:
        return
    if not _ffmpeg_available():
        print("    ⚠ 找不到 ffmpeg，跳过音频压缩（Storage 占用会偏高）")
        print("      提示：launchd 启动时 PATH 很短，plist 里要显式加上 /opt/homebrew/bin")
        return

    src_path = os.path.join(work_dir, "source" + os.path.splitext(audio_path)[1])
    dst_path = os.path.join(work_dir, "compressed.mp3")
    if not os.path.exists(src_path):
        return

    before = os.path.getsize(src_path)
    if not compress_audio(src_path, dst_path):
        return

    after = os.path.getsize(dst_path)
    new_path = f"{podcast_id}.mp3"
    with open(dst_path, "rb") as file_obj:
        client.upload_object(new_path, file_obj.read(), content_type="audio/mpeg")

    if new_path != audio_path:
        try:
            client.delete_object(audio_path)
        except Exception as exc:  # noqa: BLE001
            # 删不掉只是浪费空间，不影响这一集能否播放
            print(f"    ⚠ 原始音频删除失败（不影响播放）：{exc}")

    patch_podcast(client, podcast_id, audio_path=new_path, audio_bytes=after)
    print(f"    ✓ 音频压缩 {before / 1e6:.1f}MB -> {after / 1e6:.1f}MB（{after / before:.0%}）")


# ══════════════════════════════════════════════════════════════════════
#  单集处理
# ══════════════════════════════════════════════════════════════════════

def process(client: SupabaseClient, job: dict) -> None:
    podcast_id = job["id"]
    source_language = pipeline.normalize_source_language(job.get("source_language"))
    audio_path = job.get("audio_path")

    print(
        f"[{podcast_id}] 开始处理：{job.get('title')!r} "
        f"(source_language={source_language}, audio={audio_path})"
    )

    if not audio_path:
        raise RuntimeError("podcasts.audio_path 为空，无法下载音频")

    work_dir = os.path.join(config.WORKER_TMP_DIR, podcast_id)
    os.makedirs(work_dir, exist_ok=True)
    local_audio = os.path.join(work_dir, "source" + os.path.splitext(audio_path)[1])

    try:
        # ── 1. 下载音频 ──────────────────────────────────────────────
        print(f"[{podcast_id}] 1/6 下载音频…")
        with open(local_audio, "wb") as file_obj:
            file_obj.write(client.download_object(audio_path))
        print(f"[{podcast_id}]      {os.path.getsize(local_audio) / 1e6:.1f}MB")

        # ── 2. 转写 ─────────────────────────────────────────────────
        print(f"[{podcast_id}] 2/6 转写（Whisper {whisper_asr.DEFAULT_MODEL}，本地 CPU）…")
        started = time.time()

        def on_transcribe_progress(done: int, total: int) -> None:
            ratio = done / max(total, 1)
            progress = PROGRESS_TRANSCRIBE_START + int(
                ratio * (PROGRESS_TRANSCRIBE_END - PROGRESS_TRANSCRIBE_START)
            )
            patch_podcast(
                client, podcast_id, stage="transcribing",
                progress=min(progress, PROGRESS_TRANSCRIBE_END),
            )

        rows = whisper_asr.transcribe_audio(
            local_audio,
            whisper_asr.get_model(),
            source_language=source_language,
            on_progress=on_transcribe_progress,
        )

        if source_language == "auto":
            sample = " ".join(row["text"] for row in rows[:40])
            transcription_language = podcast_asr.detect_text_language(sample)
        else:
            transcription_language = source_language

        elapsed = time.time() - started
        print(
            f"[{podcast_id}]      {len(rows)} 行 / {elapsed:.1f}s "
            f"({elapsed / max(_audio_duration(rows), 1):.2f}x 实时) / language={transcription_language}"
        )

        # ── 3. 组装字幕段 ───────────────────────────────────────────
        segments = pipeline.build_segments(rows, source_language, transcription_language)
        if not segments:
            raise RuntimeError("转写结果为空，未得到任何字幕行")

        # 时长优先取容器声明的**真实时长**。
        #
        # 为什么不用「最后一句的结束时间」：Whisper 的最后一段通常在尾部静音
        # 之前就结束了 —— 一集 25 秒的音频最后一句只到 23.8 秒，于是列表显示
        # 0:23、播放器显示 0:25，用户一眼就能看出不一致。
        # 探测失败（容器头损坏等）才回落到最后一句的结束时间。
        last_end = float(segments[-1].get("end_seconds") or segments[-1]["start_seconds"])
        audio_duration = podcast_asr.probe_duration(local_audio) or last_end
        patch_podcast(
            client, podcast_id,
            stage="transcribed",
            progress=PROGRESS_TRANSCRIBE_END,
            transcription_language=transcription_language,
            total_segments=len(segments),
            duration_label=pipeline.format_timestamp(audio_duration),
            duration_seconds=audio_duration,
        )
        # 先把原文落库：即便后面翻译挂掉，用户至少能看到字幕
        upsert_segments(client, podcast_id, segments)

        # ── 4. 翻译 ─────────────────────────────────────────────────
        print(f"[{podcast_id}] 3/6 翻译 {len(segments)} 句…")
        total_segments = max(len(segments), 1)
        flushed = 0

        def on_translate_progress(done_count: int) -> None:
            nonlocal flushed
            progress = PROGRESS_TRANSCRIBE_END + int(
                (done_count / total_segments) * (PROGRESS_TRANSLATE_END - PROGRESS_TRANSCRIBE_END)
            )
            patch_podcast(
                client, podcast_id,
                stage="translating",
                progress=min(progress, PROGRESS_TRANSLATE_END),
                translated_count=done_count,
            )
            if done_count - flushed >= TRANSLATE_FLUSH_EVERY:
                upsert_segments(client, podcast_id, segments)
                flushed = done_count

        pipeline.translate_segments(segments, on_progress=on_translate_progress)

        # ── 5. 收尾回写 ─────────────────────────────────────────────
        upsert_segments(client, podcast_id, segments)
        patch_podcast(
            client, podcast_id,
            status="success",
            stage="completed",
            progress=100,
            translated_count=len(segments),
            total_segments=len(segments),
            error_message=None,
            locked_by=None,
            locked_at=None,
        )
        print(f"[{podcast_id}] 4/6 字幕已入库（{len(segments)} 段）")

        # ── 6. 压缩音频 ─────────────────────────────────────────────
        print(f"[{podcast_id}] 5/6 压缩音频…")
        compress_and_replace(client, podcast_id, audio_path, work_dir)
        print(f"[{podcast_id}] 6/6 完成 ✓")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _audio_duration(rows: list[dict]) -> float:
    """用最后一行字幕的 end 估算音频时长，只为打日志。"""
    if not rows:
        return 0.0
    return float(rows[-1].get("end") or 0.0)


def handle_failure(client: SupabaseClient, job: dict, exc: Exception) -> None:
    """失败处理：记错误；还有重试次数就退回队列，否则永久标记 failed。

    为什么要有重试：网络抖动、百度翻译偶发限流、音频临时下载失败都很常见，
    这些情况下「自动重来一次」远比让用户手动重传体验好。
    """
    podcast_id = job["id"]
    attempts = int(job.get("attempts") or 0)
    message = f"{type(exc).__name__}: {exc}"[:500]
    can_retry = attempts < config.MAX_ATTEMPTS

    print(f"[{podcast_id}] ✗ 失败（第 {attempts}/{config.MAX_ATTEMPTS} 次）：{message}")
    if can_retry:
        print(f"[{podcast_id}]   退回队列，等待重试")

    try:
        patch_podcast(
            client, podcast_id,
            status="queued" if can_retry else "failed",
            stage="queued" if can_retry else "failed",
            error_message=message,
            locked_by=None,
            locked_at=None,
        )
    except Exception as patch_exc:  # noqa: BLE001
        # 连状态都写不回去时，至少让日志留下痕迹 —— 否则这行会一直卡在 processing
        print(f"[{podcast_id}]   ⚠ 回写失败状态也失败了：{patch_exc}")


# ══════════════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════════════

def touch_worker(client: SupabaseClient, current_job: str | None) -> None:
    client.rpc(
        "touch_worker",
        {
            "p_worker_id": config.WORKER_ID,
            "p_model": config.WHISPER_MODEL,
            "p_current_job": current_job,
            "p_hostname": os.uname().nodename,
        },
    )


def claim_next(client: SupabaseClient) -> dict | None:
    """原子认领一个任务。返回 None 表示当前没有待处理任务。"""
    rows = client.rpc("claim_next_podcast", {"p_worker_id": config.WORKER_ID})
    if not rows:
        return None
    return rows[0]


def main() -> int:
    try:
        config.validate()
    except config.ConfigError as exc:
        print(f"[worker] 配置错误，启动中止：\n    {exc}", file=sys.stderr)
        return 2

    os.makedirs(config.WORKER_TMP_DIR, exist_ok=True)
    client = SupabaseClient()

    print(f"[worker] 启动 {config.describe()}")
    print(f"[worker] 临时目录 {config.WORKER_TMP_DIR}")
    if not _ffmpeg_available():
        print("[worker] ⚠ PATH 里没有 ffmpeg，音频压缩会被跳过")

    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= config.WORKER_HEARTBEAT_SECONDS:
            try:
                touch_worker(client, None)
                last_heartbeat = now
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] ⚠ 心跳失败（稍后重试）：{exc}")
                time.sleep(config.WORKER_POLL_SECONDS)
                continue

        try:
            job = claim_next(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] ⚠ 认领任务失败（稍后重试）：{exc}")
            time.sleep(config.WORKER_POLL_SECONDS)
            continue

        if not job:
            time.sleep(config.WORKER_POLL_SECONDS)
            continue

        try:
            touch_worker(client, job["id"])
            process(client, job)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            handle_failure(client, job, exc)
        finally:
            try:
                touch_worker(client, None)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[worker] 已手动停止")
        sys.exit(0)

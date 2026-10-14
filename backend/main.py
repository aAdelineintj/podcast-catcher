import json
import os
import shutil
import threading
import uuid
from glob import glob

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import pipeline
import podcast_asr
import whisper_asr

# 仓库根目录（backend/ 的上一级）。配置与数据都以仓库根为基准，避免受启动工作目录影响。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 先读仓库根的 .env，再读当前工作目录的 .env（不覆盖已有值）
load_dotenv(os.path.join(_REPO_ROOT, ".env"))
load_dotenv()

app = FastAPI()
router = APIRouter()

_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# PocketBay 会挂载持久卷并注入 POCKETBAY_DATA_DIR=/data；本地开发回落到 <仓库根>/data
DATA_ROOT = os.getenv("POCKETBAY_DATA_DIR") or os.path.join(_REPO_ROOT, "data")
INPUT_DIR = os.path.join(DATA_ROOT, "input")
OUTPUT_DIR = os.path.join(DATA_ROOT, "output")
# 任务元数据落盘目录：进程重启后据此恢复任务，避免「查询 404 → 前端静默停摆」
JOB_STATE_DIR = os.path.join(DATA_ROOT, "jobs")
os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOB_STATE_DIR, exist_ok=True)

app.mount("/media", StaticFiles(directory=INPUT_DIR, check_dir=False), name="media")

# 腾讯云密钥：仅在 ASR_ENGINE=tencent 时需要，whisper 模式下不会被读取
TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID") or os.getenv("TENCENTCLOUD_SECRET_ID")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY") or os.getenv("TENCENTCLOUD_SECRET_KEY")
TENCENT_REGION = os.getenv("TENCENT_REGION", "ap-shanghai")
ASR_MAX_WORKERS = int(os.getenv("ASR_MAX_WORKERS", "16"))

# ASR 引擎：whisper = 本地 Whisper（免费、无额度）；tencent = 云端一句话识别。
# 两者对上层返回完全相同的 [{start, end, text}]，所以改这一个变量就能整条链路回滚。
ASR_ENGINE = os.getenv("ASR_ENGINE", "whisper").strip().lower()

# 语种判定 / 时间戳格式 / 百度翻译的参数与实现都在 pipeline.py —— 与云端 worker 共用同一份，
# 保证「同一集音频在本地跑和线上跑」产出逐字节一致的字幕。

# 不透明句柄：tencent 模式是 TencentSentenceASR 实例，whisper 模式是 Whisper 模型
_asr_client = None
_asr_lock = threading.Lock()


def get_asr_client():
    """延迟初始化：缺少密钥时应用仍能启动，只有跑任务时才报错。

    whisper 模式返回 Whisper 模型对象，tencent 模式返回 TencentSentenceASR 实例。
    两者都只被当作不透明句柄传给对应的 transcribe_audio，上层不关心类型。
    """
    global _asr_client
    if _asr_client is None:
        with _asr_lock:
            if _asr_client is None:
                if ASR_ENGINE == "tencent":
                    _asr_client = podcast_asr.TencentSentenceASR(
                        TENCENT_SECRET_ID,
                        TENCENT_SECRET_KEY,
                        region=TENCENT_REGION,
                        max_workers=ASR_MAX_WORKERS,
                    )
                else:
                    _asr_client = whisper_asr.get_model()
    return _asr_client


jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# 需要落盘的任务元数据字段。segments 体积大，单独写进 OUTPUT_DIR 的结果文件，不在这里重复存。
_JOB_STATE_FIELDS = (
    "job_id", "status", "stage", "filename", "source_language",
    "transcription_language", "progress", "translated_count",
    "total_segments", "error",
)


def job_state_path(job_id: str) -> str:
    return os.path.join(JOB_STATE_DIR, f"{job_id}.json")


def persist_job(job_id: str) -> None:
    """把任务元数据写盘（原子替换）。落盘失败不影响主流程。"""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        snapshot = {key: job.get(key) for key in _JOB_STATE_FIELDS}

    path = job_state_path(job_id)
    try:
        with open(f"{path}.tmp", "w", encoding="utf-8") as file_obj:
            json.dump(snapshot, file_obj, ensure_ascii=False, indent=2)
        os.replace(f"{path}.tmp", path)
    except OSError as exc:
        print(f"[{job_id}] 任务状态落盘失败: {exc}")


def write_result_file(job_id: str, segments: list[dict]) -> str:
    """把当前已得到的字幕写进 OUTPUT_DIR（原子替换）。

    转录一完成就调用一次，这样即便后续翻译阶段挂掉或进程重启，
    已经转出来的原文也不会丢。
    """
    result_path = os.path.join(OUTPUT_DIR, f"{job_id}.json")
    with open(f"{result_path}.tmp", "w", encoding="utf-8") as file_obj:
        json.dump(segments, file_obj, ensure_ascii=False, indent=2)
    os.replace(f"{result_path}.tmp", result_path)
    return result_path


def load_persisted_jobs() -> int:
    """启动时回灌历史任务。

    没有这一步时：进程一重启，GET /jobs/{id} 就返回 404，前端轮询静默停摆，
    用户只能看到「一直卡在处理中」，完全不知道发生了什么。
    """
    restored = 0
    for state_path in sorted(glob(os.path.join(JOB_STATE_DIR, "*.json"))):
        try:
            with open(state_path, "r", encoding="utf-8") as file_obj:
                meta = json.load(file_obj)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[jobs] 跳过损坏的任务状态文件 {state_path}: {exc}")
            continue

        job_id = str(meta.get("job_id") or os.path.splitext(os.path.basename(state_path))[0])
        result_path = os.path.join(OUTPUT_DIR, f"{job_id}.json")

        segments: list[dict] = []
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as file_obj:
                    loaded = json.load(file_obj)
                if isinstance(loaded, list):
                    segments = loaded
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[{job_id}] 结果文件解析失败: {exc}")

        status = str(meta.get("status") or "unknown")
        stage = str(meta.get("stage") or "unknown")
        error = meta.get("error")

        # 落盘状态仍是「排队中/处理中」，说明上次是被强制中断的（进程重启）。
        # 必须显式转成 failed 并写明原因，否则前端会一直等一个不会到来的结果。
        if status in {"queued", "processing"}:
            status, stage = "failed", "failed"
            error = "任务在后端进程重启时中断，结果未生成，请重新上传该音频。"

        with jobs_lock:
            jobs[job_id] = {
                "job_id": job_id,
                "status": status,
                "stage": stage,
                "filename": meta.get("filename") or job_id,
                "source_language": pipeline.normalize_source_language(meta.get("source_language")),
                "transcription_language": meta.get("transcription_language"),
                "progress": int(meta.get("progress") or 0),
                "translated_count": int(meta.get("translated_count") or 0),
                "total_segments": int(meta.get("total_segments") or len(segments)),
                "error": error,
                "segments": segments,
                "input_path": resolve_job_media_path(job_id),
                "output_path": result_path if segments else None,
            }
        restored += 1

    return restored

def build_progress_snapshot(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        segments = [dict(segment) for segment in job["segments"]]
        media_filename = os.path.basename(job["input_path"]) if job.get("input_path") else None
        return {
            "job_id": job_id,
            "status": job["status"],
            "stage": job["stage"],
            "filename": job["filename"],
            "source_language": job.get("source_language", "auto"),
            "transcription_language": job.get("transcription_language"),
            "media_url": f"/media/{media_filename}" if media_filename else None,
            "progress": job["progress"],
            "translated_count": job["translated_count"],
            "total_segments": job["total_segments"],
            "error": job["error"],
            "segments": segments,
        }


def resolve_job_media_path(job_id: str) -> str | None:
    matches = sorted(glob(os.path.join(INPUT_DIR, f"{job_id}.*")))
    return matches[0] if matches else None


def update_job(job_id: str, **changes):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)
    persist_job(job_id)


def create_job_record(job_id: str, filename: str, input_path: str, source_language: str = "auto"):
    normalized_source_language = pipeline.normalize_source_language(source_language)
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "stage": "uploaded",
            "filename": filename,
            "source_language": normalized_source_language,
            "transcription_language": None,
            "progress": 0,
            "translated_count": 0,
            "total_segments": 0,
            "error": None,
            "segments": [],
            "input_path": input_path,
            "output_path": None,
        }
    persist_job(job_id)


def queue_processing_job(job_id: str, input_path: str, source_language: str = "auto"):
    worker = threading.Thread(
        target=process_job,
        args=(job_id, input_path, source_language),
        daemon=True,
    )
    worker.start()


def process_job(job_id: str, input_path: str, source_language: str = "auto"):
    segments: list[dict] = []
    try:
        source_language = pipeline.normalize_source_language(source_language)

        update_job(job_id, status="processing", stage="transcribing", progress=5)
        engine_label = (
            "Tencent SentenceRecognition (cloud)"
            if ASR_ENGINE == "tencent"
            else f"Whisper (local, {whisper_asr.DEFAULT_MODEL})"
        )
        print(f"[{job_id}] 1. File saved: {input_path}")
        print(
            f"[{job_id}] 2. Transcribing ({engine_label}): "
            f"{jobs[job_id]['filename']} (source_language={source_language})"
        )

        asr = get_asr_client()

        def on_progress(done: int, total: int) -> None:
            progress = 5 + int((done / max(total, 1)) * 15)
            update_job(job_id, stage="transcribing", progress=min(progress, 20))

        transcribe = (
            podcast_asr.transcribe_audio
            if ASR_ENGINE == "tencent"
            else whisper_asr.transcribe_audio
        )
        rows = transcribe(
            input_path,
            asr,
            source_language=source_language,
            on_progress=on_progress,
        )

        if source_language == "auto":
            sample = " ".join(row["text"] for row in rows[:40])
            transcription_language = podcast_asr.detect_text_language(sample)
        else:
            transcription_language = source_language
        update_job(job_id, transcription_language=transcription_language)
        print(
            f"[{job_id}] 3. Transcription finished: {len(rows)} rows, "
            f"language={transcription_language}"
        )

        segments = pipeline.build_segments(rows, source_language, transcription_language)

        update_job(
            job_id,
            stage="transcribed",
            progress=20,
            total_segments=len(segments),
            segments=segments,
        )

        # 转录一完成就先落一版盘：即便后续翻译阶段挂掉或进程重启，
        # 已经转出来的原文也不会丢。
        write_result_file(job_id, segments)

        print(f"[{job_id}] 4. Translating {len(segments)} segments...")
        total_segments = max(len(segments), 1)
        flushed = 0

        def on_translate_progress(done_count: int) -> None:
            """每完成一个批次回调一次：更新任务状态，并按句数阈值刷盘。

            刷盘频率由这里决定，pipeline.translate_segments 只管报告进度，
            不碰任何存储 —— 云端 worker 用同一个回调接法回写 Supabase。
            """
            nonlocal flushed
            progress = 20 + int((done_count / total_segments) * 75)
            update_job(
                job_id,
                stage="translating",
                progress=min(progress, 95),
                translated_count=done_count,
                segments=segments,
            )

            # 每积累 20 句刷一次盘，中断时也能保住大部分已翻译内容
            if done_count - flushed >= 20:
                write_result_file(job_id, segments)
                flushed = done_count

        pipeline.translate_segments(segments, on_progress=on_translate_progress)

        result_path = write_result_file(job_id, segments)

        update_job(
            job_id,
            status="success",
            stage="completed",
            progress=100,
            translated_count=len(segments),
            output_path=result_path,
        )
        print(f"[{job_id}] 5. Output saved: {result_path}")
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            stage="failed",
            error=str(exc),
        )
        # 失败时也把已经拿到的部分结果落盘，避免用户「什么都看不到」
        if segments:
            try:
                partial_path = write_result_file(job_id, segments)
                update_job(job_id, output_path=partial_path, total_segments=len(segments))
                print(f"[{job_id}] 已保留部分结果: {len(segments)} 段")
            except OSError as write_exc:
                print(f"[{job_id}] 部分结果落盘失败: {write_exc}")
        print(f"[{job_id}] Error processing file: {str(exc)}")


@router.post("/process-podcast")
async def process_podcast(
    file: UploadFile = File(...),
    source_language: str = Form("auto"),
):
    job_id = str(uuid.uuid4())
    filename = file.filename or "audio.mp3"
    _, file_ext = os.path.splitext(filename)
    input_path = os.path.join(INPUT_DIR, f"{job_id}{file_ext}")
    normalized_source_language = pipeline.normalize_source_language(source_language)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    create_job_record(job_id, filename, input_path, normalized_source_language)
    queue_processing_job(job_id, input_path, normalized_source_language)

    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "source_language": normalized_source_language,
    }


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    return build_progress_snapshot(job_id)


@router.get("/jobs/{job_id}/media")
async def get_job_media(job_id: str):
    media_path = resolve_job_media_path(job_id)
    if not media_path:
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(media_path)


@router.get("/health")
async def health_check():
    return {"status": "ok", "jobs": len(jobs)}


# 同时挂到 /api/* 与根路径：/api 是通用约定，根路径兼容既有前端调用。
app.include_router(router, prefix="/api")
app.include_router(router)

# 启动即回灌历史任务。没有这一步，进程重启后 GET /jobs/{id} 会返回 404，
# 前端轮询静默停摆，用户只能看到「一直卡在处理中」。
_restored_jobs = load_persisted_jobs()
print(f"[jobs] 已恢复 {_restored_jobs} 个历史任务（状态目录 {JOB_STATE_DIR}）")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

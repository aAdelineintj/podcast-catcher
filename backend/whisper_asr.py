"""本地 Whisper 转写：替换腾讯云「一句话识别」，对外保持与 podcast_asr 完全相同的契约。

对外只暴露 `transcribe_audio(path, asr, source_language, on_progress) -> [{start, end, text}]`，
start/end 为秒。因此 backend/main.py 只需换一行调用即可切换引擎。

为什么这样设计（依据 2026-09 在本项目语料上的实测结论）：

1. **不能「VAD 切段 → 每段单独喂 Whisper」。**
   openai-whisper 每次 `transcribe()` 都会把输入 pad/trim 到 30 秒 mel
   （`N_FRAMES = 3000`）。现有 VAD 在 874s 音频上按 merge_gap=400ms 切出约 190 段，
   每段 3 秒也要走完整的 30 秒 encoder 前向 → 等效计算量约 ×6.5，
   M1 上 base 从 ~4 分钟膨胀到 ~25 分钟。
   且短段上下文不足会让法语质量更差（podcast_asr.py 已实测 <1.0s 段失败率 33%）。

2. **不能直接用 Whisper 原生 segment。**
   原生 segment 粒度 5~30s、边界与句子不对齐，精听的高亮/跳转体验会崩坏。

3. 所以走第三条路：**整文件（分块）转写一次 + `word_timestamps=True`，
   再用「句末标点 / 词间静音 / 时长与字数上限」三重规则二次切句。**
   时间戳直接来自词级结果，不做任何文本↔时间的推断对齐。

4. **解码走 `podcast_asr.decode_to_pcm16k`（PyAV），不依赖 ffmpeg CLI。**
   `whisper.load_audio()` 会 shell 调用 ffmpeg，而本机 ffmpeg 装在
   `/opt/homebrew/bin`，不在非登录 shell 的 PATH 里，worker 由 launchd 拉起时必然失败。

5. **`condition_on_previous_text=False` 是必须的。**
   默认 True 时 Whisper 会在音乐/静音处整段重复输出（实测出现连续两行完全相同的文本）。

6. **分块而不是整文件一次跑。** 长音频整文件跑会让前端进度条几分钟不动，
   且峰值内存随音频长度线性增长。分块（默认 300s）后每块结束回调一次进度；
   切点会在 ±8s 内吸附到能量最低的 30ms 帧，避免把词从中间切断。
"""

from __future__ import annotations

import os
import threading

import numpy as np
from dotenv import load_dotenv

import podcast_asr

# 与 main.py 相同的加载顺序：先仓库根 .env，再当前工作目录 .env（都不覆盖已有值）。
#
# 必须放在本模块读取任何配置**之前**。原因：main.py 是「先 import 本模块、
# 后 load_dotenv」，如果这里不自己加载，下面所有 os.getenv 都会读到未初始化的
# 环境，把 WHISPER_MODEL=base 之类的设置静默退回默认值。
# load_dotenv 默认 override=False，重复调用是幂等的。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))
load_dotenv()

SAMPLE_RATE = 16000

# base（145MB，已缓存）| small（~461MB，法语明显更准）| medium | large-v3-turbo
DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "small")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

# 单句时长/字数上限：超过就切开，保证精听的定位粒度
MAX_SEGMENT_SEC = float(os.getenv("WHISPER_MAX_SEGMENT_SEC", "12"))
MAX_SEGMENT_CHARS = int(os.getenv("WHISPER_MAX_SEGMENT_CHARS", "180"))
# 词间静音超过该值即视为句子边界
WORD_GAP_SEC = float(os.getenv("WHISPER_WORD_GAP_SEC", "0.6"))
# 幻觉抑制：no_speech 概率高于该值的窗口直接丢弃
NO_SPEECH_THRESHOLD = float(os.getenv("WHISPER_NO_SPEECH_THRESHOLD", "0.6"))
# 分块长度与切点吸附搜索半径（秒）
CHUNK_SEC = float(os.getenv("WHISPER_CHUNK_SEC", "300"))
SNAP_SEARCH_SEC = float(os.getenv("WHISPER_SNAP_SEARCH_SEC", "8"))
# 混合语言内容（英语讲解 + 法语示范）开双遍裁决；单语种内容固定语种可省一半时间。
# 实测双遍只有 13/205 行有实质差异、却要多花 1.77× 时间，且 small 的逐窗自动检测
# 已能正确处理混排内容，故默认关闭。少数语种被主导语种带坏时再打开。
BILINGUAL = os.getenv("WHISPER_BILINGUAL", "0").strip().lower() not in (
    "0", "false", "no", "",
)

_model = None
_model_lock = threading.Lock()


def get_model():
    """进程内单例。首次调用会加载模型（base 约 1~3s，small 约 3~8s）。"""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                import torch
                import whisper

                threads = os.getenv("WHISPER_THREADS", "").strip()
                count = int(threads) if threads else min(8, os.cpu_count() or 4)
                torch.set_num_threads(max(1, count))
                _model = whisper.load_model(DEFAULT_MODEL, device=DEVICE)
    return _model


# --------------------------------------------------------------------------
# 解码与分块
# --------------------------------------------------------------------------

def _load_audio_f32(path: str) -> np.ndarray:
    """任意音频 -> 16kHz 单声道 float32（Whisper 期望的输入格式）。"""
    pcm = podcast_asr.decode_to_pcm16k(path)
    if not pcm:
        raise podcast_asr.ASRError("音频解码为空")
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _quiet_boundary(audio: np.ndarray, nominal: int, search_sec: float) -> int:
    """在 nominal ± search_sec 内找能量最低的 30ms 帧，返回其中心样本下标。"""
    frame = int(0.03 * SAMPLE_RATE)
    half = int(search_sec * SAMPLE_RATE)
    low = max(0, nominal - half)
    high = min(len(audio), nominal + half)
    if high - low < frame * 2:
        return nominal

    window = audio[low:high].astype(np.float64)
    count = len(window) // frame
    if count < 1:
        return nominal
    frames = window[: count * frame].reshape(count, frame)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    return low + int(energy.argmin()) * frame + frame // 2


def _chunk_ranges(audio: np.ndarray) -> list[tuple[int, int]]:
    """把整段音频切成 [(start_sample, end_sample)]，切点吸附到低能量处。"""
    total = len(audio)
    chunk = int(CHUNK_SEC * SAMPLE_RATE)
    if total <= chunk or chunk <= 0:
        return [(0, total)]

    bounds = [0]
    position = chunk
    while position < total:
        bounds.append(_quiet_boundary(audio, position, SNAP_SEARCH_SEC))
        position += chunk
    bounds.append(total)

    # 吸附后仍严格单调（chunk=300s 远大于 2*search=16s），这里只做一次兜底裁剪
    ranges: list[tuple[int, int]] = []
    for index in range(len(bounds) - 1):
        start, end = bounds[index], bounds[index + 1]
        if end > start:
            ranges.append((start, end))
    return ranges


# --------------------------------------------------------------------------
# 转写
# --------------------------------------------------------------------------

def _transcribe_chunk(model, audio_slice: np.ndarray, language, offset_sec: float):
    """转写一块音频，产出 (绝对起始秒, 绝对结束秒, 词文本)。"""
    result = model.transcribe(
        audio_slice,
        language=language,
        task="transcribe",
        word_timestamps=True,
        condition_on_previous_text=False,   # 必须：否则音乐/静音处整段重复
        no_speech_threshold=NO_SPEECH_THRESHOLD,
        logprob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        fp16=(DEVICE != "cpu"),
        # 注意：这里必须传 None 而不是 False。
        # whisper/transcribe.py 里是 `disable=verbose is not False` ——
        # 传 False 反而会**打开** tqdm 进度条，并打印 "Detected language:"；
        # 传 None 才是完全静默（不打印文本、不打印语种、不显示进度条）。
        # 进度由本模块按分块自行上报，不需要 whisper 自己再画一条。
        verbose=None,
    )
    yield from _words_of(result, offset_sec)


def _words_of(result: dict, offset_sec: float = 0.0):
    """产出 (start, end, text)，时间为绝对秒。

    词级时间戳不可用时（极少见）退回 segment 级，避免整轨产出为空。
    """
    found = False
    for segment in result.get("segments") or []:
        for word in segment.get("words") or []:
            text = (word.get("word") or "").strip()
            if text:
                found = True
                yield offset_sec + float(word["start"]), offset_sec + float(word["end"]), text

    if found:
        return

    for segment in result.get("segments") or []:
        text = (segment.get("text") or "").strip()
        if text:
            yield offset_sec + float(segment["start"]), offset_sec + float(segment["end"]), text


# --------------------------------------------------------------------------
# 词级 -> 句级
# --------------------------------------------------------------------------

def _flush(buffer: list[str], start, end) -> dict | None:
    if not buffer or start is None or end is None:
        return None
    text = podcast_asr.join_words(buffer)
    if not text:
        return None
    return {"start": float(start), "end": float(end), "text": text}


def _split_to_sentences(words) -> list[dict]:
    """三个断点：句末标点 / 词间静音 >= WORD_GAP_SEC / 时长或字数到上限。"""
    rows: list[dict] = []
    buffer: list[str] = []
    buffer_start = None
    last_end = None

    for start, end, text in words:
        if buffer and last_end is not None and (start - last_end) >= WORD_GAP_SEC:
            row = _flush(buffer, buffer_start, last_end)
            if row:
                rows.append(row)
            buffer, buffer_start = [], None

        if buffer_start is None:
            buffer_start = start
        buffer.append(text)
        last_end = end

        joined = podcast_asr.join_words(buffer)
        if (
            podcast_asr.SENT_END_RE.search(joined)
            or (last_end - buffer_start) >= MAX_SEGMENT_SEC
            or len(joined) >= MAX_SEGMENT_CHARS
        ):
            row = _flush(buffer, buffer_start, last_end)
            if row:
                rows.append(row)
            buffer, buffer_start = [], None

    tail = _flush(buffer, buffer_start, last_end)
    if tail:
        rows.append(tail)
    return rows


def _dedupe_repeats(rows: list[dict]) -> list[dict]:
    """Whisper 在音乐/静音处会整行重复输出，这里合并相邻的完全相同行。"""
    out: list[dict] = []
    for row in rows:
        if out and row["text"] == out[-1]["text"]:
            out[-1]["end"] = row["end"]
            continue
        out.append(dict(row))
    return out


def _arbitrate(primary: list[dict], alt: list[dict]) -> list[dict]:
    """逐行裁决：取时间重叠最多的 alt 行，语种可判读强度更高者胜出。

    本项目语料是「英语讲解 + 法语示范」混排。单遍转写时，占主导的语种
    会把少数语种窗口硬转成自己的语言（实测 base 把法语压成英语音译）。
    双遍 + 逐行裁决等价于腾讯方案里的双引擎裁决。
    """
    out: list[dict] = []
    for row in primary:
        middle = (row["start"] + row["end"]) / 2.0
        best = None
        best_overlap = 0.0
        for candidate in alt:
            if candidate["start"] - 1.0 <= middle <= candidate["end"] + 1.0:
                overlap = min(candidate["end"], row["end"]) - max(candidate["start"], row["start"])
                if overlap > best_overlap:
                    best_overlap, best = overlap, candidate

        text = row["text"]
        if best is not None and (
            podcast_asr._language_confidence(best["text"])
            > podcast_asr._language_confidence(text) + 1
        ):
            text = best["text"]
        out.append({"start": row["start"], "end": row["end"], "text": text})
    return out


# --------------------------------------------------------------------------
# 对外入口（与 podcast_asr.transcribe_audio 契约一致）
# --------------------------------------------------------------------------

def transcribe_audio(
    path: str,
    asr=None,
    source_language: str = "auto",
    on_progress=None,
    **_ignored,
) -> list[dict]:
    """返回 [{start, end, text}]，start/end 为秒。"""
    model = asr if asr is not None else get_model()
    audio = _load_audio_f32(path)
    ranges = _chunk_ranges(audio)
    if not ranges:
        raise podcast_asr.ASRError("音频解码后长度为 0")

    # 固定语种只跑一遍；auto + 双语裁决跑两遍，因此进度总量按遍数预分配
    language = source_language if source_language in ("fr", "en") else None
    passes = 2 if (language is None and BILINGUAL) else 1
    total_units = max(1, len(ranges) * passes)
    units = 0

    def run(forced_language) -> list[dict]:
        nonlocal units
        words: list[tuple[float, float, str]] = []
        for start_sample, end_sample in ranges:
            offset = start_sample / SAMPLE_RATE
            words.extend(
                _transcribe_chunk(model, audio[start_sample:end_sample], forced_language, offset)
            )
            units += 1
            if on_progress:
                on_progress(units, total_units)
        return _split_to_sentences(words)

    rows = run(language)

    if language is None and BILINGUAL and rows:
        sample = " ".join(row["text"] for row in rows[:30])
        other = "en" if podcast_asr.detect_text_language(sample) == "fr" else "fr"
        rows = _arbitrate(rows, run(other))

    rows = _dedupe_repeats(rows)
    if not rows:
        raise podcast_asr.ASRError("Whisper 未产出任何文本段（音频可能全为静音）")
    return rows

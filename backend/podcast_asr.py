"""本地 VAD 切分 + 腾讯云「一句话识别」句级时间戳流水线。

设计要点（依据 2026-09 在真实语料上的实测结论）：

1. 腾讯云「录音文件识别」对法语引擎（16k_fr / 16k_multi_lang）只允许
   ResTextFormat=0，**拿不到句级时间戳**；而本项目语料以法语为主。
2. 「一句话识别」SentenceRecognition 是**同步**接口，引擎列表里明确含 16k_fr，
   且支持 ≤60s / base64 ≤3MB 的音频 —— 正好匹配一个 VAD 语音段。
3. 架构实测对比（3 份干净语料、429 句，基准为历史 Whisper 字幕的真实 start）：
   - 整文件 1 次调用 + 文本对齐：中位 0.47~1.66s，p90 2.4~4.8s，≤1s 仅 40~62%
   - 每个 VAD 段 1 次调用：中位 0.29~0.59s，p90 0.77~1.36s，≤1s 达 84~94%
   故采用后者：时间戳直接来自音频结构，不做任何文本↔时间的推断对齐。
4. 解码不要用 av.AudioResampler —— 实测该版本输出样本数被系统性放大
   （446.10s 音频被解成 538.19s，1.206x），会导致所有时间戳整体错位。
   这里原生解码后用 numpy 线性重采样，时长与容器声明严格一致。
5. VAD 的 merge_gap_ms 是成本的第一大杠杆，且免费。同一份 874s 语料实测：
       gap=250ms -> 297 段，<1.5s 碎片占 34%，最长段 10.7s
       gap=400ms -> 189 段，<1.5s 碎片占 22%，最长段 23.9s
       gap=600ms ->  99 段，但最长段 58.5s，**超出 55s 上限，尾部会被砍掉**
       gap=900ms ->  37 段，最长段 163.9s，5 段被砍
   故默认取 400ms：段数降 36%，且不产生任何超限段。
6. 引擎裁决的准确率实测已接近上限，不要再往打分函数里加东西。
   人工判读 60 段作真值（fr 32 / en 21 / 判不出 7）：
       16k_fr 单独跑正确 53%，16k_en 单独跑 35%，完美裁决上限 88%
       现行「先 fr 后 en + 阈值 5」已达 85%
   真正能省的是阈值策略（短段凑不满 5 个提示词命中，白白多跑一次），见
   _decided_score()。曾试过加「伪词惩罚 + 成词加分」的增强打分器，反而掉到
   77~79% —— 它会让含乱码的文本凑够分而提前 break。故弃用。
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed

import av
import numpy as np
import webrtcvad

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

# 一句话识别单次限制
MAX_SEGMENT_SEC = 55.0
MAX_BASE64_BYTES = 3 * 1024 * 1024

DEFAULT_ENGINE_BY_LANG = {
    "fr": "16k_fr",
    "en": "16k_en",
    "zh": "16k_zh",
    "es": "16k_es",
    "de": "16k_de",
    "ja": "16k_ja",
    "ko": "16k_ko",
}

_FRENCH_HINTS = {
    "bonjour", "merci", "maintenant", "nous", "vous", "avec", "pour", "dans",
    "une", "des", "les", "est", "etre", "suis", "sommes", "avez", "fait",
    "tres", "plus", "bien", "mais", "parce", "comme", "quand", "quel",
    "quelle", "ce", "cet", "cette", "ces", "qui", "que", "quoi", "dont",
    "mon", "ton", "son", "notre", "votre", "leur", "oui", "non", "pas",
    "je", "tu", "il", "elle", "on", "ils", "elles", "au", "aux", "du", "aujourd",
}
_ENGLISH_HINTS = {
    "the", "and", "is", "are", "was", "were", "have", "has", "do", "does",
    "did", "with", "for", "from", "that", "this", "these", "those", "what",
    "when", "where", "why", "how", "hello", "thanks", "thank", "please",
    "now", "more", "little", "talk", "about", "your", "my", "our", "their",
    "not", "yes", "no", "welcome", "learn", "podcast", "you", "we", "they",
    "will", "would", "can", "could", "there", "here", "been", "just",
}

SENT_END_RE = re.compile(r"[.!?…。！？]['\"”’)\]]*\s*$")
_NO_SPACE_BEFORE = set(".,!?;:%)]}»…’”")
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------

def probe_duration(path: str) -> float | None:
    """只读容器头拿到音频总时长（秒）。失败返回 None。

    **为什么需要它**：Whisper 的最后一个 segment 通常在尾部静音之前就结束了。
    一集 25 秒的音频，最后一句可能只到 23.8 秒 —— 于是列表显示 0:23、
    播放器显示 0:25，用户一眼就能看出不一致。

    这里读的是容器声明的时长，与播放器口径一致。
    成本极低：只解析头部，不解码任何音频帧。
    """
    try:
        container = av.open(path)
    except Exception:
        return None

    try:
        # av 的 container.duration 单位是微秒（AV_TIME_BASE = 1_000_000）
        if container.duration is not None:
            return container.duration / 1_000_000

        # 有些容器没有全局时长（比如某些流式 mp3），退到流级别
        for stream in container.streams:
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
    except Exception:
        return None
    finally:
        container.close()

    return None


def decode_to_pcm16k(path: str, target_rate: int = RATE) -> bytes:
    """任意音频 -> 16kHz 单声道 int16 PCM。时长与容器声明严格一致。"""
    container = av.open(path)
    try:
        stream = container.streams.audio[0]
        native_rate = None
        chunks = []
        for frame in container.decode(stream):
            if native_rate is None:
                native_rate = frame.sample_rate
            arr = frame.to_ndarray()
            if frame.format.is_planar:
                arr = arr.astype(np.float32)
                arr = arr.mean(axis=0) if arr.shape[0] > 1 else arr.reshape(-1)
            else:
                arr = arr.astype(np.float32).reshape(-1)
                channels = frame.layout.nb_channels
                if channels > 1:
                    arr = arr.reshape(-1, channels).mean(axis=1)
            chunks.append(arr)
    finally:
        container.close()

    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    if not len(audio):
        return b""

    if native_rate and native_rate != target_rate:
        n_out = int(round(len(audio) / native_rate * target_rate))
        audio = np.interp(
            np.linspace(0.0, len(audio) - 1.0, n_out),
            np.arange(len(audio), dtype=np.float64),
            audio,
        ).astype(np.float32)

    peak = float(np.abs(audio).max())
    if peak <= 1.5:                       # float 归一化到 int16 量级
        audio = audio * 32767.0
    return np.clip(audio, -32768, 32767).astype(np.int16).tobytes()


# --------------------------------------------------------------------------
# VAD
# --------------------------------------------------------------------------

def vad_segments(
    pcm: bytes,
    aggressiveness: int = 3,
    min_speech_ms: int = 250,
    merge_gap_ms: int = 250,
) -> list[tuple[float, float]]:
    """返回 [(start_s, end_s)]。默认参数在实测中给出 段/句≈1.0、中位误差 0.3~0.7s。"""
    if len(pcm) < FRAME_BYTES:
        return []

    vad = webrtcvad.Vad(int(aggressiveness))
    n_frames = len(pcm) // FRAME_BYTES
    flags = [
        vad.is_speech(pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES], RATE)
        for i in range(n_frames)
    ]

    raw: list[list[int]] = []
    i = 0
    while i < n_frames:
        if flags[i]:
            j = i
            while j < n_frames and flags[j]:
                j += 1
            raw.append([i, j])
            i = j
        else:
            i += 1

    gap_frames = max(1, int(merge_gap_ms) // FRAME_MS)
    merged: list[list[int]] = []
    for seg in raw:
        if merged and seg[0] - merged[-1][1] <= gap_frames:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))

    min_frames = max(1, int(min_speech_ms) // FRAME_MS)
    return [
        (s * FRAME_MS / 1000.0, e * FRAME_MS / 1000.0)
        for s, e in merged
        if e - s >= min_frames
    ]


def _split_long_segments(
    segments: list[tuple[float, float]], limit: float = MAX_SEGMENT_SEC
) -> list[tuple[float, float]]:
    """把超过一句话识别上限的段等分切开。

    为什么必须切而不是直接截断：_recognize_one 里有一句
    `if end_s - start_s > MAX_SEGMENT_SEC: end_s = start_s + MAX_SEGMENT_SEC`，
    它会让超长段的尾部音频被**静默丢弃**，一个字都不报错。
    merge_gap_ms 调大之后很容易产出 60~200s 的长段（实测 gap=900ms 时最长 163.9s），
    这时静默丢音会变成系统性问题，所以在这里显式切开。
    """
    out: list[tuple[float, float]] = []
    for start, end in segments:
        span = end - start
        if span <= limit:
            out.append((start, end))
            continue
        parts = int(math.ceil(span / limit))
        step = span / parts
        for k in range(parts):
            out.append((start + k * step, start + (k + 1) * step))
    return out


def _absorb_short_segments(
    segments: list[tuple[float, float]],
    min_len: float = 1.0,
    max_span: float = MAX_SEGMENT_SEC - 5.0,
) -> list[tuple[float, float]]:
    """把短于 min_len 的碎片段并进「间隙更小」的那一侧邻居。

    为什么：碎片段（<1.0s）上下文不足，两个引擎都只能吐残渣。
    人工真值 60 段的失败率与段长严格相关：

        段长 <1.0s   -> 失败率 33%（7 个失败里占 4 个，却只占 20% 的段）
        段长 1.0~1.5s -> 10%
        段长 1.5~2.5s -> 6%
        段长 >=4.0s   -> 0%

    定向实验（16 个碎片段，各测「单独跑」与「与邻居合并跑」）：
        9 个明显变好 —— 例：[143.97-144.39] 单独跑 fr 返回**空字符串**，
                         合并后得到完整正确的 "Jacques aime plaisanter."；
                         [646.05-646.77] "Le lundi." 合并后补全为
                         "Elle se'ntraîne trois fois par semaine, le lundi."
        1 个变差     —— [220.29-222.60] 原本正确的 "Multiplier." 被邻居的残渣带坏
        其余持平
    净收益明显为正，且合并后段数下降（189 -> 176），调用数同步下降。

    max_span 防止合并出超过一句话识别 55s 上限的段（默认留 5s 余量）。
    """
    segs: list[list[float]] = [[s, e] for s, e in segments]
    changed = True
    while changed:
        changed = False
        for i, (s, e) in enumerate(segs):
            if e - s >= min_len or len(segs) == 1:
                continue
            prev_gap = (s - segs[i - 1][1]) if i > 0 else float("inf")
            next_gap = (segs[i + 1][0] - e) if i < len(segs) - 1 else float("inf")
            if prev_gap <= next_gap and i > 0:
                j = i - 1
            elif i < len(segs) - 1:
                j = i + 1
            else:
                j = i - 1
            if j < 0:
                continue
            start = min(segs[i][0], segs[j][0])
            end = max(segs[i][1], segs[j][1])
            if end - start > max_span:
                continue
            segs[j] = [start, end]
            segs.pop(i)
            changed = True
            break
    return [(s, e) for s, e in segs]


def _pcm_slice_wav(pcm: bytes, start_s: float, end_s: float, pad_s: float = 0.12) -> tuple[bytes, float]:
    """切出一段并封装为 WAV。返回 (wav_bytes, 该 wav 在原始音频中的起始秒)。"""
    begin = max(0.0, start_s - pad_s)
    finish = end_s + pad_s
    a = int(begin * RATE) * 2
    b = min(len(pcm), int(finish * RATE) * 2)
    data = pcm[a:b]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(data)
    return buf.getvalue(), begin


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------

def detect_text_language(text: str) -> str:
    """粗判 fr / en / zh / auto。用于 auto 模式下的引擎选择。"""
    if not text or not text.strip():
        return "auto"
    if len(_CJK_RE.findall(text)) >= 2:
        return "zh"
    if re.search(r"[àâçéèêëîïôùûüÿœæ]", text.lower()):
        return "fr"

    tokens = re.findall(r"[a-zA-ZÀ-ÿ']+", text.lower())
    if not tokens:
        return "auto"
    fr = sum(1 for t in tokens if t.strip("'") in _FRENCH_HINTS)
    en = sum(1 for t in tokens if t.strip("'") in _ENGLISH_HINTS)
    if fr == 0 and en == 0:
        return "auto"
    return "fr" if fr >= en else "en"


_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ']+")
_ACCENT_RE = re.compile(r"[àâäçéèêëîïôöùûüÿœæ]")
_CJK_ANY_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")


def _language_confidence(text: str) -> int:
    """文本的「语种可判读强度」：0 表示读不出任何语言特征（多为被错误引擎硬转的乱码）。

    auto 模式靠它裁决用哪个引擎的结果。评分 = 提示词命中数 + 重音字符加分。
    重音只加 2 分而不是直接判定：实测法语模型转英语时会蹦出零星的 à/é，
    给高权重会让整段英文旁白被误留成法语乱码（本项目语料就是英法混排）。

    注意：不要再往这里加「伪词惩罚 / 成词加分」之类的特征。实测过，
    加入后准确率从 85% 掉到 77~79%（人工真值 60 段）—— 含乱码的文本
    反而更容易凑够分而提前 break。判别力不够应该靠调阈值，不是靠堆特征。
    """
    if not text or not text.strip():
        return 0
    if _CJK_ANY_RE.findall(text):
        return 20
    tokens = _TOKEN_RE.findall(text.lower())
    fr = sum(1 for t in tokens if t.strip("'") in _FRENCH_HINTS)
    en = sum(1 for t in tokens if t.strip("'") in _ENGLISH_HINTS)
    score = fr + en
    if _ACCENT_RE.search(text.lower()):
        score += 2
    return score


# 置信度达到阈值即可认定语种明确，auto 模式无需再试其它引擎（省一半调用）。
# 阈值按段长自适应：短段（2~4 个词）根本凑不出 5 个提示词命中，用 5 会永远
# 白跑第二个引擎。实测「短<6→3 / 中<14→4 / 长→5」在人工真值上保持 85% 准确率
# 不变，同时把调用数从 1.93 次/段降到 1.80 次/段，且没有任何一段质量变差。
_ENGINE_DECIDED_SCORE_SHORT = 3      # 词数 < 6
_ENGINE_DECIDED_SCORE_MID = 4        # 词数 < 14
_ENGINE_DECIDED_SCORE = 5            # 长段


def _decided_score(text: str) -> int:
    """该段文本达到多少分就可以停止尝试其它引擎。"""
    n = len(_TOKEN_RE.findall(text))
    if n < 6:
        return _ENGINE_DECIDED_SCORE_SHORT
    if n < 14:
        return _ENGINE_DECIDED_SCORE_MID
    return _ENGINE_DECIDED_SCORE


def join_words(words: list[str]) -> str:
    out = ""
    for w in words:
        if not w:
            continue
        if not out:
            out = w
        elif w[0] in _NO_SPACE_BEFORE or w.startswith("'"):
            out += w
        elif _CJK_RE.search(w) or _CJK_RE.search(out[-1:]):
            out += w
        else:
            out += " " + w
    return out.strip()


# --------------------------------------------------------------------------
# 腾讯云一句话识别
# --------------------------------------------------------------------------

class ASRError(RuntimeError):
    pass


class TencentSentenceASR:
    """线程安全的 SentenceRecognition 封装（同步、无轮询）。"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str = "ap-shanghai",
        max_workers: int = 16,
        max_retries: int = 3,
    ):
        if not secret_id or not secret_key:
            raise ASRError("缺少腾讯云 TENCENT_SECRET_ID / TENCENT_SECRET_KEY")

        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.asr.v20190614 import asr_client

        cred = credential.Credential(secret_id, secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "asr.tencentcloudapi.com"
        http_profile.reqTimeout = 30
        client_profile = ClientProfile(httpProfile=http_profile)

        self._client = asr_client.AsrClient(cred, region, client_profile)
        self._models = __import__(
            "tencentcloud.asr.v20190614.models", fromlist=["models"]
        )
        self.max_workers = max(1, int(max_workers))
        self.max_retries = max(1, int(max_retries))

    def recognize(
        self, wav_bytes: bytes, engine: str, word_info: int = 2
    ) -> tuple[str, list[tuple[str, float, float]]]:
        """返回 (文本, [(词, 起始秒, 结束秒)])。词级时间戳不可用时列表为空。"""
        if len(wav_bytes) * 4 // 3 > MAX_BASE64_BYTES:
            raise ASRError("音频段过大，超过一句话识别 3MB 限制")

        payload = {
            "ProjectId": 0,
            "SubServiceType": 2,
            "EngSerViceType": engine,
            "SourceType": 1,
            "VoiceFormat": "wav",
            "Data": base64.b64encode(wav_bytes).decode("ascii"),
            "DataLen": len(wav_bytes),
            "WordInfo": int(word_info),
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                request = self._models.SentenceRecognitionRequest()
                request.from_json_string(json.dumps(payload))
                response = self._client.SentenceRecognition(request)
                text = (response.Result or "").strip()
                words: list[tuple[str, float, float]] = []
                for item in (response.WordList or []):
                    # SDK 的 SentenceWord 模型字段是 Word / StartTime / EndTime，
                    # 单位毫秒（实测：末词 EndTime 与段时长吻合）。不存在 OffsetStartMs。
                    words.append(
                        (
                            item.Word or "",
                            float(item.StartTime or 0) / 1000.0,
                            float(item.EndTime or 0) / 1000.0,
                        )
                    )
                return text, words
            except Exception as exc:                       # noqa: BLE001
                last_error = exc
                message = str(exc)
                if "UserHasNoAmount" in message or "UserNotRegistered" in message:
                    raise ASRError(message) from exc
                if attempt < self.max_retries - 1:
                    time.sleep(0.4 * (2 ** attempt))
        raise ASRError(str(last_error))


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------

def _recognize_one(asr, pcm, seg, source_language, word_info):
    start_s, end_s = seg
    if end_s - start_s > MAX_SEGMENT_SEC:
        end_s = start_s + MAX_SEGMENT_SEC
    wav_bytes, offset = _pcm_slice_wav(pcm, start_s, end_s)

    if source_language in DEFAULT_ENGINE_BY_LANG:
        engines = [DEFAULT_ENGINE_BY_LANG[source_language]]
    else:                                   # auto：先法语（本项目主语种），判不准再换英语
        engines = ["16k_fr", "16k_en"]

    best: tuple[int, str, list] | None = None
    last_error = ""
    for engine in engines:
        try:
            candidate, candidate_words = asr.recognize(wav_bytes, engine, word_info=word_info)
        except ASRError as exc:
            # 记录而不是丢弃：鉴权失败 / 服务未开通这类系统性错误必须能浮到上层，
            # 否则整轨识别失败会表现为「0 段且无报错」，极难排查。
            last_error = f"[{engine}] {exc}"
            continue
        if not candidate:
            continue
        confidence = _language_confidence(candidate)
        if best is None or confidence > best[0]:
            best = (confidence, candidate, candidate_words)
        if confidence >= _decided_score(candidate):
            # 语种已明确，无需再试其它引擎（单语种内容因此只调用 1 次）
            break

    if best is not None:
        text, words = best[1], best[2]
    else:
        text, words = "", []

    return {"start": start_s, "end": end_s, "offset": offset, "text": text,
            "words": words, "error": last_error}


def transcribe_audio(
    path: str,
    asr: TencentSentenceASR,
    source_language: str = "auto",
    word_info: int = 2,
    vad_aggressiveness: int | None = None,
    vad_min_speech_ms: int | None = None,
    vad_merge_gap_ms: int | None = None,
    on_progress=None,
) -> list[dict]:
    """返回 [{start, end, text}]，start/end 为秒。"""
    pcm = decode_to_pcm16k(path)
    if not pcm:
        raise ASRError("音频解码为空")

    segments = vad_segments(
        pcm,
        aggressiveness=int(vad_aggressiveness if vad_aggressiveness is not None
                           else os.getenv("VAD_AGGRESSIVENESS", "3")),
        min_speech_ms=int(vad_min_speech_ms if vad_min_speech_ms is not None
                          else os.getenv("VAD_MIN_SPEECH_MS", "250")),
        merge_gap_ms=int(vad_merge_gap_ms if vad_merge_gap_ms is not None
                         else os.getenv("VAD_MERGE_GAP_MS", "400")),
    )
    if not segments:
        raise ASRError("VAD 未检测到任何语音段")

    # 先把碎片段并进邻居（上下文不足是质量损失的主因），再切开超长段。
    # 顺序不能反：_absorb_short_segments 的 max_span 已保证不超 55s。
    segments = _absorb_short_segments(
        segments,
        min_len=float(os.getenv("VAD_MIN_SEGMENT_MS", "1000")) / 1000.0,
    )
    # 超长段先切开，否则 _recognize_one 会静默截断、丢掉尾部音频
    segments = _split_long_segments(segments)

    results: list[dict | None] = [None] * len(segments)
    done = 0
    with ThreadPoolExecutor(max_workers=asr.max_workers) as pool:
        futures = {
            pool.submit(_recognize_one, asr, pcm, seg, source_language, word_info): index
            for index, seg in enumerate(segments)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:                # noqa: BLE001
                s, e = segments[index]
                results[index] = {"start": s, "end": e, "offset": s, "text": "",
                                  "words": [], "error": f"{type(exc).__name__}: {exc}"}
            done += 1
            if on_progress:
                on_progress(done, len(segments))

    rows: list[dict] = []
    for item in results:
        if not item:
            continue
        rows.extend(_rows_from_result(item))

    # 全部段落都失败时，把真实错误抛出去，而不是静默返回 0 段。
    if not rows:
        errors = [item.get("error") for item in results if item and item.get("error")]
        if errors:
            raise ASRError(
                f"全部 {len(errors)} 个语音段识别失败，首个错误：{errors[0]}"
            )

    if os.getenv("VAD_MERGE_BY_PUNCT", "0") not in ("0", "false", "False"):
        rows = _merge_unfinished(rows)
    return rows


def _rows_from_result(item: dict) -> list[dict]:
    text = (item.get("text") or "").strip()
    if not text:
        return []

    words = item.get("words") or []
    if words:
        offset = item["offset"]
        word_start = offset + words[0][1]
        # 只有词表里确实带标点时才按标点切句；否则整段作为一行。
        # （若 ASR 不返回标点，强行切句会退化成过度合并，反而破坏逐句定位。）
        has_punct = any(
            w and w[0] in ".!?…。！？" for w, _, _ in words
        )
        if has_punct:
            rows: list[dict] = []
            buffer: list[str] = []
            buffer_start: float | None = None
            last_end = offset
            for word, ws, we in words:
                if buffer_start is None:
                    buffer_start = offset + ws
                buffer.append(word)
                last_end = offset + we
                if SENT_END_RE.search(join_words(buffer)):
                    rows.append({
                        "start": buffer_start,
                        "end": last_end,
                        "text": join_words(buffer),
                    })
                    buffer, buffer_start = [], None
            if buffer:
                rows.append({
                    "start": buffer_start if buffer_start is not None else word_start,
                    "end": last_end,
                    "text": join_words(buffer),
                })
            if rows:
                return rows

        # 词级时间戳可用但无标点：整段一行，起点用首词时间（比段起点更准）
        return [{
            "start": word_start,
            "end": offset + words[-1][2],
            "text": text,
        }]

    return [{"start": item["start"], "end": item["end"], "text": text}]


def _merge_unfinished(rows: list[dict], max_merge: int = 3, max_span_s: float = 10.0) -> list[dict]:
    """把缺少句末标点的行并入下一行，避免 VAD 过切把一句话拆成多行。"""
    merged: list[dict] = []
    pending: dict | None = None

    for row in rows:
        if pending is None:
            pending = {**row, "_merges": 0}
            continue
        span = row["end"] - pending["start"]
        if (not SENT_END_RE.search(pending["text"])
                and pending["_merges"] < max_merge
                and span <= max_span_s):
            pending["text"] = f"{pending['text']} {row['text']}".strip()
            pending["end"] = row["end"]
            pending["_merges"] += 1
            continue
        merged.append(pending)
        pending = {**row, "_merges": 0}

    if pending is not None:
        merged.append(pending)

    for row in merged:
        row.pop("_merges", None)
    return merged

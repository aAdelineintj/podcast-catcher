"""纯函数管线：语种判定、时间戳、百度翻译的批量与并发。

**为什么单独成文件**：这套口径有两条使用方——

1. `main.py`（本地 FastAPI 后端，阶段一/本地开发）
2. `worker/worker.py`（云端任务执行器，阶段二）

两边必须产出**逐字节一致**的字幕。把它们放在同一个模块里，
是唯一能保证「同一集音频在本地跑和线上跑结果相同」的做法——
否则切句规则、语种分组、翻译降级策略只要有一处漂移，
同一份音频会得到两套字幕，而这类问题在联调时极难发现。

本模块**不依赖 FastAPI、不依赖 torch**，只依赖 `requests` 与标准库。
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import threading
import time

import requests
from dotenv import load_dotenv

# 仓库根目录（backend/ 的上一级）。配置以仓库根为基准，避免受启动工作目录影响。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))
load_dotenv()

# ── 百度翻译 ──────────────────────────────────────────────────────────
BAIDU_APP_ID = os.getenv("BAIDU_APP_ID") or os.getenv("APP_ID")
BAIDU_SECRET_KEY = os.getenv("BAIDU_SECRET_KEY") or os.getenv("SECRET_KEY")

# 标准版 QPS=1，高级版（个人认证后）QPS=10，尊享版 QPS=100。
# 默认按高级版配置；若账号仍是标准版，把 TRANSLATE_QPS 调成 1。
TRANSLATE_QPS = float(os.getenv("TRANSLATE_QPS", "10"))
TRANSLATE_MAX_WORKERS = int(os.getenv("TRANSLATE_MAX_WORKERS", "6"))
# 单次请求累计字符上限。官方建议 2000 字符以内，认证后上限 6000；
# 这里取 1500 留出余量，兼顾批量效率与单批失败时的重试代价。
TRANSLATE_BATCH_CHARS = int(os.getenv("TRANSLATE_BATCH_CHARS", "1500"))

# 时间戳小数位：0 = "M:SS"（与历史行为一致）；1 = "M:SS.s"（更高精度）
TIMESTAMP_DECIMALS = int(os.getenv("TIMESTAMP_DECIMALS", "0"))

FRENCH_HINTS = {
    "bonjour", "merci", "maintenant", "nous", "vous", "avec", "pour", "dans",
    "une", "des", "les", "est", "etre", "suis", "sommes", "avez", "fait",
    "tres", "plus", "bien", "mais", "parce", "comme", "quand", "quel",
    "quelle", "ce", "cet", "cette", "ces", "qui", "que", "quoi", "dont",
    "mon", "ton", "son", "notre", "votre", "leur", "oui", "non", "pas",
}

ENGLISH_HINTS = {
    "the", "and", "is", "are", "was", "were", "have", "has", "do", "does",
    "did", "with", "for", "from", "that", "this", "these", "those", "what",
    "when", "where", "why", "how", "hello", "thanks", "thank", "please",
    "now", "more", "little", "talk", "about", "your", "my", "our", "their",
    "not", "yes", "no", "welcome", "learn", "podcast",
}

SOURCE_LANGUAGE_ALIASES = {
    "auto": "auto",
    "detect": "auto",
    "detected": "auto",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "法语": "fr",
    "en": "en",
    "eng": "en",
    "english": "en",
    "英语": "en",
}

BAIDU_LANGUAGE_BY_SOURCE = {
    "auto": "auto",
    "fr": "fra",
    "en": "en",
}


def normalize_source_language(value: str | None) -> str:
    normalized = (value or "auto").strip().lower()
    return SOURCE_LANGUAGE_ALIASES.get(normalized, "auto")


def baidu_language_for_source(source_language: str) -> str:
    return BAIDU_LANGUAGE_BY_SOURCE.get(normalize_source_language(source_language), "auto")


# ---------------------------------------------------------------- 翻译限速与批量
#
# 为什么需要限速：百度翻译按 QPS 计费，超出直接返回 54003「访问频率受限」。
# 单纯堆线程池并不能保证 QPS 达标——每个请求耗时约 200ms 时，8 个线程的
# 瞬时吞吐可达 40 QPS，远超高级版的 10 QPS 上限。所以这里用「按固定间隔
# 发放调用许可」的方式精确控制请求发起速率，与线程池配合使用。
_translate_rate_lock = threading.Lock()
_translate_next_slot = 0.0


def acquire_translate_slot() -> None:
    """按 TRANSLATE_QPS 限制请求发起速率。

    在锁内只做时间计算与占位，实际 sleep 放在锁外，
    这样多个线程可以各自排队到错开的时隙，而不是串行阻塞在锁上。
    """
    global _translate_next_slot
    interval = 1.0 / max(TRANSLATE_QPS, 0.1)

    with _translate_rate_lock:
        now = time.monotonic()
        if _translate_next_slot < now:
            _translate_next_slot = now
        wait = _translate_next_slot - now
        _translate_next_slot += interval

    if wait > 0:
        time.sleep(wait)


def baidu_translate(text, from_lang="auto", to_lang="zh"):
    if not BAIDU_APP_ID or not BAIDU_SECRET_KEY:
        return "Translation Error: API keys missing"

    url = "http://api.fanyi.baidu.com/api/trans/vip/translate"
    salt = str(random.randint(32768, 65536))
    sign_str = BAIDU_APP_ID + text + salt + BAIDU_SECRET_KEY
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    params = {
        "q": text,
        "from": from_lang,
        "to": to_lang,
        "appid": BAIDU_APP_ID,
        "salt": salt,
        "sign": sign,
    }

    try:
        acquire_translate_slot()
        response = requests.get(url, params=params, timeout=10)
        result = response.json()
        if "trans_result" in result:
            return result["trans_result"][0]["dst"]
        return f"Error: {result.get('error_msg', 'Unknown error')}"
    except Exception as exc:
        return f"Network Error: {str(exc)}"


def build_translate_batches(
    items: list[tuple[int, str]], max_chars: int
) -> list[list[tuple[int, str]]]:
    """把 (索引, 文本) 按累计字符数分组，每组不超过 max_chars。

    单条文本本身超过 max_chars 时独占一组，交给接口自行处理。
    """
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0

    for index, text in items:
        length = len(text) + 1  # +1 为换行分隔符
        if current and current_chars + length > max_chars:
            batches.append(current)
            current, current_chars = [], 0
        current.append((index, text))
        current_chars += length

    if current:
        batches.append(current)
    return batches


def baidu_translate_batch(texts: list[str], from_lang: str, to_lang: str = "zh") -> list[str]:
    """一次请求翻译多段文本。

    百度翻译支持在 q 字段中用换行符分隔多段文本，返回的 trans_result
    数组按输入顺序逐段对应。这比逐句请求快一到两个数量级。

    返回长度与 texts 一致的译文列表。条数对不上或接口报错时抛异常，
    由调用方降级为逐句翻译——绝不静默错位，否则译文会和字幕张冠李戴。
    """
    if not BAIDU_APP_ID or not BAIDU_SECRET_KEY:
        raise RuntimeError("百度翻译密钥缺失")

    # 文本内部的换行会破坏「一段一行」的对应关系，先压成空格
    safe_texts = [re.sub(r"[\r\n]+", " ", text) for text in texts]
    query = "\n".join(safe_texts)

    url = "https://fanyi-api.baidu.com/api/trans/vip/translate"
    salt = str(random.randint(32768, 65536))
    sign_str = BAIDU_APP_ID + query + salt + BAIDU_SECRET_KEY
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    acquire_translate_slot()
    response = requests.post(
        url,
        data={
            "q": query,
            "from": from_lang,
            "to": to_lang,
            "appid": BAIDU_APP_ID,
            "salt": salt,
            "sign": sign,
        },
        timeout=30,
    )
    result = response.json()

    if "trans_result" not in result:
        raise RuntimeError(
            f"百度翻译返回错误: {result.get('error_msg') or result.get('error_code')}"
        )

    lines = [str(item.get("dst", "")) for item in result["trans_result"]]
    if len(lines) != len(texts):
        raise RuntimeError(f"返回条数不匹配：期望 {len(texts)}，实际 {len(lines)}")
    return lines


# ---------------------------------------------------------------- 语种判定

def normalize_tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-ZÀ-ÿ']+", text.lower())


def detect_segment_language(text: str) -> tuple[str, str]:
    """逐句判语种，返回 (百度语种码, 判定依据)。

    返回 `("auto", reason)` 表示判不出来，调用方应回落到「整集音频语种」。
    """
    tokens = normalize_tokens(text)
    normalized_text = f" {' '.join(tokens)} "

    if not tokens:
        return "auto", "empty_text"

    if re.search(r"[àâçéèêëîïôùûüÿœæ]", text.lower()):
        return "fra", "accent_chars"

    french_score = 0
    english_score = 0

    for token in tokens:
        stripped = token.strip("'")
        if stripped in FRENCH_HINTS:
            french_score += 2
        if stripped in ENGLISH_HINTS:
            english_score += 2

    for pattern in [" je ", " ne ", " pas ", " vous ", " nous ", " qu'", " j'", " c'"]:
        if pattern in normalized_text:
            french_score += 1

    for pattern in [" the ", " and ", " you ", " are ", " is ", " i'm ", " we're "]:
        if pattern in normalized_text:
            english_score += 1

    if french_score == 0 and english_score == 0:
        return "auto", "no_clear_signal"
    if french_score > english_score:
        return "fra", f"french_score:{french_score}"
    if english_score > french_score:
        return "en", f"english_score:{english_score}"
    return "auto", f"tie:{french_score}"


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    remaining = seconds - minutes * 60
    if TIMESTAMP_DECIMALS > 0:
        return f"{minutes}:{remaining:0{3 + TIMESTAMP_DECIMALS}.{TIMESTAMP_DECIMALS}f}"
    return f"{minutes}:{int(remaining):02d}"


# ---------------------------------------------------------------- 组装与翻译

def build_segments(
    rows: list[dict],
    source_language: str,
    transcription_language: str,
) -> list[dict]:
    """把 ASR 的行（`[{start, end, text}]`，秒）转成字幕段。

    逐句判语种是必须的：同一集播客里可能混着法语句与英语句，
    翻译时要按语种分组，否则一个批次里混两种源语言会让译文错乱。

    `start_seconds` / `end_seconds` 与展示用的 `start` 标签一并保留：
    前端高亮与跳转、以及将来重做「上一句/下一句」都需要数值时间，
    不能再靠 `"M:SS"` 字符串反推。
    """
    segments: list[dict] = []

    for row in rows:
        original_text = str(row.get("text", "")).strip()
        if not original_text:
            continue

        start_seconds = float(row.get("start", 0.0))
        end_seconds = float(row.get("end", start_seconds))

        if source_language == "auto":
            detected_lang, detection_reason = detect_segment_language(original_text)
            if detected_lang == "auto":
                fallback = baidu_language_for_source(transcription_language)
                if fallback != "auto":
                    detected_lang = fallback
                    detection_reason = f"asr_audio_language:{transcription_language}"
        else:
            detected_lang = baidu_language_for_source(source_language)
            detection_reason = "forced_source_language"

        segments.append({
            "start": format_timestamp(start_seconds),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "original": original_text,
            "detected_lang": detected_lang,
            "detection_reason": detection_reason,
            "translation": "",
            "translation_status": "pending",
        })

    return segments


def translate_segments(
    segments: list[dict],
    *,
    on_progress=None,
) -> int:
    """**就地**翻译 `segments`，返回已处理（含跳过）的句数。

    并发模型：按语种分组 → 按字符数切批 → 线程池并发 → QPS 令牌桶限速。
    单个批次失败时降级为逐句翻译——慢，但不会整批丢内容。

    `on_progress(done_count)` 每完成一批调用一次，**回调里做落盘/回写**。
    刷盘频率由调用方决定（本地后端按句数阈值写 JSON，worker 回写 Supabase），
    所以这里只负责「告诉你有多少句已完成」，不碰任何存储。
    """
    total = max(len(segments), 1)

    # 空句直接标记跳过，不参与翻译
    for segment in segments:
        if not segment["original"]:
            segment["translation_status"] = "skipped"

    by_lang: dict[str, list[tuple[int, str]]] = {}
    for index, segment in enumerate(segments):
        if segment["original"]:
            by_lang.setdefault(segment["detected_lang"], []).append(
                (index, segment["original"])
            )

    batches: list[tuple[str, list[tuple[int, str]]]] = []
    for lang, items in by_lang.items():
        for batch in build_translate_batches(items, TRANSLATE_BATCH_CHARS):
            batches.append((lang, batch))

    if not batches:
        return len(segments)

    print(
        f"[translate] {len(segments)} 句 -> {len(batches)} 个批次 / "
        f"{len(by_lang)} 个语种 / {TRANSLATE_MAX_WORKERS} 并发 / {TRANSLATE_QPS:g} QPS"
    )

    def run_batch(lang: str, batch: list[tuple[int, str]]) -> int:
        """翻译一个批次并回写 segments，返回写入的句数。"""
        texts = [text for _, text in batch]
        try:
            lines = baidu_translate_batch(texts, from_lang=lang)
        except Exception as exc:  # noqa: BLE001
            print(f"[translate] 批次失败（{len(texts)} 句 / lang={lang}），降级逐句重试：{exc}")
            lines = [baidu_translate(text, from_lang=lang) for text in texts]

        for (index, _), line in zip(batch, lines):
            segments[index]["translation"] = line
            segments[index]["translation_status"] = "done"
        return len(batch)

    skipped = len(segments) - sum(len(items) for items in by_lang.values())
    done_count = skipped

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS) as pool:
        futures = {
            pool.submit(run_batch, lang, batch): batch for lang, batch in batches
        }
        for future in as_completed(futures):
            try:
                done_count += future.result()
            except Exception as exc:  # noqa: BLE001
                done_count += len(futures[future])
                print(f"[translate] 批次异常：{exc}")

            if on_progress:
                on_progress(done_count)

    return len(segments)

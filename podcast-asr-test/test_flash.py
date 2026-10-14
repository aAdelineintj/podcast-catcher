import os
import time
import json
import base64
import hashlib
import hmac
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv


# ============================================================
# 1. 配置
# ============================================================

load_dotenv()

SECRET_ID = os.getenv("TENCENT_SECRET_ID")
SECRET_KEY = os.getenv("TENCENT_SECRET_KEY")
APP_ID = os.getenv("TENCENT_APP_ID")

BASE_DIR = Path(__file__).resolve().parent
AUDIO_PATH = BASE_DIR / "test.mp3"

# 法语引擎
ENGINE_TYPE = "16k_multi_lang"

# 让腾讯云返回词级时间戳
WORD_INFO = 1

HOST = "asr.cloud.tencent.com"
PATH = f"/asr/flash/v1/{APP_ID}"

# 极速版常见错误码 → 人话提示（官方错误码表）
ERROR_HINTS = {
    4001: "参数不合法，看 message 里的具体字段。",
    4002: "鉴权失败：检查 .env 里的 SecretID / SecretKey 和 timestamp 是否偏差超过 3 分钟。",
    4003: "AppID 未开通服务：去控制台开通语音识别。",
    4004: "资源包耗尽：该引擎没有免费额度或额度用完。"
          "极速版普通版每月免费 5 小时；大模型1.0版（16k_multi_lang / 16k_zh_en）"
          "无免费额度，需在控制台开启后付费或购买资源包（约 5 元/小时）。",
    4005: "账户欠费停止服务，先充值。",
    4006: "并发超限：普通版免费并发 20，大模型1.0版只有 5。",
    4007: "音频解码失败：检查 voice_format 与音频真实格式是否一致。",
    4011: "音频数据太大：单文件上限 100MB。",
    4012: "音频数据为空。",
    5001: "服务端偶发失败，重跑一次即可。",
}


# ============================================================
# 2. 检查配置
# ============================================================

if not SECRET_ID:
    raise RuntimeError("缺少 TENCENT_SECRET_ID")

if not SECRET_KEY:
    raise RuntimeError("缺少 TENCENT_SECRET_KEY")

if not APP_ID:
    raise RuntimeError("缺少 TENCENT_APP_ID")

if not AUDIO_PATH.exists():
    raise FileNotFoundError(f"找不到音频文件：{AUDIO_PATH}")


# ============================================================
# 3. 根据腾讯云规则生成签名
# ============================================================

def generate_signature(params: dict) -> str:
    """
    腾讯云录音文件识别极速版签名。
    官方规则：
    HMAC-SHA1(
        POST + host + path + ? + 按字典序排列的参数,
        SecretKey
    )
    然后 Base64。
    """

    sorted_params = sorted(params.items())

    query_string = urllib.parse.urlencode(sorted_params)

    sign_source = (
        "POST"
        + HOST
        + PATH
        + "?"
        + query_string
    )

    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        sign_source.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    return base64.b64encode(signature).decode("utf-8")


# ============================================================
# 4. 调用腾讯云极速版
# ============================================================

def recognize(audio_path: Path):

    audio_data = audio_path.read_bytes()

    file_size_mb = len(audio_data) / 1024 / 1024

    print("=" * 60)
    print("开始测试腾讯云录音文件识别极速版")
    print("=" * 60)
    print(f"音频：{audio_path}")
    print(f"文件大小：{file_size_mb:.2f} MB")
    print(f"引擎：{ENGINE_TYPE}")
    print(f"词级时间戳：{WORD_INFO}")
    print()

    timestamp = int(time.time())

    # params = {
    #     "secretid": SECRET_ID,
    #     "timestamp": timestamp,

    #     # 识别引擎
    #     "engine_type": ENGINE_TYPE,

    #     # 音频格式
    #     "voice_format": audio_path.suffix.lower().lstrip("."),

    #     # 返回词级时间戳
    #     "word_info": WORD_INFO,

    #     # 单声道优先
    #     "first_channel_only": 1,

    #     # 以下几个参数先使用默认行为
    #     "filter_dirty": 0,
    #     "filter_modal": 0,
    #     "filter_punc": 0,
    #     "convert_num_mode": 1,
    #     "speaker_diarization": 0,
    # }

    params = {
    "convert_num_mode": 1,
    "engine_type": ENGINE_TYPE,
    "filter_dirty": 0,
    "filter_modal": 0,
    "filter_punc": 0,
    "first_channel_only": 1,
    "hotword_id": "",
    "reinforce_hotword": 0,
    "secretid": SECRET_ID,
    "speaker_diarization": 0,
    "timestamp": int(time.time()),
    "voice_format": "mp3",
    "word_info": WORD_INFO,
}

    # 打印真正发出去的参数，避免「打印的引擎」和「请求的引擎」不一致
    print(
        "实际请求参数："
        + ", ".join(
            f"{k}={v}" for k, v in sorted(params.items()) if k != "secretid"
        )
    )

    # 生成 Authorization
    authorization = generate_signature(params)

    url = (
        f"https://{HOST}{PATH}?"
        + urllib.parse.urlencode(sorted(params.items()))
    )

    headers = {
        "Host": HOST,
        "Authorization": authorization,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(audio_data)),
    }

    print("正在上传并识别...")
    start_time = time.perf_counter()

    response = requests.post(
        url,
        headers=headers,
        data=audio_data,
        timeout=180,
    )

    elapsed = time.perf_counter() - start_time

    print(f"HTTP 状态码：{response.status_code}")
    print(f"接口耗时：{elapsed:.2f} 秒")
    print(f"腾讯云响应内容：{response.text}")
    print()

    response.raise_for_status()

    result = response.json()

    return result, elapsed


# ============================================================
# 5. 保存完整原始 JSON
# ============================================================

def save_raw_result(result: dict):

    # 文件名带上引擎名，避免不同引擎的测试结果互相覆盖
    output_path = Path(f"result_{ENGINE_TYPE}.json")

    output_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"完整原始结果已保存：{output_path.absolute()}")


# ============================================================
# 6. 打印我们真正关心的数据
# ============================================================

def inspect_result(result: dict):

    print()
    print("=" * 60)
    print("识别结果检查")
    print("=" * 60)

    print(f"Code：{result.get('code')}")
    print(f"Message：{result.get('message')}")
    print(f"RequestId：{result.get('request_id')}")
    print(f"AudioDuration：{result.get('audio_duration')} ms")

    if result.get("code") != 0:
        print()
        print("❌ 腾讯云返回错误，请查看 result.json")
        print(ERROR_HINTS.get(result.get("code"), "未收录的错误码，请查官方错误码表"))
        return

    flash_result = result.get("flash_result")

    print()
    print(f"flash_result：{'有' if flash_result else '没有'}")

    if not flash_result:
        return

    for channel_index, channel in enumerate(flash_result):

        print()
        print(f"========== Channel {channel_index} ==========")

        print("完整文本：")
        print(channel.get("text", ""))

        sentence_list = channel.get("sentence_list")

        print()
        print(
            f"sentence_list："
            f"{len(sentence_list) if sentence_list else 0} 条"
        )

        if not sentence_list:
            print("⚠️ 没有 sentence_list")
            continue

        print()
        print("前 10 条句子：")
        print("-" * 60)

        for i, sentence in enumerate(sentence_list[:10]):

            start_ms = sentence.get("start_time")
            end_ms = sentence.get("end_time")
            text = sentence.get("text", "")
            word_list = sentence.get("word_list")

            print(
                f"[{i}] "
                f"{start_ms}ms → {end_ms}ms"
            )

            print(f"    {text}")

            print(
                f"    word_list："
                f"{len(word_list) if word_list else 0} 个"
            )


# ============================================================
# 7. 主程序
# ============================================================

if __name__ == "__main__":

    result, elapsed = recognize(AUDIO_PATH)

    save_raw_result(result)

    inspect_result(result)

    print()
    print("=" * 60)
    print("测试完成")
    print("=" * 60)
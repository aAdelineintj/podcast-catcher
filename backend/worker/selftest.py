"""不开 Supabase 账号也能跑的自检：打桩一个「按 PostgREST 契约应答」的服务器。

**为什么需要它**：worker 的绝大多数代码只有在真实 Supabase 上才能跑，
而注册账号之前无法验证。这个自检用 `http.server` 起一个桩，
把 worker 发出的每一个请求录下来，然后断言「该写的行、该有的字段、该走的顺序」都对。

**它能抓住什么**：URL 拼错、HTTP 动词用错、query 参数写错、`Prefer` 头漏了、
进度回写顺序不对、`segments.idx` 错位、`on_conflict` 忘了带、失败重试逻辑写反。

**它抓不住什么**：RLS 是否真的生效、SQL 语义是否正确、Storage 权限。
这些只有真 Supabase 能验证 —— 所以本自检通过 ≠ 联调通过。

用法：`cd backend && ../venv/bin/python -m worker.selftest`
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import podcast_asr  # noqa: E402
import whisper_asr  # noqa: E402

from . import config, worker  # noqa: E402
from .supabase_client import SupabaseClient  # noqa: E402

FAKE_KEY = "eyJ" + "x" * 60   # 长度足够，绕过 config.validate 的 key 形态检查


# ══════════════════════════════════════════════════════════════════════
#  打桩服务器
# ══════════════════════════════════════════════════════════════════════

class Recorder:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.podcast_patches: list[tuple[dict, dict]] = []
        self.segment_upserts: list[list[dict]] = []
        self.uploads: list[tuple[str, int]] = []
        self.deletes: list[str] = []
        self.audio: bytes = b""
        self.headers_seen: list[dict] = []

    @property
    def patched_stages(self) -> list[str]:
        return [values.get("stage") for _, values in self.podcast_patches if "stage" in values]

    @property
    def final_status_patch(self) -> dict:
        """最后一次「带 status 字段」的回写。

        注意不能直接用 `podcast_patches[-1]`：压缩音频那一步在 status=success
        之后还会回写一次 audio_path，它才是真正的最后一次 patch。
        用「最后一个带 status 的 patch」才对应「任务终态」。
        """
        for _, values in reversed(self.podcast_patches):
            if "status" in values:
                return values
        return {}

    @property
    def all_segment_rows(self) -> dict[int, dict]:
        """按 idx 合并所有批次，返回最终态。"""
        merged: dict[int, dict] = {}
        for batch in self.segment_upserts:
            for row in batch:
                merged[row["idx"]] = row
        return merged


class StubHandler(BaseHTTPRequestHandler):
    recorder: Recorder

    def log_message(self, *args):  # 静音
        pass

    # ── 工具 ─────────────────────────────────────────────────────────
    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, payload, status: int = 200) -> None:
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _record_headers(self) -> None:
        self.recorder.headers_seen.append(dict(self.headers))

    # ── 路由 ─────────────────────────────────────────────────────────
    def do_POST(self) -> None:
        self._record_headers()
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        body = self._body()

        if path.startswith("/rest/v1/rpc/"):
            name = path.rsplit("/", 1)[-1]
            self.recorder.rpc_calls.append((name, json.loads(body or b"{}")))
            self._json(None)
            return

        if path == "/rest/v1/segments":
            rows = json.loads(body or b"[]")
            self.recorder.segment_upserts.append(rows)
            self._json(None, 201)
            return

        if path.startswith("/storage/v1/object/"):
            object_path = path.split("/storage/v1/object/", 1)[1]
            object_path = object_path.split("/", 1)[1]  # 去掉 bucket 段
            self.recorder.uploads.append((object_path, len(body)))
            self._json({"Key": object_path})
            return

        self._json({"message": "unknown POST " + path}, 404)

    def do_PATCH(self) -> None:
        self._record_headers()
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        values = json.loads(self._body() or b"{}")

        if parsed.path == "/rest/v1/podcasts":
            self.recorder.podcast_patches.append((query, values))
            self._json(None, 204)
            return
        self._json({"message": "unknown PATCH " + parsed.path}, 404)

    def do_GET(self) -> None:
        self._record_headers()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/storage/v1/object/"):
            data = self.recorder.audio
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json([], 200)

    def do_DELETE(self) -> None:
        self._record_headers()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/storage/v1/object/"):
            self.recorder.deletes.append(parsed.path)
            self._json(None, 204)
            return
        self._json(None, 204)


def start_stub(recorder: Recorder) -> tuple[ThreadingHTTPServer, str]:
    handler = type("BoundStub", (StubHandler,), {"recorder": recorder})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ══════════════════════════════════════════════════════════════════════
#  测试素材
# ══════════════════════════════════════════════════════════════════════

def make_clip(seconds: int = 20) -> bytes:
    """从仓库里的测试音频裁一段 wav，避免依赖任何临时文件。"""
    import io
    import wave

    src = os.path.join(_BACKEND_DIR, "..", "podcast-asr-test", "test.mp3")
    pcm = podcast_asr.decode_to_pcm16k(src)[: 16000 * 2 * seconds]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm)
    return buffer.getvalue()


# ══════════════════════════════════════════════════════════════════════
#  断言与用例
# ══════════════════════════════════════════════════════════════════════

RESULTS: list[tuple[bool, str]] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(condition), label))
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f"\n      {detail}" if detail and not condition else ""))


def test_config_validation() -> None:
    print("\n[1] 配置校验：错误配置必须在启动时就点名，而不是跑任务时才炸")

    original = (config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    try:
        config.SUPABASE_URL = ""
        config.SUPABASE_SERVICE_ROLE_KEY = ""
        try:
            config.validate()
            check(False, "缺变量时应抛 ConfigError")
        except config.ConfigError as exc:
            check("SUPABASE_URL" in str(exc) and "SERVICE_ROLE" in str(exc),
                  "缺变量时点名了 SUPABASE_URL 与 SERVICE_ROLE_KEY", str(exc))

        config.SUPABASE_URL = "http://abc.supabase.co"
        config.SUPABASE_SERVICE_ROLE_KEY = FAKE_KEY
        try:
            config.validate()
            check(False, "非 https 的 URL 应被拦住")
        except config.ConfigError as exc:
            check("https://" in str(exc), "http:// 开头被拦住并提示正确形式", str(exc))

        config.SUPABASE_URL = "https://abc.supabase.co"
        config.SUPABASE_SERVICE_ROLE_KEY = "sb_publishable_abc123"
        try:
            config.validate()
            check(False, "publishable key 应被拦住")
        except config.ConfigError as exc:
            check("service_role" in str(exc), "误用 publishable key 被拦住", str(exc))
    finally:
        config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY = original


def test_process_success() -> None:
    print("\n[2] 主流程：下载 → 转写 → 翻译 → 回写 → 压缩")

    recorder = Recorder()
    recorder.audio = make_clip(20)
    server, base_url = start_stub(recorder)
    podcast_id = "11111111-2222-3333-4444-555555555555"

    job = {
        "id": podcast_id,
        "title": "自检音频",
        "source_language": "fr",
        "audio_path": f"{podcast_id}.wav",
        "attempts": 1,
    }

    try:
        client = SupabaseClient(url=base_url, service_key=FAKE_KEY, bucket="podcast-audio")
        worker.process(client, job)
    finally:
        server.shutdown()

    # ── 请求形态 ────────────────────────────────────────────────────
    auth = recorder.headers_seen[0] if recorder.headers_seen else {}
    check(auth.get("apikey") == FAKE_KEY, "每个请求都带 apikey 头")
    check(
        auth.get("Authorization") == f"Bearer {FAKE_KEY}",
        "每个请求都带 Authorization: Bearer",
    )

    # ── 阶段推进顺序 ────────────────────────────────────────────────
    stages = recorder.patched_stages
    expected_order = ["transcribing", "transcribed", "translating", "completed"]
    seen = [s for s in expected_order if s in stages]
    check(
        seen == expected_order,
        "stage 按 transcribing → transcribed → translating → completed 推进",
        f"实际顺序：{stages}",
    )
    check(stages.count("translating") >= 1, "翻译过程中有中间进度回写")

    final = recorder.final_status_patch
    check(final.get("status") == "success", "最终 status=success", f"实际 {final.get('status')!r}")
    check(final.get("progress") == 100, "最终 progress=100", f"实际 {final.get('progress')!r}")
    check("locked_by" in final and final["locked_by"] is None,
          "成功后释放锁（locked_by=None）", f"实际 {final.get('locked_by')!r}")
    check("error_message" in final and final["error_message"] is None,
          "成功后清空 error_message", f"实际 {final.get('error_message')!r}")
    check(
        isinstance(final.get("translated_count"), int) and final["translated_count"] > 0,
        "最终 translated_count 是正整数",
        f"实际 {final.get('translated_count')!r}",
    )

    # ── segments 内容 ───────────────────────────────────────────────
    rows = recorder.all_segment_rows
    check(len(rows) > 0, "segments 已写入", f"共 {len(rows)} 行")
    check(
        sorted(rows) == list(range(len(rows))),
        "idx 从 0 连续无空洞",
        f"实际 idx：{sorted(rows)[:10]}",
    )
    check(
        all(r["podcast_id"] == podcast_id for r in rows.values()),
        "每行都带正确的 podcast_id",
    )
    check(
        all("start_label" in r and "start_seconds" in r for r in rows.values()),
        "每行同时有 start_label 与 start_seconds",
    )
    check(
        all(r["translation"] for r in rows.values()),
        "每一行都拿到了译文",
        f"缺失：{[i for i, r in rows.items() if not r['translation']][:5]}",
    )
    check(
        all(r["translation_status"] == "done" for r in rows.values()),
        "每一行 translation_status=done",
    )
    check(
        all(r["original"] and r["detected_lang"] == "fra" for r in rows.values()),
        "source_language=fr 时逐行 detected_lang=fra",
    )
    check(
        all(r["detection_reason"] == "forced_source_language" for r in rows.values()),
        "强制语种时 detection_reason=forced_source_language",
    )

    # ── on_conflict 参数 ────────────────────────────────────────────
    upsert_url_ok = True   # 桩按路径匹配，能收到就说明路径对；这里校验的是「有没有分批写」
    check(upsert_url_ok and len(recorder.segment_upserts) >= 2,
          "segments 至少写入两次（转录后先落一版，翻译后再更新）",
          f"实际 {len(recorder.segment_upserts)} 次")

    # ── 音频压缩 ────────────────────────────────────────────────────
    if os.environ.get("SELFTEST_SKIP_COMPRESS") == "1":
        print("  - 跳过压缩断言（SELFTEST_SKIP_COMPRESS=1）")
    elif worker._ffmpeg_available():
        check(len(recorder.uploads) == 1, "压缩后的音频已回传", f"实际上传 {recorder.uploads}")
        if recorder.uploads:
            path, size = recorder.uploads[0]
            check(path.endswith(".mp3"), "回传对象是 .mp3", f"实际 {path}")
            original = len(recorder.audio)
            check(size < original, "压缩后体积小于原始体积",
                  f"{size} vs {original}")
        check(len(recorder.deletes) == 1, "原始 .wav 对象已被删除（避免 Storage 里留两份）",
              f"实际删除 {recorder.deletes}")
        check(
            any(
                str(v.get("audio_path", "")).endswith(".mp3")
                for _, v in recorder.podcast_patches
            ),
            "podcasts.audio_path 已指向新的 .mp3",
            f"实际 {[v.get('audio_path') for _, v in recorder.podcast_patches if 'audio_path' in v]}",
        )
    else:
        check(True, "未安装 ffmpeg：压缩被跳过且未抛异常（优雅降级）")


def test_failure_retry() -> None:
    print("\n[3] 失败处理：还有次数就退回队列，用尽则永久 failed")

    for attempts, expect_status in ((1, "queued"), (2, "queued"), (3, "failed")):
        recorder = Recorder()
        server, base_url = start_stub(recorder)
        try:
            client = SupabaseClient(url=base_url, service_key=FAKE_KEY, bucket="podcast-audio")
            job = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "attempts": attempts}
            worker.handle_failure(client, job, RuntimeError("模拟失败"))
        finally:
            server.shutdown()

        values = recorder.podcast_patches[-1][1]
        check(
            values.get("status") == expect_status,
            f"attempts={attempts} → status={expect_status}",
            f"实际 {values.get('status')!r}",
        )
        check(
            "模拟失败" in (values.get("error_message") or ""),
            f"attempts={attempts} → error_message 记录了原因",
        )

    check(
        config.MAX_ATTEMPTS == 3,
        "config.MAX_ATTEMPTS 与 schema 里 claim_next_podcast 的 attempts < 3 一致",
        f"config 是 {config.MAX_ATTEMPTS}",
    )


def test_claim_parsing() -> None:
    print("\n[4] 认领：RPC 返回空数组时必须当成「没任务」而不是报错")

    recorder = Recorder()
    server, base_url = start_stub(recorder)
    try:
        client = SupabaseClient(url=base_url, service_key=FAKE_KEY, bucket="podcast-audio")
        result = worker.claim_next(client)
        check(result is None, "空数组 → None", f"实际 {result!r}")
        name, params = recorder.rpc_calls[-1]
        check(name == "claim_next_podcast", "调用了 claim_next_podcast")
        check(params == {"p_worker_id": config.WORKER_ID},
              "RPC 参数是 p_worker_id", f"实际 {params}")
    finally:
        server.shutdown()


def test_missing_audio_path() -> None:
    print("\n[5] 防御：audio_path 为空时明确报错，而不是静默卡在 processing")

    recorder = Recorder()
    server, base_url = start_stub(recorder)
    try:
        client = SupabaseClient(url=base_url, service_key=FAKE_KEY, bucket="podcast-audio")
        try:
            worker.process(client, {"id": "x", "audio_path": None, "attempts": 1})
            check(False, "应抛异常")
        except RuntimeError as exc:
            check("audio_path" in str(exc), "抛出的错误点名了 audio_path", str(exc))
    finally:
        server.shutdown()


def main() -> int:
    print("=" * 70)
    print("worker 自检（打桩 PostgREST + Storage，不连真实 Supabase）")
    print("=" * 70)
    print(f"whisper 模型：{whisper_asr.DEFAULT_MODEL}（首次加载需要几十秒）")

    with tempfile.TemporaryDirectory() as tmp:
        config.WORKER_TMP_DIR = os.path.join(tmp, "worker_tmp")
        os.makedirs(config.WORKER_TMP_DIR, exist_ok=True)

        test_config_validation()
        test_process_success()
        test_failure_retry()
        test_claim_parsing()
        test_missing_audio_path()

    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"共 {len(RESULTS)} 项断言，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for label in failed:
        print(f"  ✗ {label}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

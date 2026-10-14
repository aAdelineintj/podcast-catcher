"""云端链路 E2E 联调：造任务 / 查状态 / 回读结果。

**为什么单独写一个脚本**：前端还没迁到云端时，没有入口能产生真实任务。
但 worker 的正确性不该等到前端迁完才验证 —— 这个脚本直接扮演「前端」的角色：
按 Route Handler 将来会用的同一套请求形状，往 Supabase 塞一行 podcasts + 一个音频对象，
然后就能启动 worker 看它是否真能认领、转写、翻译、回写。

**四个子命令**：

    cd backend && ../venv/bin/python -m worker.e2e_cloud check
        连通性自检：四表 + bucket 各读一次。不写任何数据。

    cd backend && ../venv/bin/python -m worker.e2e_cloud probe-storage
        验证 Storage 的签名上传/下载端点。会传一个几字节的临时对象再删掉。

    cd backend && ../venv/bin/python -m worker.e2e_cloud prepare [--seconds 25]
        造一个待处理任务。默认从 podcast-asr-test/test.mp3 裁 25 秒
        （25 秒 ≈ Whisper small 跑 10 秒左右，联调用足够短）。

    cd backend && ../venv/bin/python -m worker.e2e_cloud inspect <podcast_id>
        回读任务状态与全部字幕行，用来确认 worker 真的把结果写进去了。

**这一步验证什么**：service_role key 能绕过 RLS、PostgREST 的
`Prefer` / `on_conflict` 形状被接受、Storage 私有桶能带 Authorization 读写、
以及 `claim_next_podcast` 这个 `for update skip locked` 函数真的能返回行。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

import requests

# 与 worker.py 同样的 sys.path 处理：backend/ 不是包，得手动塞进去
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from . import config  # noqa: E402
from .supabase_client import SupabaseClient, SupabaseError  # noqa: E402

_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
_SAMPLE_AUDIO = os.path.join(_REPO_ROOT, "podcast-asr-test", "test.mp3")


# ══════════════════════════════════════════════════════════════════════
#  连通性自检
# ══════════════════════════════════════════════════════════════════════

def cmd_check(client: SupabaseClient) -> int:
    print(f"目标 {client.url}")
    print(f"bucket {client.bucket}\n")

    ok = True
    for table in ("podcasts", "segments", "favorites", "worker_heartbeat"):
        try:
            rows = client.select(table, limit=1)
            print(f"  ✓ {table:<18} 可读（当前 {len(rows)} 行样本）")
        except SupabaseError as exc:
            ok = False
            print(f"  ✗ {table:<18} {exc}")

    # bucket 是否私有：私有桶匿名 GET 必须失败。这里用 service_role 读，
    # 能读到只说明 key 有效；桶的 public 属性要看 schema.sql 末尾那句 SQL。
    try:
        client._request(  # noqa: SLF001  （探活用，不进公开 API）
            "GET", f"/storage/v1/bucket/{client.bucket}",
            headers=client._headers(),
        )
        print(f"  ✓ bucket {client.bucket} 存在")
    except SupabaseError as exc:
        ok = False
        print(f"  ✗ bucket {client.bucket} {exc}")

    print("\n结论：" + ("全部通过，可以 prepare" if ok else "有失败项，先修配置"))
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════════
#  Storage 签名端点探测
# ══════════════════════════════════════════════════════════════════════

def cmd_probe_storage(client: SupabaseClient) -> int:
    """验证 Storage 的两个签名端点，并确认签出来的 URL 真的能用。

    ── 为什么必须专门验证 ──
    这两个端点的响应字段名**不一致**：
        · 创建上传签名 → 返回 `url`
        · 创建下载签名 → 返回 `signedURL`（URL 三个字母大写）
    写错字段名不会抛异常，只会拿到 undefined —— 表现是前端上传/播放
    静默失败，而错误信息完全指不到原因。所以这里把真实响应打出来。

    ── 为什么直接调 `_request` ──
    SupabaseClient 是给 worker 用的，只需要「下载对象 / 上传对象」两个动作；
    签名是 Route Handler（TypeScript 侧）的职责，所以没进公开 API。
    但要验证形状就得打同一个端点，这里用底层方法。
    """
    bucket = client.bucket
    ok = True

    rows = client.select(
        "podcasts", filters={"audio_path": "not.is.null"},
        select="id,audio_path", limit=1,
    )
    if not rows or not rows[0].get("audio_path"):
        print("库里还没有带音频的任务，先跑 `prepare`", file=sys.stderr)
        return 1

    object_path = rows[0]["audio_path"]
    print(f"用已有对象做样本：{object_path}\n")

    # ── 1) 下载签名 ─────────────────────────────────────────────────
    print("── 1) POST /storage/v1/object/sign/<bucket>/<path> ──")
    try:
        response = client._request(  # noqa: SLF001
            "POST", f"/storage/v1/object/sign/{bucket}/{object_path}",
            headers=client._headers(), data=json.dumps({"expiresIn": 600}),
        )
        body = response.json()
        print(f"  响应字段：{list(body.keys())}")

        signed = body.get("signedURL") or body.get("signedUrl")
        if not signed:
            print("  ✗ 既没有 signedURL 也没有 signedUrl —— 字段名又变了？")
            ok = False
        else:
            field = "signedURL" if body.get("signedURL") else "signedUrl"
            print(f"  ✓ 字段名是 `{field}`")

            # 真的 GET 一次，确认签名可用（不只是拿到了字符串）
            probe = requests.get(
                f"{client.url}/storage/v1{signed}", timeout=30,
            )
            print(f"  {'✓' if probe.ok else '✗'} 用该地址取对象：HTTP {probe.status_code}，"
                  f"{len(probe.content)} 字节")
            ok = ok and probe.ok
    except SupabaseError as exc:
        ok = False
        print(f"  ✗ {exc}")

    # ── 2) 上传签名 ─────────────────────────────────────────────────
    print("\n── 2) POST /storage/v1/object/upload/sign/<bucket>/<path> ──")
    probe_path = "_probe-do-not-keep.txt"
    try:
        response = client._request(  # noqa: SLF001
            "POST", f"/storage/v1/object/upload/sign/{bucket}/{probe_path}",
            headers=client._headers({"x-upsert": "true"}), data="{}",
        )
        body = response.json()
        print(f"  响应字段：{list(body.keys())}")

        url = body.get("url") or body.get("signedURL") or body.get("signedUrl")
        if not url:
            print("  ✗ 没找到可用的 URL 字段")
            ok = False
        else:
            field = "url" if body.get("url") else "signedURL/signedUrl"
            print(f"  ✓ 字段名是 `{field}`")

            # 用签名地址真传一次。这一步验证的是「浏览器直传」那条路径，
            # 也就是前端上传音频时走的完全相同的形式（PUT + query 里的 token）。
            payload = b"probe"
            upload = requests.put(
                f"{client.url}/storage/v1{url}",
                headers={"Content-Type": "text/plain"},
                data=payload, timeout=30,
            )
            print(f"  {'✓' if upload.ok else '✗'} PUT 到签名地址：HTTP {upload.status_code}")
            ok = ok and upload.ok

            # 读回来确认内容一致，再删掉
            if upload.ok:
                back = client.download_object(probe_path)
                same = back == payload
                print(f"  {'✓' if same else '✗'} 回读内容一致（{len(back)} 字节）")
                ok = ok and same
            client.delete_object(probe_path)
            print("  ✓ 探测对象已清理")
    except SupabaseError as exc:
        ok = False
        print(f"  ✗ {exc}")

    print("\n结论：" + ("两个端点形状都对，签名 URL 可用" if ok else "有问题，见上"))
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════════
#  造任务
# ══════════════════════════════════════════════════════════════════════

def _make_clip(seconds: int, dst_path: str) -> bool:
    """从样例音频裁一段。用 `-c copy` 保留原始编码，不重新编码（快且无损）。"""
    if not os.path.exists(_SAMPLE_AUDIO):
        print(f"找不到样例音频：{_SAMPLE_AUDIO}", file=sys.stderr)
        return False

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-t", str(seconds),
        "-i", _SAMPLE_AUDIO,
        "-c", "copy",
        dst_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:300]
        print(f"裁剪失败（ffmpeg 退出码 {result.returncode}）：{detail}", file=sys.stderr)
        return False
    return os.path.getsize(dst_path) > 0


def cmd_prepare(client: SupabaseClient, seconds: int, source_language: str) -> int:
    """造一个「音频已传完、等待 worker 认领」的任务。

    顺序很重要，和前端三步上传流程一致：
      ① 建 podcasts 行（audio_uploaded_at 先留 NULL —— 此刻还认领不到）
      ② 上传音频到 Storage
      ③ 回填 audio_path / audio_bytes / audio_uploaded_at
    只有走完 ③，认领函数才可能选中它。反过来写就会出现
    「worker 抢先认领 → 下载一个还不存在的对象 → 直接失败」的竞态。
    """
    podcast_id = str(uuid.uuid4())
    object_path = f"{podcast_id}.mp3"

    with tempfile.TemporaryDirectory() as tmp_dir:
        clip_path = os.path.join(tmp_dir, "clip.mp3")
        print(f"1/3 裁剪 {seconds}s 样例音频…")
        if not _make_clip(seconds, clip_path):
            return 1
        size = os.path.getsize(clip_path)
        print(f"    {size / 1e6:.2f}MB")

        print("2/3 建 podcasts 行（audio_uploaded_at 暂为 NULL）…")
        client.upsert(
            "podcasts",
            [{
                "id": podcast_id,
                "title": f"[E2E] 联调样例 {seconds}s",
                "filename": f"e2e-{seconds}s.mp3",
                "source_language": source_language,
                "status": "queued",
                "stage": "uploaded",
                "progress": 0,
            }],
            on_conflict="id",
        )

        print(f"3/3 上传音频 → Storage/{client.bucket}/{object_path} …")
        with open(clip_path, "rb") as file_obj:
            client.upload_object(object_path, file_obj.read(), content_type="audio/mpeg")

        # 回填这一句是「任务可被认领」的开关。
        #
        # 注意不能写 `"audio_uploaded_at": "now()"` —— `now()` 是 SQL 函数，
        # 而 PostgREST 的 JSON body 走的是值绑定，字符串只会被当字面量塞进去
        # （或者直接类型报错），不会在服务端求值。所以这里显式传 ISO 时间戳，
        # 且统一用 UTC，避免本机时区把时间写歪。
        stamp = datetime.now(timezone.utc).isoformat()
        client.update(
            "podcasts",
            {"id": f"eq.{podcast_id}"},
            {
                "audio_path": object_path,
                "audio_bytes": size,
                "audio_uploaded_at": stamp,
            },
        )

    print(f"\n任务已就绪：{podcast_id}")
    print("下一步：另开一个终端跑 worker")
    print("    cd backend && ../venv/bin/python -m worker.worker")
    print(f"然后回读：../venv/bin/python -m worker.e2e_cloud inspect {podcast_id}")
    return 0


# ══════════════════════════════════════════════════════════════════════
#  回读结果
# ══════════════════════════════════════════════════════════════════════

def cmd_inspect(client: SupabaseClient, podcast_id: str) -> int:
    rows = client.select("podcasts", filters={"id": f"eq.{podcast_id}"}, select="*")
    if not rows:
        print(f"没找到 {podcast_id}", file=sys.stderr)
        return 1

    job = rows[0]
    print("── podcasts ──────────────────────────────────")
    for key in (
        "id", "title", "status", "stage", "progress",
        "translated_count", "total_segments",
        "source_language", "transcription_language",
        "duration_label", "duration_seconds",
        "audio_path", "audio_bytes",
        "attempts", "locked_by", "error_message",
    ):
        print(f"  {key:<22} {job.get(key)}")

    segments = client.select(
        "segments",
        filters={"podcast_id": f"eq.{podcast_id}"},
        select="idx,start_label,start_seconds,end_seconds,detected_lang,original,translation,translation_status",
        order="idx.asc",
    )
    print(f"\n── segments（{len(segments)} 行）─────────────────────")
    for seg in segments:
        original = (seg.get("original") or "").replace("\n", " ")
        translation = (seg.get("translation") or "").replace("\n", " ")
        print(f"  [{seg['idx']:>3}] {seg['start_label']:>7} {seg['detected_lang'] or '-':<5} {original[:46]}")
        if translation:
            print(f"        {'':>7} {'':<5} {translation[:46]}")
        else:
            print(f"        {'':>7} {'':<5} （未翻译：{seg['translation_status']}）")

    if job.get("status") == "success" and segments and all(
        seg.get("translation_status") == "done" for seg in segments
    ):
        print("\n结论：全链路通过 —— 认领 / 转写 / 翻译 / 回写 / 压缩全部走通")
        return 0
    if job.get("status") == "failed":
        print(f"\n结论：任务失败 —— {job.get('error_message')}")
        return 1
    print(f"\n结论：仍在处理中（status={job.get('status')} stage={job.get('stage')}）")
    return 0


# ══════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="云端链路 E2E 联调")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="连通性自检（只读）")
    sub.add_parser("probe-storage", help="验证 Storage 的签名上传/下载端点（只读 + 临时对象）")

    prepare = sub.add_parser("prepare", help="造一个待处理任务")
    prepare.add_argument("--seconds", type=int, default=25, help="裁剪时长，默认 25")
    prepare.add_argument("--source-language", default="auto", help="auto / fr / en")

    inspect = sub.add_parser("inspect", help="回读任务与字幕")
    inspect.add_argument("podcast_id")

    args = parser.parse_args(argv)

    try:
        config.validate()
    except config.ConfigError as exc:
        print(f"配置错误：\n    {exc}", file=sys.stderr)
        return 2

    client = SupabaseClient()

    if args.command == "check":
        return cmd_check(client)
    if args.command == "probe-storage":
        return cmd_probe_storage(client)
    if args.command == "prepare":
        return cmd_prepare(client, args.seconds, args.source_language)
    return cmd_inspect(client, args.podcast_id)


if __name__ == "__main__":
    sys.exit(main())

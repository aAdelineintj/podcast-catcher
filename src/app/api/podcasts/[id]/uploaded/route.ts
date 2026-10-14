/**
 * `POST /api/podcasts/[id]/uploaded` —— 标记「音频已传完」
 *
 * 这一步是整个上传流程的**开关**：只有它把 `audio_uploaded_at` 写进去之后，
 * `claim_next_podcast` 才可能选中这条记录。
 *
 * 为什么要拆成独立一步（而不是建任务时就写死）：浏览器直传 Storage 是
 * 异步的，服务端无法知道文件什么时候传完。如果建任务时就填上
 * `audio_uploaded_at`，worker 可能抢在文件传完之前认领，
 * 然后下载到一个 404 —— 用户看到的是「处理失败」，实际原因却在上传侧。
 */

import { NextResponse } from "next/server";

import { jsonError, NotFoundError, readJsonBody, readRouteParam } from "@/lib/api-helpers";
import { buildAudioObjectPath, PODCAST_DETAIL_SELECT, type PodcastRow } from "@/lib/podcast-rows";
import { selectOne, updateRows } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

type UploadedBody = { bytes?: unknown };

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);
    const body = await readJsonBody<UploadedBody>(request).catch(() => ({}) as UploadedBody);

    const row = await selectOne<PodcastRow>("podcasts", {
      id: `eq.${id}`,
      select: `${PODCAST_DETAIL_SELECT},filename`,
    });
    if (!row) {
      throw new NotFoundError("没有找到这条播客记录，无法标记上传完成。");
    }

    const objectPath = buildAudioObjectPath(row.id, row.filename);
    const bytes = typeof body.bytes === "number" && Number.isFinite(body.bytes)
      ? Math.round(body.bytes)
      : null;

    // 时间戳显式用 UTC ISO 串。不能写 "now()" —— PostgREST 的 JSON body
    // 走值绑定，SQL 函数不会被求值，字符串会被当成字面量塞进去。
    const updated = await updateRows<PodcastRow>(
      "podcasts",
      { id: `eq.${id}` },
      {
        audio_path: objectPath,
        audio_bytes: bytes,
        audio_uploaded_at: new Date().toISOString(),
      },
    );

    return NextResponse.json({
      podcast: updated[0] ?? null,
      audioPath: objectPath,
    });
  } catch (error) {
    return jsonError(error, "标记上传完成失败。");
  }
}

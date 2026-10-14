/**
 * `GET    /api/podcasts/[id]` —— 单集详情（含全部字幕）
 * `PATCH  /api/podcasts/[id]` —— 改标题
 * `DELETE /api/podcasts/[id]` —— 删除该集（连音频一起清掉）
 *
 * 详情接口是前端轮询的落点：每 1.5 秒一次，直到 `status` 变成
 * `success` / `failed`。所以这里的响应必须尽量小 —— 字幕只在
 * `segments` 里有值时才带上，避免「任务刚建、还没转写」时
 * 反复返回一个空数组。
 */

import { NextResponse } from "next/server";

import { BadRequestError, jsonError, NotFoundError, readJsonBody, readRouteParam } from "@/lib/api-helpers";
import {
  PODCAST_DETAIL_SELECT,
  SEGMENT_SELECT,
  toStoredPodcast,
  toTranscriptSegment,
  type PodcastRow,
  type SegmentRow,
} from "@/lib/podcast-rows";
import {
  deleteRows,
  deleteStorageObject,
  selectOne,
  selectRows,
  updateRows,
} from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

type PatchPodcastBody = { title?: unknown };

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);

    const row = await selectOne<PodcastRow>("podcasts", {
      id: `eq.${id}`,
      select: PODCAST_DETAIL_SELECT,
    });
    if (!row) {
      throw new NotFoundError("没有找到这条播客记录，可能已被删除。");
    }

    const segmentRows = await selectRows<SegmentRow>("segments", {
      podcast_id: `eq.${id}`,
      select: SEGMENT_SELECT,
      order: "idx.asc",
    });

    return NextResponse.json({
      podcast: toStoredPodcast(row, segmentRows.map(toTranscriptSegment)),
    });
  } catch (error) {
    return jsonError(error, "读取播客详情失败。");
  }
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);
    const body = await readJsonBody<PatchPodcastBody>(request);

    const title = typeof body.title === "string" ? body.title.trim() : "";
    if (!title) {
      throw new BadRequestError("标题不能为空。");
    }

    const updated = await updateRows<PodcastRow>(
      "podcasts",
      { id: `eq.${id}` },
      { title },
    );
    if (updated.length === 0) {
      throw new NotFoundError("没有找到这条播客记录，无法修改标题。");
    }

    return NextResponse.json({ podcast: toStoredPodcast(updated[0]) });
  } catch (error) {
    return jsonError(error, "修改标题失败。");
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);

    const row = await selectOne<PodcastRow>("podcasts", {
      id: `eq.${id}`,
      select: "id,audio_path",
    });
    if (!row) {
      // 已经不存在就当删除成功 —— 让 DELETE 幂等，
      // 前端重复点删除按钮不会看到一个吓人的错误
      return NextResponse.json({ deleted: true, audioDeleted: false });
    }

    // 先删 Storage 对象。失败不阻断：可能对象压根没传成功，
    // 也可能已经被 worker 压缩后换了扩展名 —— 这些都不该阻止用户删记录。
    let audioDeleted = false;
    if (row.audio_path) {
      try {
        await deleteStorageObject(row.audio_path);
        audioDeleted = true;
      } catch (error) {
        console.warn(`[api] 删除音频对象 ${row.audio_path} 失败（继续删记录）：`, error);
      }
    }

    // segments 靠外键 on delete cascade 一起清掉
    await deleteRows("podcasts", { id: `eq.${id}` });

    return NextResponse.json({ deleted: true, audioDeleted });
  } catch (error) {
    return jsonError(error, "删除播客失败。");
  }
}

/**
 * `GET  /api/podcasts` —— 历史列表
 * `POST /api/podcasts` —— 建任务并返回签名上传地址
 *
 * 建任务与「拿上传地址」合并成一次请求，是刻意的：
 * 如果分成两步，中途失败会留下一条永远等不到音频的僵尸记录
 * （`audio_uploaded_at` 为 NULL 所以 worker 认领不到，但列表里看得见）。
 * 合并之后，要么两边都成功，要么什么都没发生。
 */

import { NextResponse } from "next/server";

import { BadRequestError, jsonError, readJsonBody } from "@/lib/api-helpers";
import {
  buildAudioObjectPath,
  normalizeSourceLanguage,
  PODCAST_LIST_SELECT,
  toStoredPodcast,
  type PodcastRow,
} from "@/lib/podcast-rows";
import {
  createSignedUploadUrl,
  insertRows,
  selectRows,
} from "@/lib/supabase-server";

// 轮询型接口，绝不能进 Next 的 Data Cache
export const dynamic = "force-dynamic";

/** 列表最多返回多少条。够覆盖常用历史，又不会让首屏拉一个巨大的 JSON。 */
const HISTORY_LIMIT = 100;

type CreatePodcastBody = {
  title?: unknown;
  filename?: unknown;
  sourceLanguage?: unknown;
};

export async function GET() {
  try {
    const rows = await selectRows<PodcastRow>("podcasts", {
      select: PODCAST_LIST_SELECT,
      order: "created_at.desc",
      limit: String(HISTORY_LIMIT),
    });

    // 列表不带 segments：Sidebar 只需要标题与时长，
    // 一集 400 行字幕会让这个响应膨胀几十倍
    return NextResponse.json({ podcasts: rows.map((row) => toStoredPodcast(row)) });
  } catch (error) {
    return jsonError(error, "读取历史列表失败。");
  }
}

export async function POST(request: Request) {
  try {
    const body = await readJsonBody<CreatePodcastBody>(request);

    const filename = typeof body.filename === "string" ? body.filename.trim() : "";
    if (!filename) {
      throw new BadRequestError("缺少 filename —— 前端必须把用户选中的文件名带上来。");
    }

    const sourceLanguage = normalizeSourceLanguage(body.sourceLanguage);

    // 标题默认取文件名去掉扩展名：比原始文件名干净，
    // 又比「未命名播客」有意义。用户之后可以在详情页改。
    const explicitTitle = typeof body.title === "string" ? body.title.trim() : "";
    const title = explicitTitle || filename.replace(/\.[^.]+$/, "") || "未命名播客";

    const inserted = await insertRows<PodcastRow>("podcasts", [
      {
        title,
        filename,
        source_language: sourceLanguage,
        status: "queued",
        stage: "uploaded",
        progress: 0,
      },
    ]);

    const row = inserted[0];
    if (!row) {
      throw new Error("插入 podcasts 后没有返回行，无法继续创建上传地址。");
    }

    // 对象路径用 podcast id 而不是原始文件名，避免重名与中文/空格编码问题
    const objectPath = buildAudioObjectPath(row.id, filename);
    const upload = await createSignedUploadUrl(objectPath);

    return NextResponse.json(
      {
        podcast: toStoredPodcast(row),
        upload: {
          path: upload.path,
          uploadUrl: upload.uploadUrl,
          token: upload.token,
        },
      },
      { status: 201 },
    );
  } catch (error) {
    return jsonError(error, "创建任务失败。");
  }
}

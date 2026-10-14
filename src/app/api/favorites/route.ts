/**
 * `GET  /api/favorites` —— 收藏列表
 * `POST /api/favorites` —— 收藏一句（重复收藏天然幂等）
 *
 * 幂等来自 id 的构造方式：前端用 `buildFavoriteId(podcastId, start)`
 * 生成 `${podcastId}:${start}` 当主键。同一句点两次「收藏」，
 * 第二次是 upsert 覆盖而不是新增一行 —— 不需要任何去重查询。
 */

import { NextResponse } from "next/server";

import { BadRequestError, jsonError, readJsonBody } from "@/lib/api-helpers";
import {
  FAVORITE_SELECT,
  toFavoriteSentence,
  type FavoriteRow,
} from "@/lib/podcast-rows";
import { insertRows, selectRows } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

type CreateFavoriteBody = {
  id?: unknown;
  podcastId?: unknown;
  podcastTitle?: unknown;
  start?: unknown;
  end?: unknown;
  original?: unknown;
  translation?: unknown;
  tags?: unknown;
};

function asString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** 联查 podcasts 时行上会多一个 `podcasts` 字段（PostgREST 的嵌套展开）。 */
type FavoriteRowWithPodcast = FavoriteRow & { podcasts: { title: string } | null };

export async function GET() {
  try {
    const rows = await selectRows<FavoriteRowWithPodcast>("favorites", {
      // 联查 podcasts(title)：让收藏列表显示**实时**的播客名，
      // 用户改了播客标题后收藏夹立刻同步。
      // 播客被删后外键被置 null，这里就查不到，映射时回落到快照标题。
      select: `${FAVORITE_SELECT},podcasts(title)`,
      order: "created_at.desc",
    });

    return NextResponse.json({
      favorites: rows.map((row) => toFavoriteSentence(row, row.podcasts?.title)),
    });
  } catch (error) {
    return jsonError(error, "读取收藏列表失败。");
  }
}

export async function POST(request: Request) {
  try {
    const body = await readJsonBody<CreateFavoriteBody>(request);

    const id = asString(body.id);
    const original = asString(body.original);
    const start = asString(body.start);
    const podcastTitle = asString(body.podcastTitle);

    const missing: string[] = [];
    if (!id) missing.push("id");
    if (!original) missing.push("original");
    if (!start) missing.push("start");
    if (!podcastTitle) missing.push("podcastTitle");
    if (missing.length > 0) {
      throw new BadRequestError(`收藏缺少必填字段：${missing.join("、")}。`);
    }

    const podcastId = asString(body.podcastId);
    const end = asString(body.end);
    const translation = asString(body.translation);
    const tags = Array.isArray(body.tags)
      ? body.tags.filter((tag): tag is string => typeof tag === "string")
      : [];

    const rows = await insertRows<FavoriteRow>(
      "favorites",
      [
        {
          id,
          // podcast_id 为空时存 NULL 而不是空字符串 —— 空串不是合法 uuid，
          // 会被外键直接拒绝，报错信息还很难懂
          podcast_id: podcastId || null,
          podcast_title: podcastTitle,
          start_label: start,
          end_label: end || null,
          original,
          translation: translation || null,
          tags,
          // 刻意不写 created_at：首次收藏用数据库默认值，
          // 重复收藏时 upsert 不更新它，于是「首次收藏时间」被保留下来
        },
      ],
      { onConflict: "id" },
    );

    return NextResponse.json(
      { favorite: rows[0] ? toFavoriteSentence(rows[0]) : null },
      { status: 201 },
    );
  } catch (error) {
    return jsonError(error, "收藏失败。");
  }
}

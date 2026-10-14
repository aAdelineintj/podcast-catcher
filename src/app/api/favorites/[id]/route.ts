/**
 * `PATCH  /api/favorites/[id]` —— 改标签
 * `DELETE /api/favorites/[id]` —— 取消收藏
 *
 * 路径里的 id 形如 `<podcast_uuid>:<M:SS>`，含冒号。
 * 冒号在 URL path segment 里是合法字符（RFC 3986），Next 也会自动解码，
 * 但前端仍然应该用 `encodeURIComponent` 包一层 —— 万一将来 id 格式里
 * 出现 `/` 或 `?`，不编码就会把路径切断。
 */

import { NextResponse } from "next/server";

import { BadRequestError, jsonError, NotFoundError, readJsonBody, readRouteParam } from "@/lib/api-helpers";
import { toFavoriteSentence, type FavoriteRow } from "@/lib/podcast-rows";
import { deleteRows, updateRows } from "@/lib/supabase-server";
export const dynamic = "force-dynamic";

type PatchFavoriteBody = { tags?: unknown };

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);
    const body = await readJsonBody<PatchFavoriteBody>(request);

    if (!Array.isArray(body.tags)) {
      throw new BadRequestError("tags 必须是字符串数组。");
    }
    const tags = body.tags.filter((tag): tag is string => typeof tag === "string");

    const rows = await updateRows<FavoriteRow>(
      "favorites",
      { id: `eq.${id}` },
      { tags },
    );
    if (rows.length === 0) {
      throw new NotFoundError("没有找到这条收藏记录。");
    }

    return NextResponse.json({ favorite: toFavoriteSentence(rows[0]) });
  } catch (error) {
    return jsonError(error, "修改标签失败。");
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);

    // 幂等：删一个本来就不存在的收藏，对用户来说结果一样是「没了」
    const rows = await deleteRows<{ id: string }>("favorites", { id: `eq.${id}` });

    return NextResponse.json({ deleted: true, removed: rows.length });
  } catch (error) {
    return jsonError(error, "取消收藏失败。");
  }
}

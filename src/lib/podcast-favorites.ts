/**
 * 收藏的数据访问层。
 *
 * 与 podcast-history.ts 同一套思路：云端是权威，localStorage 只是缓存。
 * 差别在于收藏是**用户主动产生**的数据，所以写入必须先打到服务端再算数 ——
 * 不能本地先记、指望以后再同步：用户换台设备打开就该看到同样的收藏。
 *
 * ── 幂等从哪来 ──
 * id 由 `buildFavoriteId(podcastId, start)` 生成，形如 `<uuid>:<M:SS>`。
 * 同一句点两次收藏，第二次是 upsert 覆盖同一行，不需要任何去重逻辑。
 */

import type { FavoriteSentence } from "@/types/podcast";
import { apiRequest } from "@/lib/api";

/** 键名带 v2：旧缓存里的 id 是 FastAPI 的 job_id，与云端 uuid 对不上。 */
const CACHE_KEY = "podcast-favorites-cache-v2";

export function buildFavoriteId(podcastId: string, start: string): string {
  return `${podcastId}:${start}`;
}

function sortByDate(items: FavoriteSentence[]): FavoriteSentence[] {
  return [...items].sort(
    (left, right) => new Date(right.createdAt).getTime() - new Date(left.createdAt).getTime(),
  );
}

function readCache(): FavoriteSentence[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) {
      return [];
    }
    return sortByDate(JSON.parse(raw) as FavoriteSentence[]);
  } catch {
    return [];
  }
}

function writeCache(items: FavoriteSentence[]): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(sortByDate(items)));
  } catch {
    // 配额满了也不抛：缓存写不进去不影响功能
  }
}

/** 同步读缓存。首屏与「当前句子是否已收藏」用得到。 */
export function loadPodcastFavorites(): FavoriteSentence[] {
  return readCache();
}

/** 同步判断某句是否已收藏 —— 收藏按钮的样式要在渲染时立刻决定，不能等异步。 */
export function hasPodcastFavorite(id: string): boolean {
  return readCache().some((item) => item.id === id);
}

// ══════════════════════════════════════════════════════════════════════
//  服务端
// ══════════════════════════════════════════════════════════════════════

export async function fetchPodcastFavorites(): Promise<FavoriteSentence[]> {
  const data = await apiRequest<{ favorites: FavoriteSentence[] }>("/api/favorites");
  const items = sortByDate(data.favorites ?? []);
  writeCache(items);
  return items;
}

export async function createPodcastFavorite(
  payload: FavoriteSentence,
): Promise<FavoriteSentence> {
  const data = await apiRequest<{ favorite: FavoriteSentence }>("/api/favorites", {
    method: "POST",
    body: JSON.stringify({
      id: payload.id,
      podcastId: payload.podcastId,
      podcastTitle: payload.podcastTitle,
      start: payload.start,
      end: payload.end,
      original: payload.original,
      translation: payload.translation,
      tags: payload.tags ?? [],
    }),
  });

  const favorite = data.favorite;
  writeCache([favorite, ...readCache().filter((item) => item.id !== favorite.id)]);
  return favorite;
}

export async function deletePodcastFavorite(id: string): Promise<void> {
  await apiRequest(`/api/favorites/${encodeURIComponent(id)}`, { method: "DELETE" });
  writeCache(readCache().filter((item) => item.id !== id));
}

export async function updatePodcastFavoriteTags(
  id: string,
  tags: string[],
): Promise<FavoriteSentence> {
  const data = await apiRequest<{ favorite: FavoriteSentence }>(
    `/api/favorites/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify({ tags }) },
  );

  const favorite = data.favorite;
  writeCache(readCache().map((item) => (item.id === favorite.id ? favorite : item)));
  return favorite;
}

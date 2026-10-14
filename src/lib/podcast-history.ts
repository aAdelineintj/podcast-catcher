/**
 * 播客历史的数据访问层。
 *
 * ── 阶段二的定位变化 ──
 * 迁移到 Supabase 之前，localStorage 是**唯一数据源**。现在云端是权威，
 * localStorage 降级成**首屏缓存**：
 *
 *   · 首屏：同步读缓存立刻渲染，避免侧栏先空一下再填上内容
 *   · 随后：`fetchPodcastHistory()` 拉云端数据覆盖缓存
 *   · 写入：一律先打服务端，成功了再更新缓存
 *
 * ── 为什么写入不能「本地先写、后台同步」──
 * 播客的状态（status / progress / segments）是 worker 在服务端写的。
 * 如果前端也往同一个方向写，两边就会互相覆盖 —— 典型症状是轮询把
 * worker 刚写好的 `success` 冲回 `processing`，任务看起来永远跑不完。
 * 所以这里的规则很简单：**状态的唯一写入方是 worker，前端只读**。
 */

import type { StoredPodcast } from "@/types/podcast";
import { apiRequest } from "@/lib/api";

/**
 * 缓存键换了名字（原来叫 `podcast-history`）。
 *
 * 这是刻意的：旧缓存里的 id 是 FastAPI 的 job_id，而新数据的 id 是
 * Supabase 的 uuid，两者对不上。沿用同一个键会让首屏拿旧 id 去请求详情，
 * 得到一堆 404。换个键等于自动作废旧缓存。
 */
const CACHE_KEY = "podcast-history-cache-v2";

function sortByDate(items: StoredPodcast[]): StoredPodcast[] {
  return [...items].sort(
    (left, right) => new Date(right.date).getTime() - new Date(left.date).getTime(),
  );
}

function readCache(): StoredPodcast[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) {
      return [];
    }
    return sortByDate(JSON.parse(raw) as StoredPodcast[]);
  } catch {
    // 缓存坏了不该让页面白屏 —— 直接当没有缓存，等云端数据
    return [];
  }
}

function writeCache(items: StoredPodcast[]): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(sortByDate(items)));
  } catch {
    // localStorage 满了（Safari 隐私模式下配额极小）也不该抛出去 ——
    // 缓存只是优化，写不进去不影响功能
  }
}

/** 同步读缓存。只用于首屏渲染，**不要**拿它当权威数据。 */
export function loadPodcastHistory(): StoredPodcast[] {
  return readCache();
}

export function getPodcastHistoryItem(id: string): StoredPodcast | null {
  return readCache().find((item) => item.id === id) ?? null;
}

/**
 * 把单条写进缓存。
 *
 * 详情接口返回的数据带 segments，而列表接口不带。缓存里保留 segments
 * 是为了让 FavoritesView 能推算收藏句子的结束时间（它需要查同播客的
 * 下一条字幕），所以合并时**不能**用空 segments 覆盖已有的。
 */
export function cachePodcastItem(item: StoredPodcast): void {
  const current = readCache();
  const previous = current.find((entry) => entry.id === item.id);

  const merged: StoredPodcast =
    previous && previous.segments.length > 0 && item.segments.length === 0
      ? { ...item, segments: previous.segments }
      : item;

  writeCache([merged, ...current.filter((entry) => entry.id !== item.id)]);
}

export function removeCachedPodcast(id: string): void {
  writeCache(readCache().filter((item) => item.id !== id));
}

// ══════════════════════════════════════════════════════════════════════
//  服务端
// ══════════════════════════════════════════════════════════════════════

/**
 * 拉取历史列表并刷新缓存。
 *
 * 列表接口不含 segments，所以合并时要把缓存里已有的字幕保留下来 ——
 * 否则用户从详情页返回首页，字幕就没了（再点进去又要重新等一轮）。
 */
export async function fetchPodcastHistory(): Promise<StoredPodcast[]> {
  const data = await apiRequest<{ podcasts: StoredPodcast[] }>("/api/podcasts");
  const incoming = data.podcasts ?? [];

  const cached = new Map(readCache().map((item) => [item.id, item]));
  const merged = incoming.map((item) => {
    const previous = cached.get(item.id);
    if (previous && previous.segments.length > 0 && item.segments.length === 0) {
      return { ...item, segments: previous.segments };
    }
    return item;
  });

  writeCache(merged);
  return sortByDate(merged);
}

/** 拉取单集详情（含全部字幕），并写进缓存。轮询就靠它。 */
export async function fetchPodcastDetail(id: string): Promise<StoredPodcast> {
  const data = await apiRequest<{ podcast: StoredPodcast }>(
    `/api/podcasts/${encodeURIComponent(id)}`,
  );
  cachePodcastItem(data.podcast);
  return data.podcast;
}

/** 改标题。服务端是唯一真相，成功后同步缓存。 */
export async function renamePodcast(id: string, title: string): Promise<StoredPodcast> {
  const data = await apiRequest<{ podcast: StoredPodcast }>(
    `/api/podcasts/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify({ title }) },
  );
  cachePodcastItem(data.podcast);
  return data.podcast;
}

/** 删除整集（服务端会连 Storage 里的音频一起清掉）。 */
export async function deletePodcast(id: string): Promise<void> {
  await apiRequest(`/api/podcasts/${encodeURIComponent(id)}`, { method: "DELETE" });
  removeCachedPodcast(id);
}

/**
 * 数据库行 ↔ 前端类型的映射。
 *
 * **为什么要单独一层**：数据库用 snake_case、前端用 camelCase，
 * 且几个字段的语义并不一一对应（比如 `created_at` 在界面上叫 `date`）。
 * 把这层映射集中在两个函数里，比散落在各个 Route Handler 里可靠得多 ——
 * 将来加字段只改一处，不会出现「列表接口给了新字段、详情接口忘了给」。
 */

import type {
  FavoriteSentence,
  SourceLanguage,
  StoredPodcast,
  TranscriptSegment,
} from "@/types/podcast";

// ══════════════════════════════════════════════════════════════════════
//  行类型
// ══════════════════════════════════════════════════════════════════════

export type PodcastRow = {
  id: string;
  title: string;
  filename: string;
  source_language: string;
  transcription_language: string | null;
  duration_label: string | null;
  duration_seconds: number | null;
  status: string;
  stage: string;
  progress: number;
  translated_count: number;
  total_segments: number;
  error_message: string | null;
  audio_path: string | null;
  audio_bytes: number | null;
  audio_uploaded_at: string | null;
  created_at: string;
};

export type SegmentRow = {
  podcast_id: string;
  idx: number;
  start_label: string;
  start_seconds: number;
  end_seconds: number | null;
  original: string;
  detected_lang: string | null;
  detection_reason: string | null;
  translation: string | null;
  translation_status: string;
};

export type FavoriteRow = {
  id: string;
  podcast_id: string | null;
  podcast_title: string;
  start_label: string;
  end_label: string | null;
  original: string;
  translation: string | null;
  tags: string[] | null;
  created_at: string;
};

// ══════════════════════════════════════════════════════════════════════
//  查询字段清单
// ══════════════════════════════════════════════════════════════════════

/** 列表接口不取 segments —— 一集 400 行字幕会让列表响应膨胀到几百 KB。 */
export const PODCAST_LIST_SELECT = [
  "id", "title", "filename", "source_language", "transcription_language",
  "duration_label", "duration_seconds", "status", "stage", "progress",
  "translated_count", "total_segments", "error_message",
  "audio_path", "audio_bytes", "audio_uploaded_at", "created_at",
].join(",");

export const PODCAST_DETAIL_SELECT = PODCAST_LIST_SELECT;

export const SEGMENT_SELECT = [
  "idx", "start_label", "original", "translation",
  "translation_status", "detected_lang", "detection_reason",
].join(",");

export const FAVORITE_SELECT = [
  "id", "podcast_id", "podcast_title", "start_label", "end_label",
  "original", "translation", "tags", "created_at",
].join(",");

// ══════════════════════════════════════════════════════════════════════
//  语种
// ══════════════════════════════════════════════════════════════════════

export const SOURCE_LANGUAGES = ["auto", "fr", "en"] as const;

/**
 * 把任意输入收敛到合法语种。
 *
 * 这个校验必须做：`podcasts.source_language` 是 `text` 而不是 enum，
 * 数据库不会拦下 "zh" 这类值；一旦写进去，worker 的
 * `normalize_source_language` 会把它当 auto 处理，用户选了中文却得到自动识别，
 * 而且没有任何报错。
 */
export function normalizeSourceLanguage(value: unknown): SourceLanguage {
  return typeof value === "string" && (SOURCE_LANGUAGES as readonly string[]).includes(value)
    ? (value as SourceLanguage)
    : "auto";
}

// ══════════════════════════════════════════════════════════════════════
//  映射
// ══════════════════════════════════════════════════════════════════════

/**
 * 音频播放地址。
 *
 * **刻意不返回 Supabase 的签名 URL**：签名 URL 一小时就过期，
 * 而用户完全可能把页面开着去吃饭，回来一按播放就 403。
 * 返回这个稳定的站内地址，由 `/api/podcasts/[id]/media` 每次现签再 302 ——
 * 链接永不过期，密钥也永远不出服务端。
 */
export function mediaPathFor(podcastId: string): string {
  return `/api/podcasts/${podcastId}/media`;
}

export function toTranscriptSegment(row: SegmentRow): TranscriptSegment {
  return {
    start: row.start_label,
    original: row.original,
    translation: row.translation ?? "",
    translation_status: row.translation_status,
    detected_lang: row.detected_lang ?? undefined,
    detection_reason: row.detection_reason ?? undefined,
  };
}

export function toStoredPodcast(
  row: PodcastRow,
  segments: TranscriptSegment[] = [],
): StoredPodcast {
  return {
    id: row.id,
    title: row.title,
    // 前端按 date 倒序排 History，所以直接给 ISO 时间戳（旧版给的是
    // toLocaleString 结果，能被 new Date() 解析但时区语义含糊）
    date: row.created_at,
    duration: row.duration_label ?? "0:00",
    // 音频还没传完时 audio_path 为 NULL，此时不给播放地址 ——
    // 否则播放器会去请求一个必然 404 的地址，报出看不懂的错误
    mediaUrl: row.audio_path ? mediaPathFor(row.id) : null,
    status: row.status,
    stage: row.stage,
    progress: row.progress,
    translatedCount: row.translated_count,
    totalSegments: row.total_segments,
    sourceLanguage: normalizeSourceLanguage(row.source_language),
    transcriptionLanguage: row.transcription_language,
    errorMessage: row.error_message,
    segments,
  };
}

/**
 * `livePodcastTitle` 是联查 `podcasts(title)` 带出来的实时标题。
 *
 * 优先用它：用户把播客改名之后，收藏列表里也该跟着变 ——
 * 否则会出现「首页显示新名字、收藏夹还挂着旧名字」的割裂感。
 * 只有播客被删了（外键 on delete set null，联查不到）才回落到
 * 收藏时快照的 `podcast_title`。这正是当初冗余存一份的目的：
 * 不是为了让名字实时，而是为了播客没了之后收藏还能显示个名字。
 */
export function toFavoriteSentence(
  row: FavoriteRow,
  livePodcastTitle?: string | null,
): FavoriteSentence {
  return {
    id: row.id,
    podcastId: row.podcast_id ?? "",
    podcastTitle: livePodcastTitle || row.podcast_title,
    mediaUrl: row.podcast_id ? mediaPathFor(row.podcast_id) : null,
    start: row.start_label,
    end: row.end_label ?? undefined,
    original: row.original,
    translation: row.translation ?? "",
    tags: row.tags ?? [],
    createdAt: row.created_at,
  };
}

// ══════════════════════════════════════════════════════════════════════
//  对象路径
// ══════════════════════════════════════════════════════════════════════

/** 允许的音频扩展名。限定白名单，避免用户传 `x.mp3/../../etc` 这类路径。 */
const ALLOWED_AUDIO_EXTENSIONS = [".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".mp4"];

/**
 * 从原始文件名推出 Storage 里的对象路径：`<podcast_id><ext>`。
 *
 * 用 podcast id 而不是原始文件名，有两个原因：
 *  1. 文件名可能重复（两个人各传一个 `audio.mp3`），而 id 全局唯一
 *  2. 文件名可能含中文、空格、`/` —— 直接当对象路径要处理一堆编码问题
 * 扩展名保留是必要的：worker 要靠它判断该用什么解码器。
 */
export function buildAudioObjectPath(podcastId: string, filename: string): string {
  const dotIndex = filename.lastIndexOf(".");
  const rawExt = dotIndex >= 0 ? filename.slice(dotIndex).toLowerCase() : "";
  const ext = ALLOWED_AUDIO_EXTENSIONS.includes(rawExt) ? rawExt : ".mp3";
  return `${podcastId}${ext}`;
}

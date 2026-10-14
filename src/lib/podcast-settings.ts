import type { SourceLanguage } from "@/types/podcast";

const STORAGE_KEY = "podcast-source-language";
// 旧版本把默认语种写成 fr 并持久化，导致英法混排内容里的英语段落被法语引擎硬转成乱码
// （实测：`Hello once again and welcome to learn French by podcast.` →
// `ChartPdcast Witer Riiser, Famocaiann Porttuor, Spybrady in Los Angeles.`）。
// 用版本号区分「用户主动选择」与「旧默认值遗留」，升级时丢弃一次旧值。
const VERSION_KEY = "podcast-source-language-version";
const STORAGE_VERSION = 2;

/**
 * 首次进入的默认语种。
 *
 * 必须是 auto：强制指定语种会把另一种语言的段落交给错误引擎，输出乱码**且不报错**，
 * 页面上有字幕有译文、看起来是成功的，属于最隐蔽的一类问题。
 * 只有内容确实单一语种、且明确知道是哪一种时，才让用户手动切到 fr / en 省额度。
 */
export const DEFAULT_SOURCE_LANGUAGE: SourceLanguage = "auto";

const VALID_LANGUAGES: readonly SourceLanguage[] = ["fr", "en", "auto"];

export function loadSourceLanguage(): SourceLanguage {
  if (typeof window === "undefined") {
    return DEFAULT_SOURCE_LANGUAGE;
  }

  try {
    // 旧版本遗留值一次性作废：无法区分「用户选的 fr」和「旧默认值 fr」，
    // 而误用 fr 的代价是整段乱码，所以宁可让用户重选一次。
    if (window.localStorage.getItem(VERSION_KEY) !== String(STORAGE_VERSION)) {
      window.localStorage.removeItem(STORAGE_KEY);
      window.localStorage.setItem(VERSION_KEY, String(STORAGE_VERSION));
      return DEFAULT_SOURCE_LANGUAGE;
    }

    const raw = window.localStorage.getItem(STORAGE_KEY);
    return VALID_LANGUAGES.includes(raw as SourceLanguage)
      ? (raw as SourceLanguage)
      : DEFAULT_SOURCE_LANGUAGE;
  } catch {
    return DEFAULT_SOURCE_LANGUAGE;
  }
}

export function saveSourceLanguage(value: SourceLanguage) {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.setItem(STORAGE_KEY, value);
    window.localStorage.setItem(VERSION_KEY, String(STORAGE_VERSION));
  } catch {
    // 隐私模式等 localStorage 不可用时静默降级，不影响当次使用
  }
}

/**
 * 前端调用自己后端的统一入口。
 *
 * `NEXT_PUBLIC_API_BASE` 是为「前端与后端不同源」的场景留的（比如把
 * Next 静态导出到 CDN、API 单独部署）。本项目前后端同源，所以默认为空串 ——
 * 这样 `apiUrl("/api/podcasts")` 就是 `/api/podcasts`，走相对路径。
 */

const RAW_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export const API_BASE = RAW_BASE.replace(/\/+$/, "");

export const apiUrl = (path: string) => {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
};

/** 服务端错误响应的形状（见 src/lib/api-helpers.ts 的 jsonError）。 */
type ApiErrorBody = { error?: string };

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * 统一的请求包装：把 HTTP 状态和错误体翻译成一句能直接展示给用户的话。
 *
 * **为什么要统一**：直接 `fetch` 时，`response.json()` 在 404/500 上
 * 会抛 SyntaxError（因为错误响应可能不是 JSON），而 `!response.ok` 又
 * 只能给出一句「请求失败」—— 排查线上问题时这两点都很折磨。
 * 这里把服务端 `{ error: "..." }` 里的说明原样带出来，用户看到的就是
 * 「缺少环境变量 SUPABASE_URL」这种能直接动手的信息。
 */
export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;

  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: init.body
        ? { "Content-Type": "application/json", ...init.headers }
        : init.headers,
      // 轮询型请求，绝不能让浏览器或中间层缓存住
      cache: "no-store",
    });
  } catch {
    // fetch 只在网络层失败时抛异常（DNS、断网、被拦截），
    // HTTP 4xx/5xx 不会走到这里
    throw new ApiError(0, "无法连接到服务器，请检查网络后重试。");
  }

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = (await response.json()) as ApiErrorBody;
      if (body?.error) {
        message = body.error;
      }
    } catch {
      // 响应体不是 JSON（比如网关直接返回了 HTML 错误页），保留默认文案
    }
    throw new ApiError(response.status, message);
  }

  if (response.status === 204) {
    return null as T;
  }

  return (await response.json()) as T;
}

/**
 * 把文件直传到 Storage 的签名地址。
 *
 * **为什么绕过自己的后端**：Vercel Serverless 的请求体上限是 4.5MB，
 * 而播客音频几十 MB 很常见。走服务端中转会在传完之后才 413 失败，
 * 用户白等一场。签名 URL 让浏览器直连 Storage，
 * 服务端只负责签发（几百字节）和事后登记。
 *
 * 注意这里是 `PUT` 而不是 `POST`，且**不能**用 `apiRequest` ——
 * 目标不是自己的后端，响应体也不是 JSON，成功时可能是空的。
 */
export async function uploadFileToSignedUrl(uploadUrl: string, file: File): Promise<void> {
  let response: Response;

  try {
    response = await fetch(uploadUrl, {
      method: "PUT",
      headers: {
        // 不设 Content-Type 的话 Storage 会按 application/octet-stream 存，
        // 播放时浏览器可能拒绝解码
        "Content-Type": file.type || "audio/mpeg",
      },
      body: file,
    });
  } catch {
    throw new ApiError(0, "音频上传失败：无法连接到存储服务，请检查网络。");
  }

  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.text()).slice(0, 200);
    } catch {
      // 拿不到响应体就算了，状态码本身已经够定位问题
    }
    throw new ApiError(
      response.status,
      `音频上传失败（HTTP ${response.status}）。${detail}`.trim(),
    );
  }
}

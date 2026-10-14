/**
 * 服务端 Supabase 客户端：PostgREST + Storage REST，只用原生 fetch。
 *
 * ⚠️ **只能在 Route Handlers / Server Components 里 import**。
 *
 * 这里持有 service_role key，而本项目的 RLS 是「开启 + 零策略」——
 * 也就是说这把钥匙能读写整库，没有任何行级限制。它一旦进入
 * Client Component 的依赖图就会被 Next 打进浏览器包，等于把数据库公开。
 * 所以：**永远不要加 `NEXT_PUBLIC_` 前缀，永远不要从客户端组件 import 本文件。**
 *
 * 文件末尾有一道运行时守卫兜底。本来该用 `server-only` 包，
 * 但为了不引入额外依赖（本项目只装了 8 个包），用零依赖的方式等价实现。
 *
 * ── 为什么不用 `@supabase/supabase-js` ──
 * 见 backend/worker/supabase_client.py 顶部注释：本项目只需要 6 个端点，
 * 而官方 SDK 会拖入 gotrue / storage3 / realtime 等一整串依赖。
 * 服务端直接打 REST 反而更透明 —— 每个请求的形状都写在代码里。
 */

/** Storage 里的 bucket 名。默认值兜底，避免少配一个变量就整站 500。 */
const DEFAULT_BUCKET = "podcast-audio";

/** 签名下载 URL 的有效期。太短会在用户暂停后失效，太长等于给了长效直链。 */
const SIGNED_URL_TTL_SECONDS = 60 * 60; // 1 小时

export class SupabaseError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, method: string, path: string, body: string) {
    super(`[${status}] ${method} ${path}\n${body.slice(0, 600)}`);
    this.name = "SupabaseError";
    this.status = status;
    this.body = body;
  }
}

/** 配置类错误单独分出来：这类错误该给用户看修复指引，而不是当成 500 吞掉。 */
export class SupabaseConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SupabaseConfigError";
  }
}

type SupabaseConfig = { url: string; key: string; bucket: string };

let cachedConfig: SupabaseConfig | null = null;

/**
 * 读取并校验配置。
 *
 * **刻意在函数内读 `process.env` 而不是模块顶层**：模块顶层会在
 * `next build` 阶段就被求值，那时环境变量可能还没注入，
 * 结果是把 undefined 固化进产物。运行时读就没有这个问题。
 */
export function getSupabaseConfig(): SupabaseConfig {
  if (cachedConfig) {
    return cachedConfig;
  }

  const url = process.env.SUPABASE_URL?.trim().replace(/\/+$/, "");
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY?.trim();
  const bucket = process.env.SUPABASE_STORAGE_BUCKET?.trim() || DEFAULT_BUCKET;

  const missing: string[] = [];
  if (!url) missing.push("SUPABASE_URL");
  if (!key) missing.push("SUPABASE_SERVICE_ROLE_KEY");
  if (missing.length > 0) {
    throw new SupabaseConfigError(
      `缺少环境变量：${missing.join("、")}。` +
        `本地请写进项目根的 .env；Vercel 请到 Project Settings → Environment Variables 配置。` +
        `注意这两个变量都不能带 NEXT_PUBLIC_ 前缀。`,
    );
  }

  // 这个坑踩过一次：Supabase 控制台有个 "REST URL" 栏显示的是
  // https://xxx.supabase.co/rest/v1/，从那里复制会得到带后缀的地址，
  // 拼接后变成 /rest/v1/rest/v1/podcasts → 404，而报错信息完全指不到原因。
  if (url!.includes("/rest/v1") || url!.includes("/storage/v1")) {
    throw new SupabaseConfigError(
      `SUPABASE_URL 只能填项目根地址（形如 https://xxxx.supabase.co）。` +
        `当前值带了 /rest/v1 或 /storage/v1 后缀，拼接后路径会重复而 404。` +
        `请在控制台 Project Settings → Data API 里取 "Project URL"。`,
    );
  }

  cachedConfig = { url: url!, key: key!, bucket };
  return cachedConfig;
}

/** 让测试能重置缓存（也用于配置错误后重新读取）。 */
export function resetSupabaseConfigCache(): void {
  cachedConfig = null;
}

function buildHeaders(
  config: SupabaseConfig,
  extra?: Record<string, string>,
): Record<string, string> {
  return {
    apikey: config.key,
    Authorization: `Bearer ${config.key}`,
    ...extra,
  };
}

/**
 * 所有请求的唯一出口。
 *
 * `cache: "no-store"` 是**必需的**：Next 会把服务端 fetch 的结果放进
 * Data Cache，而本项目大量使用轮询（每 2 秒问一次任务进度）。
 * 一旦被缓存，用户会永远看到第一次的 `progress: 0`，
 * 且刷新页面也不恢复 —— 这类 bug 排查起来非常费劲。
 */
async function request(
  method: string,
  path: string,
  init: { body?: string; headers?: Record<string, string> } = {},
): Promise<Response> {
  const config = getSupabaseConfig();
  const headers = buildHeaders(config, init.headers);

  // 只在**真有请求体**时才声明 Content-Type。
  //
  // 这个条件不是可有可无的：Storage 的 DELETE 端点是 Fastify 实现的，
  // 一旦看到 `Content-Type: application/json` 却没收到 body，会直接返回
  // 400 `Body cannot be empty when content-type is set to 'application/json'`。
  // 后果是「删除音频永远失败」—— 而删除失败在 Route Handler 里只打警告
  // 不中断（不该因为清不掉音频就不让用户删记录），于是 Storage 里
  // 会静默堆积删不掉的孤儿文件，直到某天发现配额满了。
  if (init.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${config.url}${path}`, {
    method,
    headers,
    body: init.body,
    cache: "no-store",
  });

  if (!response.ok) {
    throw new SupabaseError(response.status, method, path, await response.text());
  }
  return response;
}

async function requestJson<T>(
  method: string,
  path: string,
  init: { body?: string; headers?: Record<string, string> } = {},
): Promise<T> {
  const response = await request(method, path, init);
  const text = await response.text();
  return (text ? JSON.parse(text) : null) as T;
}

function encodeObjectPath(objectPath: string): string {
  // 逐段编码，保留 "/" 作为路径分隔符
  return objectPath.split("/").map(encodeURIComponent).join("/");
}

// ══════════════════════════════════════════════════════════════════════
//  PostgREST
// ══════════════════════════════════════════════════════════════════════

/**
 * 查询行。`query` 直接写 PostgREST 的运算符表达式。
 *
 * 例：`selectRows("podcasts", { id: "eq.xxx", order: "created_at.desc", limit: "20" })`
 */
export async function selectRows<T = Record<string, unknown>>(
  table: string,
  query: Record<string, string> = {},
): Promise<T[]> {
  const search = new URLSearchParams({ select: "*", ...query });
  return requestJson<T[]>("GET", `/rest/v1/${table}?${search.toString()}`);
}

/** 查询单行，没有就返回 null（避免每个调用点都写 `rows[0] ?? null`）。 */
export async function selectOne<T = Record<string, unknown>>(
  table: string,
  query: Record<string, string> = {},
): Promise<T | null> {
  const rows = await selectRows<T>(table, { ...query, limit: "1" });
  return rows[0] ?? null;
}

/**
 * 插入（`onConflict` 给了就是 upsert）。
 *
 * `return=representation` 让 PostgREST 把写入后的整行回传 ——
 * 前端建完任务立刻要拿到 created_at 和默认值，省一次 GET。
 */
export async function insertRows<T = Record<string, unknown>>(
  table: string,
  rows: Record<string, unknown>[],
  options: { onConflict?: string } = {},
): Promise<T[]> {
  if (rows.length === 0) {
    return [];
  }

  const query = options.onConflict
    ? `?on_conflict=${encodeURIComponent(options.onConflict)}`
    : "";

  return requestJson<T[]>("POST", `/rest/v1/${table}${query}`, {
    body: JSON.stringify(rows),
    headers: {
      Prefer: options.onConflict
        ? "resolution=merge-duplicates,return=representation"
        : "return=representation",
    },
  });
}

/** 按条件更新，返回更新后的行。 */
export async function updateRows<T = Record<string, unknown>>(
  table: string,
  filters: Record<string, string>,
  values: Record<string, unknown>,
): Promise<T[]> {
  const search = new URLSearchParams(filters);
  return requestJson<T[]>("PATCH", `/rest/v1/${table}?${search.toString()}`, {
    body: JSON.stringify(values),
    headers: { Prefer: "return=representation" },
  });
}

/** 按条件删除。返回被删掉的行，便于调用方判断「到底删到了没有」。 */
export async function deleteRows<T = Record<string, unknown>>(
  table: string,
  filters: Record<string, string>,
): Promise<T[]> {
  const search = new URLSearchParams(filters);
  return requestJson<T[]>("DELETE", `/rest/v1/${table}?${search.toString()}`, {
    headers: { Prefer: "return=representation" },
  });
}

/** 调用数据库函数。`claim_next_podcast` / `touch_worker` 都走这里。 */
export async function callRpc<T = unknown>(
  name: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  return requestJson<T>("POST", `/rest/v1/rpc/${name}`, {
    body: JSON.stringify(params),
  });
}

// ══════════════════════════════════════════════════════════════════════
//  Storage
// ══════════════════════════════════════════════════════════════════════

export type SignedUpload = {
  /** Storage 里的对象路径，如 `<podcast_id>.mp3` */
  path: string;
  /** 浏览器可以直接 PUT 的完整地址（token 已含在 query 里） */
  uploadUrl: string;
  /** 上传完把这段回传，服务端据此落库 */
  token: string;
};

/**
 * 创建一个签名上传 URL，交给浏览器直传。
 *
 * **为什么必须直传**：Vercel Serverless 的请求体上限是 4.5MB，
 * 而播客音频动辄几十 MB。如果让文件先经过 Route Handler，
 * 大文件会直接 413 —— 而且是用户传完才失败，体验最差。
 * 走签名 URL 之后，文件从浏览器直接进 Storage，服务端只传几百字节的元数据。
 *
 * ── 两个容易踩的点 ──
 *  1. 响应字段是 **`url`**（不是 `signedUrl`）。Storage 的命名不太一致：
 *     创建上传签名返回 `url`，创建下载签名返回 `signedURL`（大写 URL）。
 *  2. 上传签名 URL 的过期时间**固定 2 小时，不可配置** —— 传 expiresIn 没用。
 *     实际影响不大：创建后前端立刻上传，不会真的等到过期。
 *
 * `x-upsert: true` 在**创建签名时**设置（不是上传时），
 * 决定这个 token 后续能否覆盖已存在的对象。
 */
export async function createSignedUploadUrl(objectPath: string): Promise<SignedUpload> {
  const config = getSupabaseConfig();
  const encoded = encodeObjectPath(objectPath);

  const data = await requestJson<{ url: string }>(
    "POST",
    `/storage/v1/object/upload/sign/${config.bucket}/${encoded}`,
    {
      body: "{}",
      headers: { "x-upsert": "true" },
    },
  );

  if (!data?.url) {
    throw new SupabaseError(
      500,
      "POST",
      `/storage/v1/object/upload/sign/${config.bucket}/${encoded}`,
      `响应里没有 url 字段：${JSON.stringify(data)}`,
    );
  }

  const token = new URL(data.url, config.url).searchParams.get("token") ?? "";

  return {
    path: objectPath,
    uploadUrl: `${config.url}/storage/v1${data.url}`,
    token,
  };
}

/**
 * 创建签名下载 URL。
 *
 * 桶是私有的，所以音频**不能**用 `getPublicUrl` 那套固定地址 ——
 * 那种地址要么 403，要么需要把桶设成 public（等于公网可读）。
 * 签名 URL 带 token、有有效期，链接失效即作废。
 */
export async function createSignedDownloadUrl(
  objectPath: string,
  expiresIn: number = SIGNED_URL_TTL_SECONDS,
): Promise<string> {
  const config = getSupabaseConfig();
  const encoded = encodeObjectPath(objectPath);

  const data = await requestJson<{ signedURL: string }>(
    "POST",
    `/storage/v1/object/sign/${config.bucket}/${encoded}`,
    { body: JSON.stringify({ expiresIn }) },
  );

  if (!data?.signedURL) {
    throw new SupabaseError(
      500,
      "POST",
      `/storage/v1/object/sign/${config.bucket}/${encoded}`,
      `响应里没有 signedURL 字段：${JSON.stringify(data)}`,
    );
  }

  return `${config.url}/storage/v1${data.signedURL}`;
}

/** 删除对象。删播客时顺手清掉音频，否则 Storage 会一直占着。 */
export async function deleteStorageObject(objectPath: string): Promise<void> {
  const config = getSupabaseConfig();
  const encoded = encodeObjectPath(objectPath);
  await request("DELETE", `/storage/v1/object/${config.bucket}/${encoded}`);
}

// ══════════════════════════════════════════════════════════════════════
//  运行时守卫（零依赖版 server-only）
// ══════════════════════════════════════════════════════════════════════

// 客户端组件里 `window` 是存在的，服务端渲染时不是。所以这段只会在
// 「本模块被打进浏览器包」时触发 —— 也就是密钥即将泄露的那一刻。
if (typeof window !== "undefined") {
  throw new Error(
    "supabase-server 持有 service_role key，禁止在客户端使用。" +
      "请把数据读取挪到 Route Handler，客户端通过 fetch('/api/...') 获取。",
  );
}

/**
 * 口令闸门（Next.js 16 的 proxy 约定，即原 middleware）。
 *
 * 背景：PocketBay 单容器部署会把服务暴露在公网，而 `/api/*` 背后是
 * 付费的腾讯云 ASR + 百度翻译密钥，任何人拿到地址都能直接刷我们的额度。
 * 这里用一个口令把**所有**入口（页面、`/api/*`、`/media/*`、`/docs`）挡在门外。
 *
 * 为什么用 Cookie 而不是请求头：
 *   前端 `src/lib/api.ts` 里 `API_BASE === ""`，所有请求都走同源相对路径，
 *   浏览器会自动带上同源 Cookie，因此前端代码**一行都不用改**。
 *   `<audio src="/media/...">` 这类媒体请求同理。
 *
 * 执行时机（这是本方案成立的前提）：
 *   Next 的请求生命周期是 headers → redirects → **proxy** → beforeFiles 重写
 *   → 文件系统路由。proxy 跑在 beforeFiles 之前，所以 `next.config.ts` 里
 *   转发给 FastAPI 的那些路径同样会先经过这里，不存在绕过口子。
 *
 * 注意：文件必须叫 `src/proxy.ts` 且导出名为 `proxy`。
 *   Next 16 已弃用 `middleware.ts`（构建时会打印 deprecation 警告），
 *   且 proxy 走 Node.js runtime，而 middleware 走 edge。
 *   参考：node_modules/next/dist/build/templates/middleware.js
 *        `const isProxy = page === '/proxy' || page === '/src/proxy'`
 *        `const handlerUserland = (isProxy ? mod.proxy : mod.middleware) || mod.default`
 */

import { NextResponse, type NextRequest } from "next/server";

// ─────────────────────────────────────────────────────────────
// 配置
// ─────────────────────────────────────────────────────────────

const COOKIE_NAME = "pc_auth";
const LOGIN_PATH = "/login";
const LOGOUT_PATH = "/logout";

/** 登录状态保留时长：30 天。自用工具，不用频繁重登。 */
const MAX_AGE_SECONDS = 60 * 60 * 24 * 30;

/** 放行的精确路径（其余一律拦）。 */
const PUBLIC_EXACT = new Set([LOGIN_PATH, LOGOUT_PATH, "/favicon.ico", "/robots.txt"]);

/**
 * 放行的路径前缀。
 *
 * 只放 `/_next/`：那是构建产物（JS chunk、字体、图片），对所有人内容一致，
 * 且不含任何密钥（密钥只在后端进程里）。把它也拦住只会让登录页依赖链变脆，
 * 却几乎没有安全收益。真正敏感的三类入口——页面、`/api/*`、`/media/*`——全部拦截。
 */
const PUBLIC_PREFIXES = ["/_next/"];

/**
 * 口令与签名密钥都从环境变量读，**运行时**读取。
 *
 * 已确认 Next 只内联 `NEXT_PUBLIC_*`（build/define-env.js:53
 * `getNextPublicEnvironmentVariables()`），因此这里不会在构建期被固化成空值，
 * PocketBay 运行时注入的 `.env` 能正常生效。
 */
const PASSPHRASE = process.env.PASSPHRASE ?? "";
const AUTH_SECRET = process.env.AUTH_SECRET ?? "";

/** 两项都配齐才算「已启用口令」。 */
const CONFIGURED = PASSPHRASE.length > 0 && AUTH_SECRET.length > 0;

// ─────────────────────────────────────────────────────────────
// 令牌：HMAC-SHA256(AUTH_SECRET, PASSPHRASE)
// ─────────────────────────────────────────────────────────────

/**
 * 用 HMAC 而不是直接存口令哈希，是为了让 Cookie 的值**无法离线爆破**：
 * 攻击者即使拿到 Cookie，也不知道 AUTH_SECRET，因此没法反推口令。
 * 顺带得到一个「踢下线」开关——换掉 AUTH_SECRET，所有已发出的 Cookie 立即失效。
 *
 * 用 Web Crypto（crypto.subtle）而非 node:crypto，两种 runtime 都能跑，
 * 将来若要改回 edge 也不用动这里。
 */
async function tokenFor(passphrase: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(AUTH_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, enc.encode(passphrase));
  return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** 进程内缓存正确令牌，避免每个请求都重算一次 HMAC。 */
let cachedToken: string | null = null;
async function authToken(): Promise<string> {
  if (cachedToken === null) cachedToken = await tokenFor(PASSPHRASE);
  return cachedToken;
}

/** 定长比较，避免用响应时间泄露「前几位猜对了」。 */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ─────────────────────────────────────────────────────────────
// 登录失败限速
// ─────────────────────────────────────────────────────────────

const WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILURES = 10;
/** 上限保护：公网端点不能无界增长，超过就清一遍过期记录。 */
const MAX_TRACKED_IPS = 5000;

const failures = new Map<string, number[]>();

function pruneFailures(now: number): void {
  if (failures.size <= MAX_TRACKED_IPS) return;
  for (const [ip, hits] of failures) {
    if (hits.every((t) => now - t >= WINDOW_MS)) failures.delete(ip);
  }
}

function isRateLimited(ip: string): boolean {
  const now = Date.now();
  pruneFailures(now);
  const hits = (failures.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
  failures.set(ip, hits);
  return hits.length >= MAX_FAILURES;
}

function recordFailure(ip: string): void {
  const now = Date.now();
  const hits = (failures.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
  hits.push(now);
  failures.set(ip, hits);
}

function clientIp(request: NextRequest): string {
  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) return forwarded.split(",")[0].trim();
  return request.headers.get("x-real-ip") ?? "unknown";
}

// ─────────────────────────────────────────────────────────────
// 工具
// ─────────────────────────────────────────────────────────────

/**
 * 只接受站内相对路径，挡掉 `//evil.com` 这类协议相对跳转（开放重定向）。
 * 登录成功后的回跳地址完全来自 URL，必须校验。
 */
function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const NO_STORE = { "cache-control": "no-store, must-revalidate" } as const;

function htmlResponse(body: string, status = 200): NextResponse {
  return new NextResponse(body, {
    status,
    headers: { "content-type": "text/html; charset=utf-8", ...NO_STORE },
  });
}

/** 口令没配好时**一律拒绝**（fail closed）。宁可打不开，也不能裸奔。 */
function configMissingResponse(): NextResponse {
  return htmlResponse(
    renderPage({
      title: "口令未配置",
      body: `<p class="hint">服务端没有读到 <code>PASSPHRASE</code> / <code>AUTH_SECRET</code>，
        出于安全考虑已拒绝所有访问。请检查部署环境变量后重新发布。</p>`,
    }),
    503,
  );
}

// ─────────────────────────────────────────────────────────────
// 登录页
// ─────────────────────────────────────────────────────────────

function renderPage(opts: { title: string; body: string }): string {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>PodcastCatcher</title>
<style>
  :root { --bg:#f8fafc; --card:#ffffff; --fg:#0f172a; --muted:#64748b; --line:#e2e8f0; --primary:#3b82f6; --danger:#dc2626; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0a0a0a; --card:#171717; --fg:#ededed; --muted:#94a3b8; --line:#2a2a2a; --danger:#f87171; }
  }
  * { box-sizing: border-box; }
  body {
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    padding:24px; background:var(--bg); color:var(--fg);
    font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  .card {
    width:100%; max-width:380px; background:var(--card); border:1px solid var(--line);
    border-radius:14px; padding:32px 28px; box-shadow:0 1px 3px rgba(15,23,42,.06), 0 12px 32px rgba(15,23,42,.06);
  }
  h1 { margin:0 0 6px; font-size:20px; font-weight:600; letter-spacing:-.01em; }
  .sub { margin:0 0 24px; font-size:13px; color:var(--muted); }
  label { display:block; font-size:13px; font-weight:500; margin-bottom:8px; color:var(--muted); }
  input[type=password] {
    width:100%; padding:11px 13px; font-size:15px; color:var(--fg); background:var(--bg);
    border:1px solid var(--line); border-radius:9px; outline:none; transition:border-color .15s, box-shadow .15s;
  }
  input[type=password]:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(59,130,246,.15); }
  button {
    width:100%; margin-top:18px; padding:11px; font-size:15px; font-weight:500; color:#fff;
    background:var(--primary); border:0; border-radius:9px; cursor:pointer; transition:opacity .15s;
  }
  button:hover { opacity:.9; }
  .hint { margin:0; font-size:13px; line-height:1.7; color:var(--muted); }
  .error {
    margin:0 0 18px; padding:10px 12px; font-size:13px; color:var(--danger);
    background:rgba(220,38,38,.08); border:1px solid rgba(220,38,38,.2); border-radius:8px;
  }
  code { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; background:var(--bg); padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
  <main class="card">
    <h1>PodcastCatcher</h1>
    ${opts.body}
  </main>
</body>
</html>`;
}

function loginPage(opts: { next: string; error: string | null; subtitle?: string }): string {
  return renderPage({
    title: "口令",
    body: `
    <p class="sub">${opts.subtitle ?? "这是个人自用工具，请输入口令继续。"}</p>
    ${opts.error ? `<p class="error">${escapeHtml(opts.error)}</p>` : ""}
    <form method="POST" action="${LOGIN_PATH}" autocomplete="off">
      <label for="pass">访问口令</label>
      <input id="pass" name="pass" type="password" required autofocus autocomplete="current-password">
      <input type="hidden" name="next" value="${escapeHtml(opts.next)}">
      <button type="submit">进入</button>
    </form>`,
  });
}

// ─────────────────────────────────────────────────────────────
// /login
// ─────────────────────────────────────────────────────────────

async function handleLogin(request: NextRequest): Promise<NextResponse> {
  if (!CONFIGURED) return configMissingResponse();

  const url = request.nextUrl;

  if (request.method === "GET" || request.method === "HEAD") {
    const cookie = request.cookies.get(COOKIE_NAME)?.value ?? "";
    if (cookie && safeEqual(cookie, await authToken())) {
      return NextResponse.redirect(new URL(safeNext(url.searchParams.get("next")), url.origin));
    }
    return htmlResponse(loginPage({ next: safeNext(url.searchParams.get("next")), error: null }));
  }

  if (request.method !== "POST") {
    return new NextResponse("Method Not Allowed", {
      status: 405,
      headers: { allow: "GET, POST", ...NO_STORE },
    });
  }

  const ip = clientIp(request);
  if (isRateLimited(ip)) {
    return htmlResponse(
      loginPage({ next: "/", error: "尝试次数过多，请 10 分钟后再试。" }),
      429,
    );
  }

  // 表单以 application/x-www-form-urlencoded 提交。
  // 这里读原始文本再自行解析，而不是用 request.formData()——少一层对
  // proxy 请求体解析能力的依赖，行为更可预期。
  let submitted = "";
  let next = "/";
  try {
    const params = new URLSearchParams(await request.text());
    submitted = params.get("pass") ?? "";
    next = safeNext(params.get("next"));
  } catch {
    // 请求体读不出来就当空口令处理，走下面的失败分支。
  }

  if (submitted.length > 0 && safeEqual(await tokenFor(submitted), await authToken())) {
    failures.delete(ip);
    const response = NextResponse.redirect(new URL(next, url.origin), 303);
    response.cookies.set({
      name: COOKIE_NAME,
      value: await authToken(),
      httpOnly: true,
      sameSite: "lax",
      secure: url.protocol === "https:",
      path: "/",
      maxAge: MAX_AGE_SECONDS,
    });
    return response;
  }

  recordFailure(ip);
  return htmlResponse(loginPage({ next, error: "口令不正确。" }), 401);
}

// ─────────────────────────────────────────────────────────────
// /logout
// ─────────────────────────────────────────────────────────────

function handleLogout(request: NextRequest): NextResponse {
  const url = request.nextUrl.clone();
  url.pathname = LOGIN_PATH;
  url.search = "";
  const response = NextResponse.redirect(url);
  response.cookies.set({
    name: COOKIE_NAME,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    secure: url.protocol === "https:",
    path: "/",
    maxAge: 0,
  });
  return response;
}

// ─────────────────────────────────────────────────────────────
// 入口
// ─────────────────────────────────────────────────────────────

export async function proxy(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  if (pathname === LOGIN_PATH) return handleLogin(request);
  if (pathname === LOGOUT_PATH) return handleLogout(request);
  if (PUBLIC_EXACT.has(pathname)) return NextResponse.next();
  if (PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) return NextResponse.next();

  if (!CONFIGURED) return configMissingResponse();

  const cookie = request.cookies.get(COOKIE_NAME)?.value ?? "";
  if (cookie && safeEqual(cookie, await authToken())) return NextResponse.next();

  // 未登录。浏览器直接导航过来（Accept 含 text/html）就送去登录页；
  // 前端的 fetch / <audio> 则给一个干净的 401 JSON，避免把 HTML 当成数据解析。
  if (request.method === "GET" && (request.headers.get("accept") ?? "").includes("text/html")) {
    const url = request.nextUrl.clone();
    url.pathname = LOGIN_PATH;
    url.search = "";
    url.searchParams.set("next", `${pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(url);
  }

  return NextResponse.json({ error: "unauthorized" }, { status: 401, headers: NO_STORE });
}

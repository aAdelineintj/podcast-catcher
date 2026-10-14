import path from "node:path";
import type { NextConfig } from "next";

/**
 * PocketBay 是单服务架构：公网入口只指向一个容器。
 * 因此前端与后端跑在同一个容器里，由 Next.js 把接口路径代理给本机 FastAPI。
 */
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

/**
 * 是否把请求转发给本机 FastAPI。
 *
 * ── 为什么需要这个开关 ──
 * 阶段二把数据搬上 Supabase 之后，前端改走 `src/app/api/**` 的 Route Handlers，
 * 不再需要 FastAPI。但本地那套后端**保留下来**（FastAPI + Whisper 的
 * 完整本地闭环在调试转写/翻译管线时仍然好用），所以转发能力不能直接删掉。
 *
 * 于是用一个显式开关隔离：
 *   · 本地要跑旧链路调试 → 在 .env 里设 `LOCAL_FASTAPI=1`
 *   · Vercel 上**不设**这个变量 → 不生成任何转发规则
 *
 * **默认关闭**（而不是默认开启）是刻意的：如果默认开着，
 * Vercel 上忘了设变量就会把 `/health` 之类的请求打到不存在的 127.0.0.1:8000，
 * 得到一堆 500，而错误信息完全指不到「这里有个开关忘了关」。
 */
const LOCAL_FASTAPI = process.env.LOCAL_FASTAPI === "1";

/**
 * 需要转发给 FastAPI 的路径 —— **全部是不带 `/api` 前缀的根路径**。
 *
 * backend/main.py 把同一个 router 同时挂到了 `/api` 和根路径两处：
 *     app.include_router(router, prefix="/api")
 *     app.include_router(router)
 * 所以只用根路径这套就够，功能完全等价。
 *
 * ── 为什么 `/api/:path*` 必须从这里删掉 ──
 * 本配置用的是 `beforeFiles` rewrite，它在**文件系统路由之前**执行。
 * 只要保留 `/api/:path*` 这条规则，`src/app/api/**` 下所有 Route Handlers
 * 都会被转发给 FastAPI 而永远不执行 —— 表现是线上 `/api/podcasts` 全 500，
 * 且 Next 不会给出任何「路由被遮蔽」的提示。
 */
const LOCAL_FASTAPI_PATHS = [
  "/health",
  "/docs",
  "/redoc",
  "/openapi.json",
  "/process-podcast",
  "/jobs/:path*",
  "/media/:path*",
];

/**
 * 显式钉死项目根目录。
 *
 * Next 靠「向上查找锁文件」自动推断工作区根，只要上层任何目录里存在
 * package-lock.json / pnpm-lock.yaml / yarn.lock / bun.lock(b)，就会被误判。
 * 本机 `/Users/mac/package-lock.json` 就是这种情况（家目录里误建过 npm 项目），
 * 导致 Next 把根推断成 `/Users/mac` —— 于是每次 dev/build 都刷警告，
 * 且模块解析范围与文件监听范围被放大到整个家目录。
 *
 * 显式设置后：解析范围收敛到本项目，缓存校验与文件监听开销都随之下降，
 * 产物追踪（outputFileTracingRoot）也不会再试图追踪项目外的文件。
 */
const projectRoot = __dirname;

const nextConfig: NextConfig = {
  turbopack: {
    root: projectRoot,
  },
  outputFileTracingRoot: path.join(projectRoot),
  async rewrites() {
    return {
      beforeFiles: LOCAL_FASTAPI
        ? LOCAL_FASTAPI_PATHS.map((path) => ({
            source: path,
            destination: `${BACKEND_ORIGIN}${path}`,
          }))
        : [],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default nextConfig;

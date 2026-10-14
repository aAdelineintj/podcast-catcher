# 播客精听

把一集英语/法语播客变成**可逐句精听的双语字幕**。

上传音频 → 本机 Whisper 转写 → 百度翻译 → 页面上双语对照播放，点任意一句即可跳转重听、复制、收藏。

**完全免费**：算力跑在自己电脑上，云端只用 Supabase 免费版的数据库与对象存储。

---

## 它是怎么做到免费的

转写是整条链路里最贵的一环（云端 ASR 按次计费）。这里把它挪到本机 CPU 上跑 Whisper，
云端只负责**存数据**和**派任务**：

```
┌──────────────┐   ① 建任务 + 签上传地址
│   浏览器      │ ──────────────────────────────┐
│  (Vercel)    │                               ▼
│              │   ② 音频直传（不经 Vercel）  ┌─────────────────┐
│              │ ──────────────────────────▶ │    Supabase     │
│              │                             │  · Postgres     │
│  ④ 轮询字幕   │ ◀────────────────────────── │  · 私有 Storage │
└──────────────┘   ③ 读任务 / 写字幕          └─────────────────┘
                                                      ▲  │
                                                      │  ▼
                                            ┌─────────────────────┐
                                            │  本机 worker 轮询    │
                                            │  Whisper 转写        │
                                            │  百度翻译            │
                                            │  压缩音频回传         │
                                            └─────────────────────┘
```

两个关键设计：

- **音频不经过 Vercel**。Vercel Serverless 的请求体上限是 4.5MB，一集播客动辄几十 MB。
  浏览器拿到签名上传地址后**直传 Supabase Storage**，绕开这个限制。
- **worker 用「租约 + 原子认领」抢任务**（`FOR UPDATE SKIP LOCKED` + 30 分钟租约），
  所以同一台机器可以开多个 worker，任务不会被重复处理。

---

## 核心特性

- **逐句精听**：点任意一句原文或译文即从该句起点重播；自动高亮并滚动到当前句
- **双语对照**：左右两列同句对齐，时间戳独立成列
- **收藏夹**：跨播客收集句子，支持打标签；改播客标题时收藏夹里的标题跟着变，
  删掉播客后收藏仍保留原句与快照标题
- **播放历史**：云端权威 + localStorage 首屏缓存，首屏不闪白
- **音频自动压缩**：转写完成后压到 64kbps 单声道回传（约为原体积 15%），
  免费版 1GB Storage 从「只够存 10~20 集」变成「够存上百集」

---

## 快速开始

### 前置

- Node.js 20.9+（Next 16 的要求）、Python 3.10+、`ffmpeg`（用于音频压缩，缺失会静默跳过压缩）
- 一个 Supabase 项目（免费版即可）

### 1. 初始化 Supabase

到 Supabase SQL Editor 里执行 [`supabase/schema.sql`](supabase/schema.sql)。它会建好：

- 4 张表：`podcasts` / `segments` / `favorites` / `worker_heartbeat`
- 2 个 RPC：`claim_next_podcast`（原子认领）、`touch_worker`（心跳）
- 1 个**私有** bucket：`podcast-audio`

> 安全模型是 **RLS 开启 + 零策略**：只有 `service_role` 能读写，浏览器端不持有任何 Supabase 密钥。
> 因此**不要**加 `NEXT_PUBLIC_SUPABASE_ANON_KEY`，也不要为「能连上」加 `using (true)` 的策略。

### 2. 配置环境变量

```sh
cp .env.example .env
```

必填 5 项：

| 变量 | 用途 |
|---|---|
| `SUPABASE_URL` | `https://<ref>.supabase.co`（**不要**带 `/rest/v1/` 后缀） |
| `SUPABASE_SERVICE_ROLE_KEY` | 服务端密钥，绕过 RLS，**绝不能提交进 git** |
| `SUPABASE_STORAGE_BUCKET` | `podcast-audio` |
| `PASSPHRASE` | 站点访问口令 |
| `AUTH_SECRET` | Cookie 签名密钥（`openssl rand -hex 32`） |

> `PASSPHRASE` / `AUTH_SECRET` 缺任一，`src/proxy.ts` 会 **fail-closed**：整站返回 503 且不报错。
> 这是刻意设计 —— 宁可打不开，也不要无口令暴露。

### 3. 跑起来

```sh
npm install
npm run dev            # 前端 http://localhost:3000

# 另开一个终端跑 worker（它才是真正干活的）
cd backend
../venv/bin/python -m worker.worker
```

worker 启动前建议先自检（**不需要 Supabase 账号**，全部打桩）：

```sh
../venv/bin/python -m worker.selftest
```

想让它常驻（开机自启、崩溃重拉）：

```sh
sh scripts/install-worker.sh install   # 另有 status / logs / uninstall
```

### 4. 部署到 Vercel

1. 导入仓库
2. **Build Command 改成 `npm run build:next`** —— 默认的 `npm run build` 走的是
   单容器部署脚本 `scripts/pb-build.sh`，在 Vercel 上会失败
3. 配置上面那 5 个环境变量。**不要**设 `LOCAL_FASTAPI` / `BACKEND_ORIGIN` /
   `NEXT_PUBLIC_*` 中的任何一个

---

## 项目结构

```
src/
  app/
    api/                     # 7 个 Route Handler，前端只跟这里说话
      podcasts/              # 列表 / 创建（建行 + 签上传地址）
        [id]/                # 详情（含字幕）/ 改名 / 删除
          uploaded/          # 标记「音频传完了」—— worker 此刻才可能认领
          media/             # 302 到现签 URL（不暴露长期链接）
      favorites/             # 收藏列表 / 创建
      health/                # worker 在线探针
    podcast/[id]/            # 精听页
  lib/
    supabase-server.ts       # 服务端客户端（持 service_role）
    podcast-rows.ts          # 行 ↔ 领域对象映射
  proxy.ts                   # 口令闸门
backend/
  pipeline.py                # 切句 / 翻译口径 —— 本地与云端**共用这一份**
  whisper_asr.py             # 本机 Whisper 封装
  worker/
    worker.py                # 轮询 → 转写 → 翻译 → 回写 → 压缩
    supabase_client.py       # PostgREST + Storage REST 直连（不依赖 supabase-py）
    selftest.py              # 37 项断言，全打桩
    e2e_cloud.py             # 真实云端联调脚本
supabase/schema.sql          # 建表 + RPC + bucket
PRD/架构决策记录.md           # 为什么这么设计、踩过哪些坑
```

---

## 值得一读的设计取舍

都记在 [`PRD/架构决策记录.md`](PRD/架构决策记录.md) 里，几条比较有意思的：

- **`/api/*` 永远不转发给本地 FastAPI**。`next.config.ts` 用的是 `beforeFiles` rewrite，
  它跑在文件系统路由**之前** —— 一旦转发 `/api/*`，所有 Route Handler 都会被静默遮蔽，
  Next 不会给任何提示。
- **本地 FastAPI 保留但用开关隔离**（`LOCAL_FASTAPI=1`，默认关闭）。调试转写/翻译管线时
  那套闭环仍然好用，但线上完全不需要它。
- **用 `audio_uploaded_at` 而非状态字段当上传开关**。它为 NULL 就代表「元数据建了行、
  但音频还没传完」，认领函数要求它非空 —— 于是上传中断的任务永远不会被 worker 误领。
- **不装 `supabase-py`**。worker 只用到 6 个端点，用 `requests` 直连 PostgREST + Storage REST
  比拖进一个 SDK 更可控，也更容易看懂每个请求的形状。

---

## 已知限制

- **单用户自用**，没有账号体系与配额系统
- **worker 必须开着**。转写算力在本机，电脑休眠时任务会停在「排队中」——
  页面上的 `/api/health` 探针就是为了把这件事讲清楚
- 首次上传后需要等 worker 轮询（默认 5 秒）才会开工
- 支持的容器：mp3 / m4a / wav / aac / ogg / flac / mp4（其余扩展名一律按 mp3 处理）。
  只取音轨，不解析视频画面

---

## 致谢与依赖

本机转写 [openai-whisper](https://github.com/openai/whisper) ·
持久化 [Supabase](https://supabase.com) ·
翻译 [百度翻译开放平台](https://fanyi-api.baidu.com) ·
前端 [Next.js](https://nextjs.org)

-- ═══════════════════════════════════════════════════════════════════════════
--  播客精听 —— Supabase schema（阶段二）
--
--  用法：Supabase Dashboard → 左侧 SQL Editor → New query → 整段粘贴 → Run。
--        本脚本可重复执行（全部 if not exists / on conflict do nothing / or replace）。
--
--  执行后请确认三件事：
--    1. Table Editor 里能看到 podcasts / segments / favorites / worker_heartbeat
--    2. Storage 里能看到 podcast-audio 这个 bucket，且它是 Private（不是 Public）
--    3. 每张表的 RLS 开关都是 Enabled，且 Policies 列表为空 —— 这是故意的，见文末
--
--  ⚠️ 不要把本文件里的任何密钥写进来。密钥只存在于 Vercel 环境变量与本地 .env。
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- 1. podcasts —— 一集播客 = 一行
--
--    同时承载两件事：给前端看的展示字段（StoredPodcast），
--    和给 worker 用的认领状态（locked_by / locked_at / attempts）。
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.podcasts (
  id                     uuid primary key default gen_random_uuid(),

  -- 展示字段
  title                  text not null,
  filename               text not null,
  source_language        text not null default 'auto',   -- 用户选的：auto / fr / en
  transcription_language text,                            -- ASR 实际判定出来的：fr / en / ...
  duration_label         text,                            -- "M:SS"，与 segments.start_label 同格式
  duration_seconds       numeric,

  -- 任务状态（与本地 FastAPI 的 /jobs/{id} 响应字段一一对应，前端不用改映射）
  status                 text not null default 'queued'
                         check (status in ('queued', 'processing', 'success', 'failed')),
  stage                  text not null default 'uploaded',
  progress               int  not null default 0,
  translated_count       int  not null default 0,
  total_segments         int  not null default 0,
  error_message          text,

  -- 音频（存在 Storage，不存 URL：signed URL 会过期，只存对象路径，用时现签）
  audio_path             text,          -- 如 "<podcast_id>.mp3"
  audio_bytes            bigint,
  -- ⚠️ 关键字段：NULL 表示「元数据已建行、但音频还没传完」。
  --    认领函数要求它非空，所以上传中断的任务永远不会被 worker 误领。
  audio_uploaded_at      timestamptz,

  -- worker 认领状态
  locked_by              text,
  locked_at              timestamptz,
  attempts               int not null default 0,

  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

-- 认领查询的支撑索引：按 status 过滤 + 按 created_at 排队
create index if not exists podcasts_claim_idx
  on public.podcasts (status, created_at)
  where audio_uploaded_at is not null;

-- ⚠️ 已建过表的库要**单独跑**下面这两句（`create table if not exists` 不会给已存在的表加约束）。
-- 先 drop 再 add，可重复执行。
--
-- 钉住一个不变量：`audio_uploaded_at` 非空就代表「音频已经传完」，
-- 那 `audio_path` 必须同时有值。正常流程下这两列由
-- `POST /api/podcasts/[id]/uploaded` 在同一次 PATCH 里一起写，天然一致；
-- 但历史上出现过「只写了 audio_uploaded_at」的手工行，被
-- `claim_next_podcast` 认领后 worker 直接抛
-- `RuntimeError: podcasts.audio_path 为空，无法下载音频`，
-- 白白烧掉 3 次 attempts。与其在认领函数里加判断（那样违规行会永远停在
-- 「排队中」，比明确失败更难排查），不如在数据层让它压根写不进来。
alter table public.podcasts
  drop constraint if exists podcasts_audio_upload_consistent;
alter table public.podcasts
  add  constraint podcasts_audio_upload_consistent
       check (audio_uploaded_at is null or audio_path is not null);


-- ───────────────────────────────────────────────────────────────────────────
-- 2. segments —— 字幕行
--
--    start_label 给前端显示与收藏 id 用；start_seconds / end_seconds 给排序与跳转用。
--    两个都存是刻意的：不要再让前端从 "M:SS" 字符串反推时间
--    （旧版「上一句/下一句」就是栽在这个反推上，见 PRD/学习的点.md）。
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.segments (
  podcast_id         uuid not null references public.podcasts(id) on delete cascade,
  idx                int  not null,                    -- 从 0 开始，决定字幕顺序

  start_label        text    not null,                 -- "M:SS"
  start_seconds      numeric not null,
  end_seconds        numeric,

  original           text    not null,
  detected_lang      text,                             -- 百度语种码：fra / en / auto
  detection_reason   text,                             -- 判定依据，排查误判时用
  translation        text,
  translation_status text not null default 'pending'
                     check (translation_status in ('pending', 'done', 'skipped')),

  primary key (podcast_id, idx)
);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. favorites —— 收藏的句子
--
--    id 沿用前端的 buildFavoriteId = `${podcastId}:${start}`，
--    这样同一句重复收藏天然幂等（upsert 即可），不需要额外去重逻辑。
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.favorites (
  id            text primary key,                      -- "<podcast_id>:<M:SS>"
  -- on delete set null 而不是 cascade：收藏是用户自己攒下的句子，
  -- 不该因为「原始播客被删了」就一起消失。删播客后 podcast_id 变 NULL，
  -- 句子文本仍完整可读 —— 这正是下面 podcast_title 冗余一份的意义。
  -- （如果用 cascade，冗余字段就成了摆设：行都没了，冗余给谁看。）
  podcast_id    uuid references public.podcasts(id) on delete set null,
  podcast_title text not null,                         -- 冗余一份：播客被删后收藏仍可读
  start_label   text not null,
  end_label     text,
  original      text not null,
  translation   text,
  tags          text[] not null default '{}',
  created_at    timestamptz not null default now()
);

create index if not exists favorites_created_idx
  on public.favorites (created_at desc);

-- 已经建过表的库要单独跑这一段：`create table if not exists` 遇到已存在的表
-- 会整段跳过，包括上面那条外键定义，所以旧库的 cascade 不会自动变成 set null。
-- 先 drop 再 add 是可重复执行的，跑几次都不会出错。
alter table public.favorites
  drop constraint if exists favorites_podcast_id_fkey,
  add  constraint favorites_podcast_id_fkey
       foreign key (podcast_id) references public.podcasts(id) on delete set null;


-- ───────────────────────────────────────────────────────────────────────────
-- 4. worker_heartbeat —— 本机 worker 的存活证明
--
--    两个作用：
--      ① 前端据此显示「worker 在线 / 离线」，而不是让用户对着「排队中」干等
--      ② 每 5s 一次的写入兼作 Supabase 保活 —— 免费项目 7 天无活动会休眠，
--         只要 worker 常驻就不会触发
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.worker_heartbeat (
  worker_id   text primary key,
  last_seen   timestamptz not null default now(),
  model       text,          -- 如 "small"，便于确认 worker 跑的是哪版配置
  current_job uuid,          -- 正在处理哪一集（空 = 空闲）
  hostname    text
);


-- ───────────────────────────────────────────────────────────────────────────
-- 5. updated_at 自动维护
-- ───────────────────────────────────────────────────────────────────────────

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists podcasts_touch_updated_at on public.podcasts;
create trigger podcasts_touch_updated_at
  before update on public.podcasts
  for each row execute function public.touch_updated_at();


-- ───────────────────────────────────────────────────────────────────────────
-- 6. claim_next_podcast —— 原子认领一个待处理任务
--
--    为什么必须用 FOR UPDATE SKIP LOCKED：
--    将来若开两个 worker（比如笔记本 + 台式机），朴素写法会让两边拿到同一行、
--    重复跑同一集音频、互相覆盖 segments。SKIP LOCKED 让第二个 worker 直接跳过
--    已加锁的行去取下一个，天然安全。
--
--    租约：locked_at 超过 30 分钟视为「上一个 worker 已经死了」，允许重抢。
--    这是 worker 断电/崩溃时唯一的自救机制，不能省。
-- ───────────────────────────────────────────────────────────────────────────

create or replace function public.claim_next_podcast(p_worker_id text)
returns setof public.podcasts
language sql
security definer
set search_path = public
as $$
  with candidate as (
    select id
      from public.podcasts
     where audio_uploaded_at is not null
       and attempts < 3
       and (
             status = 'queued'
             -- 租约超时：上一个 worker 大概率已经死了
             or (status = 'processing' and locked_at < now() - interval '30 minutes')
           )
     order by created_at
     for update skip locked
     limit 1
  )
  update public.podcasts p
     set status     = 'processing',
         stage      = 'transcribing',
         progress   = greatest(p.progress, 2),
         locked_by  = p_worker_id,
         locked_at  = now(),
         attempts   = p.attempts + 1,
         updated_at = now()
    from candidate c
   where p.id = c.id
  returning p.*;
$$;


-- ───────────────────────────────────────────────────────────────────────────
-- 7. touch_worker —— 心跳 upsert
-- ───────────────────────────────────────────────────────────────────────────

create or replace function public.touch_worker(
  p_worker_id   text,
  p_model       text default null,
  p_current_job uuid default null,
  p_hostname    text default null
)
returns void
language sql
security definer
set search_path = public
as $$
  insert into public.worker_heartbeat (worker_id, model, current_job, hostname, last_seen)
  values (p_worker_id, p_model, p_current_job, p_hostname, now())
  on conflict (worker_id) do update
     set last_seen   = now(),
         -- 允许传 null 表示「这次不带这个信息」，而不是把它清空
         model       = coalesce(excluded.model, public.worker_heartbeat.model),
         hostname    = coalesce(excluded.hostname, public.worker_heartbeat.hostname),
         current_job = excluded.current_job;
$$;


-- ───────────────────────────────────────────────────────────────────────────
-- 8. 私有 bucket
--
--    public = false 是重点：桶私有之后，音频只能通过服务端现签的
--    signed URL 访问，链接过期即失效。配合「浏览器端不持有任何 Supabase 密钥」，
--    公网上拿到域名也读不到别人的音频。
-- ───────────────────────────────────────────────────────────────────────────

insert into storage.buckets (id, name, public)
values ('podcast-audio', 'podcast-audio', false)
on conflict (id) do update set public = false;


-- ───────────────────────────────────────────────────────────────────────────
-- 9. RLS：开启，且**一条 policy 都不建**
--
--    这不是漏了，是刻意设计：
--      · 所有 DB 读写都走 Next Route Handlers，用 service_role key（仅服务端）
--      · service_role 绕过 RLS，所以不建 policy 也不影响自己的应用
--      · anon / authenticated 角色没有任何 policy 可命中 → 读不到任何一行
--    结论：NEXT_PUBLIC_SUPABASE_ANON_KEY 这个变量在本项目**不需要存在**。
--    别为了「能连上」去加一条 using (true) 的 policy —— 那等于把库公开。
-- ───────────────────────────────────────────────────────────────────────────

alter table public.podcasts         enable row level security;
alter table public.segments         enable row level security;
alter table public.favorites        enable row level security;
alter table public.worker_heartbeat enable row level security;


-- ───────────────────────────────────────────────────────────────────────────
-- 10. 自检：跑完可以执行这两句看看结果
--
--   应返回 4 行、且 rls_enabled 全为 true、policy_count 全为 0
-- ───────────────────────────────────────────────────────────────────────────
--
-- select c.relname,
--        c.relrowsecurity as rls_enabled,
--        (select count(*) from pg_policies p
--          where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
--   from pg_class c
--   join pg_namespace n on n.oid = c.relnamespace
--  where n.nspname = 'public'
--    and c.relname in ('podcasts', 'segments', 'favorites', 'worker_heartbeat')
--  order by c.relname;
--
-- select id, public from storage.buckets where id = 'podcast-audio';

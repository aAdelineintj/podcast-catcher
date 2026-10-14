/**
 * `GET /api/health` —— 服务与 worker 存活探针
 *
 * 为什么值得单独做一个接口：转写算力在本机 worker 上，而 worker 可能
 * 关机、休眠、或忘了启动。这时候用户传完音频会一直停在「排队中」，
 * 界面上没有任何线索说明「你的电脑没开着」。
 * 有了这个接口，前端就能明确提示「worker 离线」而不是让用户干等。
 */

import { NextResponse } from "next/server";

import { jsonError } from "@/lib/api-helpers";
import { getSupabaseConfig, selectRows } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

/**
 * 超过这个时间没心跳就算离线。
 *
 * worker 每 5 秒写一次心跳，这里给 30 秒的宽限 —— 6 倍冗余。
 * 给太紧会把「网络抖了一下」误判成离线，反而让用户以为服务坏了。
 */
const WORKER_ONLINE_WINDOW_MS = 30_000;

type HeartbeatRow = {
  worker_id: string;
  last_seen: string;
  model: string | null;
  current_job: string | null;
  hostname: string | null;
};

export async function GET() {
  try {
    // 读一次配置：配置缺失会在这里抛出 SupabaseConfigError，
    // 由 jsonError 转成带修复指引的 500
    getSupabaseConfig();

    const rows = await selectRows<HeartbeatRow>("worker_heartbeat", {
      select: "worker_id,last_seen,model,current_job,hostname",
      order: "last_seen.desc",
      limit: "1",
    });

    const latest = rows[0] ?? null;
    const ageMs = latest ? Date.now() - new Date(latest.last_seen).getTime() : null;
    const online = ageMs !== null && ageMs < WORKER_ONLINE_WINDOW_MS;

    return NextResponse.json({
      ok: true,
      supabase: { ok: true },
      worker: latest
        ? {
            online,
            workerId: latest.worker_id,
            model: latest.model,
            hostname: latest.hostname,
            currentJob: latest.current_job,
            lastSeen: latest.last_seen,
            // 夹到 ≥0。`last_seen` 是 Supabase 用它的 `now()` 写的，而这里用
            // 本机时钟做差 —— 两台机器的时钟不可能完全一致（实测会差出 1 秒左右）。
            // 不夹的话偶尔算出 -1，前端就会显示「最后心跳 -1 秒前」。
            ageSeconds: ageMs === null ? null : Math.max(0, Math.round(ageMs / 1000)),
          }
        : {
            online: false,
            workerId: null,
            model: null,
            hostname: null,
            currentJob: null,
            lastSeen: null,
            ageSeconds: null,
          },
    });
  } catch (error) {
    return jsonError(error, "健康检查失败。");
  }
}

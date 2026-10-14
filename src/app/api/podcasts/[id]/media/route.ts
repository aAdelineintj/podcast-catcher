/**
 * `GET /api/podcasts/[id]/media` —— 音频播放地址
 *
 * 每次都**现签一个短期 URL 再 302 过去**，而不是把签名 URL 存进数据库。
 *
 * ── 为什么不直接存签名 URL ──
 * 签名 URL 一小时就过期。用户完全可能把页面开着去吃饭，回来一按播放
 * 就拿到 403，而界面上只会显示「播放失败」，看不出是链接过期。
 * 现签的话，链接永远有效，且桶可以保持私有（公网拿到域名也读不到音频）。
 *
 * ── 为什么不代理字节流 ──
 * 让音频流量穿过 Vercel 函数会消耗带宽配额，而 302 之后是浏览器
 * 直连 Supabase CDN，函数只输出几十字节的响应头。
 * Range 请求（拖动进度条）也能正常工作：浏览器跟随 302 后会带着
 * Range 头重新请求新地址，Storage 原生支持。
 */

import { NextResponse } from "next/server";

import { jsonError, NotFoundError, readRouteParam } from "@/lib/api-helpers";
import { createSignedDownloadUrl, selectOne } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const id = await readRouteParam(params);

    const row = await selectOne<{ audio_path: string | null }>("podcasts", {
      id: `eq.${id}`,
      select: "audio_path",
    });

    if (!row) {
      throw new NotFoundError("没有找到这条播客记录。");
    }
    if (!row.audio_path) {
      // 这个分支说明「记录已建、音频还没传完」。前端不该在这时请求音频，
      // 但如果请求了，错误信息要说清原因，而不是给一个空洞的 404。
      throw new NotFoundError("该播客的音频尚未上传完成，暂时无法播放。");
    }

    const signedUrl = await createSignedDownloadUrl(row.audio_path);

    return NextResponse.redirect(signedUrl, {
      status: 302,
      headers: {
        // 重定向结果本身绝不能缓存：缓存的签名 URL 会在有效期内
        // 一直指向同一个 token，一旦过期就永久 403，且刷新页面也不恢复
        "Cache-Control": "no-store, must-revalidate",
      },
    });
  } catch (error) {
    return jsonError(error, "获取音频地址失败。");
  }
}

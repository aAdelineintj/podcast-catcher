/**
 * Route Handler 的公共工具：统一的错误响应与请求体解析。
 *
 * 集中在这里的原因：`SupabaseConfigError` 和 `SupabaseError` 是两类
 * 完全不同的问题（一个是部署配错了，一个是数据库/网络出问题了），
 * 但它们都是 `Error` 的子类，散落各处时很容易被写成 `catch { 500 }` ——
 * 那样线上出问题时，日志里只有一句「服务器内部错误」，排查要从头开始。
 */

import { NextResponse } from "next/server";

import { SupabaseConfigError, SupabaseError } from "@/lib/supabase-server";

/** 用户输入有问题（缺字段、格式不对），该返回 400 而不是 500。 */
export class BadRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BadRequestError";
  }
}

/** 目标资源不存在，该返回 404。 */
export class NotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NotFoundError";
  }
}

export function jsonError(error: unknown, fallbackMessage = "服务器内部错误。"): NextResponse {
  if (error instanceof BadRequestError) {
    return NextResponse.json({ error: error.message }, { status: 400 });
  }

  if (error instanceof NotFoundError) {
    return NextResponse.json({ error: error.message }, { status: 404 });
  }

  if (error instanceof SupabaseConfigError) {
    // 这是部署问题，不是用户操作问题。日志里必须留全信息，
    // 响应里也把修复指引带出去 —— 否则线上只会看到一片 500。
    console.error("[api] Supabase 配置错误：", error.message);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  if (error instanceof SupabaseError) {
    console.error("[api] Supabase 请求失败：", error.message);
    return NextResponse.json(
      {
        error: `数据库请求失败（HTTP ${error.status}）。请检查 Supabase 项目是否正常、密钥是否有效。`,
      },
      { status: 502 },
    );
  }

  console.error("[api] 未预期错误：", error);
  return NextResponse.json({ error: fallbackMessage }, { status: 500 });
}

/** 解析 JSON 请求体。解析失败给 400，而不是让 SyntaxError 冒泡成 500。 */
export async function readJsonBody<T>(request: Request): Promise<T> {
  try {
    return (await request.json()) as T;
  } catch {
    throw new BadRequestError("请求体不是合法的 JSON。");
  }
}

/** 从 Next 的动态路由参数里取 id（Next 15+ 起 `params` 是 Promise）。 */
export async function readRouteParam(
  params: Promise<Record<string, string>>,
  key = "id",
): Promise<string> {
  const resolved = await params;
  const value = resolved?.[key];
  if (!value) {
    throw new BadRequestError(`URL 里缺少 ${key} 参数。`);
  }
  return value;
}

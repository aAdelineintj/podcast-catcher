"""Supabase 的薄封装：PostgREST + Storage REST，只用 requests。

**为什么不用 `supabase-py`**：
  1. 本仓库通篇用 `requests`，多一个 HTTP 客户端就多一套超时/重试语义要理解；
  2. worker 只需要 6 个端点，而 `supabase-py` 会拖入 httpx / gotrue / storage3 /
     realtime 等 10+ 依赖 —— 它们要和 torch + whisper 共存在同一个 venv 里，
     版本冲突面白白变大；
  3. 它真正的增值（GoTrue 会话、Realtime 订阅）worker 一个都用不到。

**认证**：全部用 service_role key。本项目 RLS 是「开启 + 零策略」，
只有 service_role 能绕过 RLS 读写 —— 也就是说这里的密钥一旦泄露等于库裸奔，
所以它只能存在于服务端环境变量，**永远不要加 `NEXT_PUBLIC_` 前缀**。
"""

from __future__ import annotations

import json
from typing import Any

import requests

from . import config


class SupabaseError(RuntimeError):
    """带上响应体的错误，便于直接看出 PostgREST 到底嫌哪里不对。"""

    def __init__(self, status: int, method: str, path: str, body: str):
        self.status = status
        super().__init__(f"[{status}] {method} {path}\n    {body[:600]}")


class SupabaseClient:
    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
        bucket: str | None = None,
        timeout: int = 60,
    ):
        self.url = (url or config.SUPABASE_URL).rstrip("/")
        self.key = service_key or config.SUPABASE_SERVICE_ROLE_KEY
        self.bucket = bucket or config.SUPABASE_STORAGE_BUCKET
        self.timeout = timeout

    # ── 基础设施 ───────────────────────────────────────────────────────

    def _headers(
        self,
        extra: dict[str, str] | None = None,
        *,
        with_content_type: bool = True,
    ) -> dict[str, str]:
        """构造请求头。

        `with_content_type=False` 用于**没有请求体**的调用（DELETE）。

        这不是洁癖：Storage 的 DELETE 端点是 Fastify 实现的，一旦看到
        `Content-Type: application/json` 却没收到 body，它会直接返回
        400 `Body cannot be empty when content-type is set to 'application/json'`。
        表现是「删除对象总是失败」—— 而 worker 里删除失败是只打警告不中断的
        （压缩只是省空间，不该把任务判成失败），所以这个问题会静默地
        让原始大文件一直躺在 Storage 里白占空间。
        """
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        if with_content_type:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            f"{self.url}{path}",
            timeout=self.timeout,
            **kwargs,
        )
        if not response.ok:
            raise SupabaseError(response.status_code, method, path, response.text)
        return response

    # ── PostgREST ─────────────────────────────────────────────────────

    def rpc(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """调用数据库函数。返回 json（setof 函数返回数组，void 返回空）。"""
        response = self._request(
            "POST",
            f"/rest/v1/rpc/{name}",
            headers=self._headers(),
            data=json.dumps(params or {}),
        )
        if not response.content:
            return None
        return response.json()

    def select(
        self,
        table: str,
        *,
        filters: dict[str, str] | None = None,
        select: str = "*",
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """查询。`filters` 的 value 直接写成 PostgREST 的运算符表达式。

        例：`select("podcasts", filters={"id": "eq.<uuid>"}, select="id,title")`
        """
        params: dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)

        response = self._request(
            "GET", f"/rest/v1/{table}", headers=self._headers(), params=params
        )
        return response.json()

    def update(self, table: str, filters: dict[str, str], values: dict[str, Any]) -> None:
        """按条件更新。`Prefer: return=minimal` 避免把整行回传（省流量）。"""
        self._request(
            "PATCH",
            f"/rest/v1/{table}",
            headers=self._headers({"Prefer": "return=minimal"}),
            params=filters,
            data=json.dumps(values, ensure_ascii=False),
        )

    def upsert(
        self,
        table: str,
        rows: list[dict],
        *,
        on_conflict: str,
        chunk_size: int = 500,
    ) -> None:
        """按冲突列合并写入（存在即更新）。

        `on_conflict` 必须与表上的唯一约束/主键一致，否则 PostgREST 会直接报错
        （不会静默变成普通 insert）—— 这正是我们想要的：宁可报错，不要重复行。

        分批是为了避开单次请求体上限；一集 30 分钟播客约 400 行，
        正常一批就够，chunk 只是兜底。
        """
        if not rows:
            return

        headers = self._headers({
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            self._request(
                "POST",
                f"/rest/v1/{table}",
                headers=headers,
                params={"on_conflict": on_conflict},
                data=json.dumps(chunk, ensure_ascii=False),
            )

    def delete(self, table: str, filters: dict[str, str]) -> None:
        self._request(
            "DELETE",
            f"/rest/v1/{table}",
            headers=self._headers({"Prefer": "return=minimal"}, with_content_type=False),
            params=filters,
        )

    # ── Storage ───────────────────────────────────────────────────────

    def download_object(self, object_path: str) -> bytes:
        """下载对象原始字节。私有桶必须带 Authorization，不能用匿名 URL。"""
        response = self._request(
            "GET",
            f"/storage/v1/object/{self.bucket}/{object_path}",
            headers=self._headers(),
        )
        return response.content

    def upload_object(
        self, object_path: str, data: bytes, content_type: str = "audio/mpeg"
    ) -> None:
        """覆盖上传对象。

        `x-upsert: true` 是必需的：转写完成后要把原始 mp3 用压缩版覆盖回去，
        没有这个头 Storage 会返回 400 Duplicate。
        """
        self._request(
            "POST",
            f"/storage/v1/object/{self.bucket}/{object_path}",
            headers=self._headers({
                "Content-Type": content_type,
                "x-upsert": "true",
            }),
            data=data,
        )

    def delete_object(self, object_path: str) -> None:
        """删除对象。用于「压缩版换了个扩展名」时清掉原始大文件。

        注意用 `with_content_type=False` —— DELETE 没有请求体，
        带上 JSON 的 Content-Type 会被 Storage 直接 400 拒掉。
        """
        self._request(
            "DELETE",
            f"/storage/v1/object/{self.bucket}/{object_path}",
            headers=self._headers(with_content_type=False),
        )

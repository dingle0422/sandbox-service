"""通用端点代理：把请求原样转发到容器内 HTTP 服务（含 SSE 流式透传）。

沙箱服务对容器内协议**零感知**——方法/query/请求体/响应体/状态码全透传；
`text/event-stream` 响应按流式转发（不缓冲）。这是 vm2 业务中立化的核心机制
（对齐 OpenSandbox 的 host/port 透传网关思路）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger("sandbox_service.proxy")

#: 逐跳头（RFC 9110 §7.6.1）：不得透传
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: 非流式请求超时；SSE 读超时另放宽
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
_SSE_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)


class UpstreamUnreachableError(RuntimeError):
    """容器内服务不可达（映射 502 sandbox_unreachable）。"""


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}


async def proxy_request(request: Request, base_url: str, path: str) -> Response:
    """把 ``request`` 转发到 ``{base_url}/{path}``，返回上游响应（SSE 走流式）。"""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    headers = _forward_headers(request)

    client = httpx.AsyncClient(timeout=_SSE_TIMEOUT)
    try:
        upstream = client.build_request(request.method, url, headers=headers, content=body)
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise UpstreamUnreachableError(f"{request.method} {url}: {exc}") from exc

    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    content_type = resp.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        async def _stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            _stream(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    try:
        content = await resp.aread()
    finally:
        await resp.aclose()
        await client.aclose()
    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


__all__ = ["proxy_request", "UpstreamUnreachableError"]

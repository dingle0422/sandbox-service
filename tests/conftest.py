"""顶层 conformance 夹具：自托管 echo-agent 作为参考 agent。

两套 conformance（agent / lifecycle）默认打进程内自启的 echo（真 HTTP，非 TestClient
桩），因此不依赖 Docker 就能验证契约；也可用环境变量把靶子换成真容器/外部服务：

- ``AGENT_BASE_URL``：直接对该 agent 跑 agent-conformance（跳过自启 echo）。
- ``SANDBOX_SERVICE_URL``：直接对该沙箱服务跑 lifecycle-conformance（见其 conftest）。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/agent/health", timeout=1.0).status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"echo agent 未在 {timeout}s 内就绪: {last}")


@pytest.fixture(scope="session")
def echo_base_url(tmp_path_factory) -> str:
    """自启 echo-agent（uvicorn 线程）并返回 base_url；``AGENT_BASE_URL`` 存在则直接用它。"""
    ext = (os.getenv("AGENT_BASE_URL") or "").strip()
    if ext:
        yield ext.rstrip("/")
        return

    import uvicorn

    os.environ["WORKSPACE"] = str(tmp_path_factory.mktemp("echo-ws"))
    import echo_agent.app as echo_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(echo_app.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_health(url)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

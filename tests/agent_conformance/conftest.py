"""agent-conformance 夹具：对一个「合规 agent」的 base_url 做黑盒 HTTP 验证。

靶子来自顶层 ``echo_base_url``（默认自启 echo；``AGENT_BASE_URL`` 可换真容器）。
每个用例后自动取消残留 run 并等到 idle，保证用例间不因单容器单活跃 run 互相污染。
"""

from __future__ import annotations

import time

import httpx
import pytest


@pytest.fixture()
def agent(echo_base_url: str):
    with httpx.Client(base_url=echo_base_url, timeout=30.0) as client:
        yield client


@pytest.fixture(autouse=True)
def _reset(agent: httpx.Client):
    yield
    # 收尾：取消任何活跃 run 并等到不 busy（echo worker 收到取消即收尾关流）
    try:
        agent.post("/agent/cancel", json={})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not agent.get("/agent/health").json().get("busy"):
                return
            time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass

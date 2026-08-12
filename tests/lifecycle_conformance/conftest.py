"""lifecycle-conformance 夹具：对 sandbox-lifecycle 北向 API 做黑盒验证。

两种靶子：
- 缺省**自托管** `sandbox_service`（内存 FakeBackend，其 ``base_url`` 指向顶层自启的
  真 echo，故通用代理/SSE 走真实转发代码）——不依赖 Docker 即可跑；
- ``SANDBOX_SERVICE_URL`` 存在则直接打该运行中的服务（未来可指向 OpenSandbox+薄适配层，
  作为「换底座」的验收锚点）；该服务开了北向鉴权时用 ``SANDBOX_SERVICE_TOKEN`` 传 Bearer。

统一暴露 ``lc``（生命周期 API 客户端，.get/.post/.put/.delete/.stream 兼容）与
``agent_port``（代理目标端口，自托管下由 FakeBackend 忽略）。
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest


class _FakeBackend:
    """内存容器后端：所有容器 base_url 指向真 echo（代理透传打真 agent）。"""

    def __init__(self, agent_url: str) -> None:
        from sandbox_service.backend import ContainerState, ContainerStatus

        self._ContainerState = ContainerState
        self._ContainerStatus = ContainerStatus
        self.agent_url = agent_url.rstrip("/")
        self.containers: dict[str, dict] = {}
        self._n = 0

    def has_image(self, image: str) -> bool:
        return True  # 假后端不涉真镜像：一律视为已就位，不触发拉取

    def pull_image(self, image: str) -> None:
        return None

    def remove_image(self, image: str) -> bool:
        return True

    def list_repo_tags(self, repo: str) -> list[tuple[str, float]]:
        return []

    def list_in_use_images(self) -> frozenset[str]:
        return frozenset()

    def image_idents(self, image: str) -> frozenset[str]:
        return frozenset({image})

    def create(self, spec) -> str:
        self._n += 1
        cid = f"c{self._n:04d}"
        self.containers[cid] = {
            "running": False,
            "exit_code": None,
            "sandbox_id": getattr(spec, "sandbox_id", ""),
        }
        return cid

    def start(self, container_id: str) -> None:
        self.containers[container_id]["running"] = True

    def stop(self, container_id: str, timeout: float = 5.0) -> None:
        c = self.containers.get(container_id)
        if c:
            c["running"] = False
            c["exit_code"] = 0

    def inspect(self, container_id: str):
        c = self.containers[container_id]
        return self._ContainerStatus(
            container_id=container_id,
            state=self._ContainerState.RUNNING if c["running"] else self._ContainerState.EXITED,
            running=c["running"],
            exit_code=c["exit_code"],
            started_at="2026-07-24T00:00:00Z",
        )

    def base_url(self, container_id: str, port: int) -> str:
        return self.agent_url

    def stop_for_sandbox(self, sandbox_id: str) -> int:
        """契约要求：销毁以运行时为准，账本外的同 id 容器也要停（见 lifecycle §2.2）。"""
        hits = [
            cid
            for cid, c in self.containers.items()
            if c.get("sandbox_id") == sandbox_id and c["running"]
        ]
        for cid in hits:
            self.stop(cid)
        return len(hits)

    def reap_orphans(self, keep_ids=frozenset(), *, min_age_seconds: float = 120.0) -> int:
        return 0


class _FakeStore:
    """内存 ObjectStore（snapshot restore 用；缺失即抛 not found）。"""

    def __init__(self) -> None:
        from sandbox_service.objectstore import ObjectNotFoundError

        self._NotFound = ObjectNotFoundError
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, key: str) -> bytes:
        if key not in self.objects:
            raise self._NotFound(key)
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture()
def agent_port() -> int:
    return int(os.getenv("AGENT_PORT") or 8080)


@pytest.fixture()
def lc(echo_base_url: str, tmp_path):
    """生命周期 API 客户端：外部 URL 直连，否则自托管 sandbox_service（TestClient）。"""
    ext = (os.getenv("SANDBOX_SERVICE_URL") or "").strip()
    if ext:
        # 真实部署一般开了 SERVICE_TOKEN，不带头会整套 401；自托管靶子 service_token="" 无需此头。
        token = (os.getenv("SANDBOX_SERVICE_TOKEN") or "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(base_url=ext.rstrip("/"), timeout=30.0, headers=headers) as client:
            yield client
        return

    from fastapi.testclient import TestClient

    from sandbox_service.config import Settings
    from sandbox_service.image_gc import ImageUsageStore
    from sandbox_service.main import create_app
    from sandbox_service.pool import SandboxPool
    from sandbox_service.service import ServiceState
    from sandbox_service.watcher import SandboxWatcher, WebhookNotifier

    settings = Settings(
        service_token="",
        workspace_root=tmp_path / "workspaces",
        pool_capacity=4,
        idle_ttl_seconds=600.0,
        reap_interval_seconds=0.0,
        watch_interval_seconds=999.0,
        agent_image="echo-agent:latest",
        enable_legacy_shim=False,
        image_gc_interval_seconds=0.0,
    )
    backend = _FakeBackend(echo_base_url)
    usage = ImageUsageStore(tmp_path / ".sandbox-service" / "image_usage.json")
    pool = SandboxPool(
        backend,
        capacity=settings.pool_capacity,
        idle_ttl=settings.idle_ttl_seconds,
        on_image_used=usage.touch,
    )
    watcher = SandboxWatcher(
        pool,
        WebhookNotifier("", token=""),
        interval_seconds=999.0,
        image_gc_interval_seconds=0.0,
    )
    state = ServiceState(
        settings=settings, pool=pool, store=_FakeStore(), watcher=watcher, image_usage=usage
    )
    with TestClient(create_app(state)) as client:
        yield client
    pool.shutdown()


@pytest.fixture()
def ready_sandbox(lc, agent_port):
    """创建一个就绪沙箱，yield 其 id；用例结束销毁（幂等）。"""
    sid = f"lc-{uuid.uuid4().hex[:8]}"
    r = lc.post("/sandboxes", json={"id": sid, "wait_ready": {"path": "/agent/health", "timeout_s": 30}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("ready", "reused")
    yield sid
    lc.delete(f"/sandboxes/{sid}")

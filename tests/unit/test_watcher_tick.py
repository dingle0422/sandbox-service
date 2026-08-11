"""SandboxWatcher.tick 的自动回收集成：evict_grace>0 时触发 reap_expired_candidates + emit。

用 FakeBackend（无 Docker）+ notify_fn 捕获事件，验证 watcher 把池的回收方法串起来。
"""

from __future__ import annotations

import time
from pathlib import Path

from sandbox_service.backend import ContainerSpec, ContainerState, ContainerStatus
from sandbox_service.pool import SandboxPool
from sandbox_service.watcher import SandboxWatcher, WebhookNotifier


class FakeBackend:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self._n = 0

    def create(self, spec: ContainerSpec) -> str:
        self._n += 1
        return f"c{self._n:04d}"

    def start(self, container_id: str) -> None:
        return None

    def stop(self, container_id: str, timeout: float = 5.0) -> None:
        self.stopped.append(container_id)

    def has_image(self, image: str) -> bool:
        return True

    def pull_image(self, image: str) -> None:
        return None

    def stop_for_sandbox(self, sandbox_id: str) -> int:
        return 0

    def inspect(self, container_id: str) -> ContainerStatus:
        return ContainerStatus(
            container_id=container_id, state=ContainerState.RUNNING, running=True
        )

    def base_url(self, container_id: str, port: int) -> str:
        return f"http://fake-{container_id}:{port}"

    def reap_orphans(self, keep_ids=frozenset(), *, min_age_seconds=120.0) -> int:  # type: ignore[override]
        return 0


def _spec(sid: str) -> ContainerSpec:
    return ContainerSpec(sandbox_id=sid, image="img", workspace_path=Path("/tmp/x"))


def _make_pool() -> tuple[SandboxPool, FakeBackend]:
    backend = FakeBackend()
    pool = SandboxPool(backend=backend, capacity=8, idle_ttl=600.0, reap_interval=0.0)
    return pool, backend


def test_tick_reaps_expired_candidate_and_emits_evicted():
    pool, backend = _make_pool()
    cid = pool.acquire("s1", _spec("s1"))[0]
    pool.release("s1")
    # 标记为很久以前的候选
    pool._evict_candidates["s1"] = time.time() - 1000.0

    events: list[tuple[str, str, str, str]] = []
    watcher = SandboxWatcher(
        pool,
        WebhookNotifier(),
        interval_seconds=5.0,
        orphan_sweep_seconds=60.0,
        evict_grace_seconds=30.0,
        notify_fn=lambda kind, sid, c, reason: events.append((kind, sid, c, reason)),
    )
    watcher.tick()

    assert pool.get_lease("s1") is None  # 租约被摘
    assert backend.stopped == [cid]  # 容器被 stop
    assert ("evicted", "s1", cid, "idle_ttl_grace_expired") in events


def test_tick_no_reap_when_grace_zero():
    """evict_grace<=0（默认）时 tick 不触发自动回收，保持「服务不自行销毁」契约。"""
    pool, backend = _make_pool()
    cid = pool.acquire("s1", _spec("s1"))[0]
    pool.release("s1")
    pool._evict_candidates["s1"] = time.time() - 1000.0

    events: list[tuple[str, str, str, str]] = []
    watcher = SandboxWatcher(
        pool,
        WebhookNotifier(),
        interval_seconds=5.0,
        orphan_sweep_seconds=60.0,
        evict_grace_seconds=0.0,  # 关闭
        notify_fn=lambda kind, sid, c, reason: events.append((kind, sid, c, reason)),
    )
    watcher.tick()

    assert pool.get_lease("s1") is not None  # 没被杀
    assert backend.stopped == []
    assert not any(k == "evicted" for k, *_ in events)


def test_tick_no_reap_when_candidate_too_young():
    """候选年龄不足 grace -> 不回收。"""
    pool, backend = _make_pool()
    pool.acquire("s1", _spec("s1"))
    pool.release("s1")
    pool._evict_candidates["s1"] = time.time()  # 刚标记

    watcher = SandboxWatcher(
        pool, WebhookNotifier(), evict_grace_seconds=30.0,
        notify_fn=lambda *a: None,
    )
    watcher.tick()

    assert pool.get_lease("s1") is not None
    assert backend.stopped == []

"""SandboxPool.reap_expired_candidates 单测：opt-in 自动回收的核心逻辑。

覆盖：grace<=0 关闭、年龄不足不回收、超期回收（摘租约+stop）、touch 竞态安全、
leased>0 防御跳过、stats 不回归（dict 改造后 evict_candidates 仍返回排序 sid）。
"""

from __future__ import annotations

import time
from pathlib import Path

from sandbox_service.backend import ContainerBackend, ContainerSpec, ContainerState, ContainerStatus
from sandbox_service.pool import SandboxPool


class FakeBackend:
    """实现 ContainerBackend 协议的最小内存后端：记录 stop 调用。"""

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


def _pool(*, idle_ttl: float = 600.0) -> tuple[SandboxPool, FakeBackend]:
    backend = FakeBackend()
    pool = SandboxPool(backend=backend, capacity=8, idle_ttl=idle_ttl, reap_interval=0.0)
    return pool, backend


def _mark_old(pool: SandboxPool, sid: str, age: float = 1000.0) -> None:
    """直接把 sid 标记为 age 秒前成为的候选（绕过 reap_now 的时间依赖）。"""
    pool._evict_candidates[sid] = time.time() - age


def test_reap_expired_disabled_when_grace_zero():
    pool, backend = _pool()
    pool.acquire("s1", _spec("s1"))
    _mark_old(pool, "s1")
    assert pool.reap_expired_candidates(grace_seconds=0.0) == []
    assert pool.reap_expired_candidates(grace_seconds=-1.0) == []
    assert pool.get_lease("s1") is not None
    assert backend.stopped == []


def test_reap_expired_skips_young_candidate():
    pool, backend = _pool()
    pool.acquire("s1", _spec("s1"))
    _mark_old(pool, "s1", age=10.0)  # 只当了 10s 候选
    destroyed = pool.reap_expired_candidates(grace_seconds=30.0)
    assert destroyed == []
    assert pool.get_lease("s1") is not None
    assert backend.stopped == []


def test_reap_expired_destroys_old_candidate():
    pool, backend = _pool()
    cid = pool.acquire("s1", _spec("s1"))[0]
    pool.release("s1")  # leased=0，与真实空闲态一致
    _mark_old(pool, "s1", age=1000.0)
    destroyed = pool.reap_expired_candidates(grace_seconds=30.0)
    assert destroyed == [("s1", cid)]
    assert pool.get_lease("s1") is None  # 租约被摘
    assert "s1" not in pool._evict_candidates
    assert backend.stopped == [cid]


def test_reap_expired_skips_touched_candidate():
    """竞态安全：标记为候选后、回收前有流量进来（touch 移出候选）-> 不回收。"""
    pool, backend = _pool()
    cid = pool.acquire("s1", _spec("s1"))[0]
    pool.release("s1")
    _mark_old(pool, "s1", age=1000.0)
    pool.touch("s1")  # 模拟标记后、回收前有流量
    destroyed = pool.reap_expired_candidates(grace_seconds=30.0)
    assert destroyed == []
    assert pool.get_lease("s1") is not None
    assert backend.stopped == []
    # touch 已把 s1 移出候选
    assert "s1" not in pool._evict_candidates


def test_reap_expired_skips_leased_candidate_defensively():
    """防御：候选列表里混入活跃租约（leased>0，状态不一致）-> 移出候选但不强杀。"""
    pool, backend = _pool()
    cid = pool.acquire("s1", _spec("s1"))[0]  # leased=1
    _mark_old(pool, "s1", age=1000.0)  # 直接塞进候选，模拟不一致
    destroyed = pool.reap_expired_candidates(grace_seconds=30.0)
    assert destroyed == []
    assert pool.get_lease("s1") is not None  # 没被杀
    assert "s1" not in pool._evict_candidates  # 但移出了候选
    assert backend.stopped == []


def test_stats_evict_candidates_sorted_after_dict_change():
    """dict 改造后 stats 仍返回排序 sid 列表。"""
    pool, _ = _pool()
    _mark_old(pool, "s2")
    _mark_old(pool, "s1")
    _mark_old(pool, "s3")
    assert pool.stats()["evict_candidates"] == ["s1", "s2", "s3"]


def test_reap_now_records_timestamp():
    """reap_now 标记候选时，_evict_candidates[sid] 存的是时间戳（float）。"""
    pool, _ = _pool(idle_ttl=0.0)  # idle_ttl=0：acquire 后立刻可逐出
    pool.acquire("s1", _spec("s1"))
    pool.release("s1")
    before = time.time()
    marked = pool.reap_now()
    assert marked == ["s1"]
    ts = pool._evict_candidates["s1"]
    assert isinstance(ts, float)
    assert ts >= before

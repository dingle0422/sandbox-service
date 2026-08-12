"""僵尸镜像 GC：账本、TTL、保护名单、在用跳过、bootstrap、watcher 开关。"""

from __future__ import annotations

import time
from pathlib import Path

from sandbox_service.backend import ContainerSpec, ContainerState, ContainerStatus
from sandbox_service.image_gc import ImageUsageStore, sweep_idle_images
from sandbox_service.pool import SandboxPool
from sandbox_service.watcher import SandboxWatcher, WebhookNotifier


class FakeImageBackend:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.repo_tags: list[tuple[str, float]] = []
        self.in_use: frozenset[str] = frozenset()
        self.remove_errors: set[str] = set()
        self._n = 0

    def create(self, spec: ContainerSpec) -> str:
        self._n += 1
        return f"c{self._n:04d}"

    def start(self, container_id: str) -> None:
        return None

    def stop(self, container_id: str, timeout: float = 5.0) -> None:
        return None

    def has_image(self, image: str) -> bool:
        return True

    def pull_image(self, image: str) -> None:
        return None

    def remove_image(self, image: str) -> bool:
        if image in self.remove_errors:
            raise RuntimeError(f"rmi failed {image}")
        self.removed.append(image)
        return True

    def list_repo_tags(self, repo: str) -> list[tuple[str, float]]:
        prefix = f"{repo}:"
        return [(t, c) for t, c in self.repo_tags if t == repo or t.startswith(prefix)]

    def list_in_use_images(self) -> frozenset[str]:
        return self.in_use

    def image_idents(self, image: str) -> frozenset[str]:
        return frozenset({image})

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


def test_store_register_idempotent_and_touch(tmp_path: Path):
    store = ImageUsageStore(tmp_path / "image_usage.json")
    store.register("repo:a", now=100.0)
    store.register("repo:a", now=200.0)
    assert store.last_used("repo:a") == 100.0
    store.touch("repo:a", now=300.0)
    assert store.last_used("repo:a") == 300.0
    # 持久化
    store2 = ImageUsageStore(tmp_path / "image_usage.json")
    assert store2.last_used("repo:a") == 300.0


def test_sweep_skips_young_image(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:old", now=1000.0)
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset(),
        ttl=100.0,
        now=1050.0,
    )
    assert result["removed"] == 0
    assert backend.removed == []
    assert store.last_used("reg/tax-agent:old") == 1000.0


def test_sweep_removes_idle_unprotected(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:zombie", now=100.0)
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset({"reg/tax-agent:current"}),
        ttl=100.0,
        now=300.0,
    )
    assert result["removed"] == 1
    assert backend.removed == ["reg/tax-agent:zombie"]
    assert store.last_used("reg/tax-agent:zombie") is None


def test_sweep_skips_protected_agent_and_service(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:keep", now=1.0)
    store.touch("sandbox-service:latest", now=1.0)
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset({"reg/tax-agent:keep", "sandbox-service:latest"}),
        ttl=1.0,
        now=100.0,
    )
    assert result["removed"] == 0
    assert result["skipped_protected"] == 2
    assert backend.removed == []


def test_sweep_skips_in_use(tmp_path: Path):
    backend = FakeImageBackend()
    backend.in_use = frozenset({"reg/tax-agent:busy"})
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:busy", now=1.0)
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset(),
        ttl=1.0,
        now=100.0,
    )
    assert result["removed"] == 0
    assert result["skipped_in_use"] == 1
    assert backend.removed == []


def test_sweep_ignores_untracked_images(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    # 未入账，即使 repo_tags 有也不直接删（bootstrap 只 seed，删仍看 TTL）
    backend.repo_tags = [("reg/tax-agent:other", 1.0)]
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset(),
        ttl=1.0,
        agent_image="reg/tax-agent:current",
        now=10.0,
    )
    # bootstrap seed，但 created=1.0 且 ttl=1、now=10 → 随后同轮会成为候选并删除
    assert result["bootstrapped"] == 1
    assert "reg/tax-agent:other" in backend.removed


def test_bootstrap_does_not_overwrite_existing(tmp_path: Path):
    backend = FakeImageBackend()
    backend.repo_tags = [("reg/tax-agent:sha1", 1.0)]
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:sha1", now=999.0)
    result = sweep_idle_images(
        backend,
        store,
        protected=frozenset({"reg/tax-agent:sha1"}),
        ttl=1.0,
        agent_image="reg/tax-agent:current",
        now=2000.0,
    )
    assert result["bootstrapped"] == 0
    assert store.last_used("reg/tax-agent:sha1") == 999.0


def test_watcher_image_gc_disabled_when_interval_zero(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:zombie", now=1.0)
    pool = SandboxPool(backend=backend, capacity=8, idle_ttl=600.0, reap_interval=0.0)
    watcher = SandboxWatcher(
        pool,
        WebhookNotifier(),
        interval_seconds=5.0,
        orphan_sweep_seconds=0.0,
        image_gc_interval_seconds=0.0,
        image_idle_ttl_seconds=1.0,
        image_usage=store,
        protected_images=frozenset(),
        agent_image="reg/tax-agent:keep",
    )
    watcher.tick()
    assert backend.removed == []


def test_watcher_image_gc_runs_when_enabled(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    store.touch("reg/tax-agent:zombie", now=1.0)
    pool = SandboxPool(backend=backend, capacity=8, idle_ttl=600.0, reap_interval=0.0)
    watcher = SandboxWatcher(
        pool,
        WebhookNotifier(),
        interval_seconds=5.0,
        orphan_sweep_seconds=0.0,
        image_gc_interval_seconds=1.0,
        image_idle_ttl_seconds=1.0,
        image_usage=store,
        protected_images=frozenset({"reg/tax-agent:keep"}),
        agent_image="reg/tax-agent:keep",
    )
    watcher._last_image_gc = 0.0
    watcher.tick()
    assert backend.removed == ["reg/tax-agent:zombie"]


def test_pool_acquire_touches_image_usage(tmp_path: Path):
    backend = FakeImageBackend()
    store = ImageUsageStore(tmp_path / "u.json")
    pool = SandboxPool(
        backend=backend,
        capacity=8,
        idle_ttl=600.0,
        reap_interval=0.0,
        on_image_used=store.touch,
    )
    spec = ContainerSpec(
        sandbox_id="s1",
        image="reg/tax-agent:v1",
        workspace_path=tmp_path / "ws",
    )
    pool.acquire("s1", spec)
    assert store.last_used("reg/tax-agent:v1") is not None
    assert store.last_used("reg/tax-agent:v1") > time.time() - 5

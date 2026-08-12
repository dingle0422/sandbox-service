"""SandboxPool：容量 N + 空闲 TTL + LRU 账本（移植旧 vm2 ContainerPool，去业务化）。

与旧实现的差异：
- 零业务字段：不再有 owner_id/project_id/Archiver——归档编排归应用层（经代理调容器内协议）。
- ``reap_now`` 只标记 ``evict_candidates``，不 archive、不 stop（销毁由调用方决策）。
- 逐出候选/死亡/退出经 webhook 通知（见 watcher.py），调用方也可轮询 /capacity 兜底。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from sandbox_service.backend import ContainerBackend, ContainerSpec, DockerBackend

logger = logging.getLogger("sandbox_service.pool")


class SandboxCreateError(RuntimeError):
    """镜像/创建/启动失败（映射 502）。"""


@dataclass
class Lease:
    container_id: str
    workspace: Path
    port: int = 8080
    leased: int = 0
    last_active: float = 0.0
    #: 调用方自定义元数据（服务不解读；legacy shim 用它带 owner_id/project_id）
    meta: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}


class SandboxPool:
    def __init__(
        self,
        backend: Optional[ContainerBackend] = None,
        *,
        capacity: int = 8,
        idle_ttl: float = 600.0,
        reap_interval: float = 0.0,
        on_image_used: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._backend: ContainerBackend = backend or DockerBackend()
        self._capacity = max(1, int(capacity))
        self._idle_ttl = max(0.0, float(idle_ttl))
        self._on_image_used = on_image_used
        self._leases: dict[str, Lease] = {}
        self._evict_candidates: dict[str, float] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None
        if reap_interval and reap_interval > 0:
            self._reap_interval = float(reap_interval)
            self._reaper = threading.Thread(target=self._reap_loop, name="sandbox-reaper", daemon=True)
            self._reaper.start()
        else:
            self._reap_interval = 0.0

    @property
    def backend(self) -> ContainerBackend:
        return self._backend

    def acquire(
        self, sandbox_id: str, spec: ContainerSpec, *, meta: Optional[dict] = None
    ) -> Optional[tuple[str, bool]]:
        """起（或幂等复用）沙箱容器。

        返回 ``(container_id, reused)``；池满返回 None（capacity_full）；创建失败抛
        :class:`SandboxCreateError`（与满区分）。``meta`` 为调用方自定义元数据（不解读）。
        """
        with self._lock:
            lease = self._leases.get(sandbox_id)
            if lease is not None and self._is_alive(lease.container_id):
                lease.leased += 1
                lease.last_active = time.time()
                if meta:
                    lease.meta.update(meta)
                self._evict_candidates.pop(sandbox_id, None)
                return lease.container_id, True
            if self._live_count() >= self._capacity:
                return None
        try:
            cid = self._backend.create(spec)
            self._backend.start(cid)
        except Exception as exc:
            logger.exception("容器起失败 sandbox=%s", sandbox_id)
            raise SandboxCreateError(str(exc)) from exc
        if self._on_image_used is not None:
            try:
                self._on_image_used(spec.image)
            except Exception:
                logger.exception("镜像用量 touch 失败 image=%s", spec.image)
        with self._lock:
            self._leases[sandbox_id] = Lease(
                container_id=cid,
                workspace=Path(spec.workspace_path),
                port=int(spec.port),
                leased=1,
                last_active=time.time(),
                meta=dict(meta or {}),
            )
            self._evict_candidates.pop(sandbox_id, None)
        logger.info("容器已起 sandbox=%s cid=%s", sandbox_id, cid[:12])
        return cid, False

    def release(self, sandbox_id: str) -> None:
        """keep-warm：租约计数归还（leased-1），空闲回收从 last_active 起算 TTL。"""
        with self._lock:
            lease = self._leases.get(sandbox_id)
            if lease is not None:
                lease.leased = max(0, lease.leased - 1)
                lease.last_active = time.time()

    def touch(self, sandbox_id: str) -> None:
        """标记有活动：刷新 last_active 并移出逐出候选。"""
        with self._lock:
            lease = self._leases.get(sandbox_id)
            if lease is not None:
                lease.last_active = time.time()
                self._evict_candidates.pop(sandbox_id, None)

    def forget(self, sandbox_id: str) -> bool:
        """丢弃僵尸租约（容器已消失）：只清账本，不 stop。返回是否确有删除。"""
        with self._lock:
            self._evict_candidates.pop(sandbox_id, None)
            return self._leases.pop(sandbox_id, None) is not None

    def terminate(self, sandbox_id: str, *, grace_seconds: float = 5.0, delete_workspace: bool = False) -> bool:
        """停容器（幂等）。默认不删工作区（工作区归属由调用方管理）。"""
        with self._lock:
            lease = self._leases.pop(sandbox_id, None)
            self._evict_candidates.pop(sandbox_id, None)
        if lease is None:
            return False
        try:
            self._backend.stop(lease.container_id, timeout=grace_seconds)
        except Exception:
            logger.exception("容器 stop 失败 sandbox=%s", sandbox_id)
        if delete_workspace:
            try:
                if lease.workspace.is_dir():
                    shutil.rmtree(lease.workspace, ignore_errors=True)
            except Exception:
                logger.exception("删工作区失败 sandbox=%s", sandbox_id)
        return True

    # ── 查询 ─────────────────────────────────────────────────────────────────
    def get_lease(self, sandbox_id: str) -> Optional[Lease]:
        with self._lock:
            return self._leases.get(sandbox_id)

    def iter_leases(self) -> list[tuple[str, Lease]]:
        with self._lock:
            return list(self._leases.items())

    def sandbox_id_for_container(self, container_id: str) -> Optional[str]:
        with self._lock:
            for sid, lease in self._leases.items():
                if lease.container_id == container_id or lease.container_id.startswith(container_id):
                    return sid
        return None

    def resolve(self, sid_or_cid: str) -> Optional[tuple[str, Lease]]:
        """按 sandbox_id 或 container_id（前缀）定位租约（north API 与 shim 双向兼容）。"""
        with self._lock:
            lease = self._leases.get(sid_or_cid)
            if lease is not None:
                return sid_or_cid, lease
            for sid, le in self._leases.items():
                if le.container_id == sid_or_cid or le.container_id.startswith(sid_or_cid):
                    return sid, le
        return None

    def base_url(self, sandbox_id: str, port: Optional[int] = None) -> str:
        lease = self.get_lease(sandbox_id)
        if lease is None:
            raise KeyError(sandbox_id)
        return self._backend.base_url(lease.container_id, int(port or lease.port))

    # ── 治理 ─────────────────────────────────────────────────────────────────
    def reap_now(self) -> list[str]:
        """空闲超 TTL → 记入 evict_candidates（不 stop）。返回本轮新标记的 sandbox id。"""
        now = time.time()
        marked: list[str] = []
        with self._lock:
            for sid, lease in self._leases.items():
                if lease.leased > 0:
                    continue
                if (now - lease.last_active) < self._idle_ttl:
                    continue
                if sid not in self._evict_candidates:
                    self._evict_candidates[sid] = now
                    marked.append(sid)
                    logger.info("TTL 可逐出 candidate sandbox=%s", sid)
        return marked

    def reap_orphan_containers(self, *, min_age_seconds: Optional[float] = None) -> int:
        """回收账本外的本服务容器。``min_age_seconds`` 保护在途创建（见 backend）。"""
        with self._lock:
            keep = frozenset(le.container_id for le in self._leases.values())
        if min_age_seconds is None:
            return self._backend.reap_orphans(keep_ids=keep)
        return self._backend.reap_orphans(keep_ids=keep, min_age_seconds=min_age_seconds)

    def reap_expired_candidates(self, *, grace_seconds: float) -> list[tuple[str, str]]:
        """opt-in 自动回收：成为 evict_candidate 超过 ``grace_seconds`` 的沙箱一律销毁。

        ``grace_seconds<=0`` 时不做任何事（保持「服务不自行销毁」契约，由调用方决策销毁）。
        返回本轮销毁的 ``(sandbox_id, container_id)`` 列表。

        原子性：check 年龄 + pop 租约/候选标记必须在同一把锁内完成，否则 ``touch``/``acquire``
        可能在锁间隙把一个正要回收的活跃沙箱移出候选--而租约还在--导致误杀。``stop`` 是慢
        Docker 调用，放到锁外执行；stop 失败则容器成孤儿，交由 ``reap_orphans`` 兜底
        （此时已不在 keep_ids）。不删工作区（工作区归属由调用方管理，与 ``terminate`` 一致）。
        """
        if grace_seconds <= 0:
            return []
        now = time.time()
        to_stop: list[tuple[str, Lease]] = []
        with self._lock:
            for sid, marked_at in list(self._evict_candidates.items()):
                if (now - marked_at) < grace_seconds:
                    continue
                lease = self._leases.get(sid)
                if lease is not None and lease.leased > 0:
                    # 防御：候选列表里不该出现活跃租约（acquire/touch 会移出候选）。出现即
                    # 状态不一致，移出候选放它一马，不强杀。
                    self._evict_candidates.pop(sid, None)
                    continue
                self._leases.pop(sid, None)
                self._evict_candidates.pop(sid, None)
                if lease is not None:
                    to_stop.append((sid, lease))
        destroyed: list[tuple[str, str]] = []
        for sid, lease in to_stop:
            try:
                self._backend.stop(lease.container_id)
            except Exception:
                logger.exception("evict 超期回收 stop 失败 sandbox=%s", sid)
            destroyed.append((sid, lease.container_id))
        if destroyed:
            logger.info(
                "evict_candidate 超期自动回收 grace=%ss count=%d ids=%s",
                grace_seconds, len(destroyed), [s for s, _ in destroyed],
            )
        return destroyed

    def stats(self) -> dict:
        with self._lock:
            live = self._live_count()
            leased = sum(
                1 for le in self._leases.values() if le.leased > 0 and self._is_alive(le.container_id)
            )
            return {
                "live": live,
                "leased": leased,
                "idle": max(0, live - leased),
                "capacity": self._capacity,
                "idleTtl": self._idle_ttl,
                "evict_candidates": sorted(self._evict_candidates),
            }

    def shutdown(self) -> None:
        self._stop.set()
        if self._reaper and self._reaper.is_alive():
            self._reaper.join(timeout=2.0)
        with self._lock:
            leases = list(self._leases.items())
            self._leases.clear()
            self._evict_candidates.clear()
        for _sid, lease in leases:
            try:
                self._backend.stop(lease.container_id)
            except Exception:
                pass

    # ── internals ────────────────────────────────────────────────────────────
    def _is_alive(self, cid: str) -> bool:
        try:
            return self._backend.inspect(cid).running
        except Exception:
            return False

    def _live_count(self) -> int:
        return sum(1 for le in self._leases.values() if self._is_alive(le.container_id))

    def _reap_loop(self) -> None:
        while not self._stop.wait(self._reap_interval):
            try:
                self.reap_now()
            except Exception:
                logger.exception("reaper 巡检异常")


__all__ = ["SandboxPool", "SandboxCreateError", "Lease"]

"""沙箱事件监视 + webhook 通知（sandbox-lifecycle.md §2.6）。

一个后台线程做两件事：
1. 容器 running→exited/dead 迁移 → ``exited`` / ``dead`` 通知；
2. 池 TTL 巡检产生的逐出候选 → ``evict_candidate`` 通知。

通知尽力而为（失败重试 3 次后放弃）；服务**不自行销毁**候选沙箱，
调用方也可轮询 ``/capacity`` 与 ``GET /sandboxes/{id}`` 兜底。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from sandbox_service.backend import DEFAULT_ORPHAN_MIN_AGE_SECONDS
from sandbox_service.pool import SandboxPool

logger = logging.getLogger("sandbox_service.watcher")


class WebhookNotifier:
    """POST 通用事件到调用方 sink，Bearer = 服务 token；失败重试 3 次。

    sink 优先级：``notify(url=...)`` 的按沙箱地址 > 部署级默认 ``callback_url``。
    二者皆空即不通知（调用方轮询 /capacity 与 GET /sandboxes/{id} 兜底）。
    服务只把 URL 当不透明 sink，不感知它属于哪个应用。
    """

    def __init__(self, callback_url: str = "", *, token: str = "") -> None:
        self._default_url = callback_url.rstrip("/") if callback_url else ""
        self._token = token

    @property
    def enabled(self) -> bool:
        return bool(self._default_url)

    def notify(
        self,
        kind: str,
        sandbox_id: str,
        container_id: str,
        reason: str,
        *,
        url: Optional[str] = None,
    ) -> None:
        sink = (url or self._default_url or "").rstrip("/")
        if not sink:
            return
        body = {
            "kind": kind,
            "sandbox_id": sandbox_id,
            "container_id": container_id,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        for attempt in range(3):
            try:
                r = httpx.post(sink, json=body, headers=headers, timeout=10.0)
                if r.status_code < 500:
                    return
            except Exception:
                pass
            time.sleep(min(2.0**attempt, 4.0))
        logger.warning("webhook 投递失败（放弃）kind=%s sandbox=%s", kind, sandbox_id)


class SandboxWatcher:
    """轮询容器状态迁移与 TTL 逐出候选，经 notifier 上报。"""

    def __init__(
        self,
        pool: SandboxPool,
        notifier: WebhookNotifier,
        *,
        interval_seconds: float = 5.0,
        orphan_sweep_seconds: float = 60.0,
        orphan_min_age_seconds: float = DEFAULT_ORPHAN_MIN_AGE_SECONDS,
        evict_grace_seconds: float = 0.0,
        notify_fn: Optional[Callable[[str, str, str, str], None]] = None,
    ) -> None:
        self._pool = pool
        self._notifier = notifier
        self._interval = max(1.0, float(interval_seconds))
        self._orphan_sweep_interval = float(orphan_sweep_seconds)
        self._orphan_min_age = float(orphan_min_age_seconds)
        #: evict_candidate 自动回收宽限期（<=0 关闭，保持「服务不自行销毁」契约）。
        #: 开启后：被标记为 candidate 超过该时长仍无人认领的沙箱由本 watcher 自动销毁。
        self._evict_grace = float(evict_grace_seconds)
        self._last_orphan_sweep = 0.0
        #: 测试注入点（签名 (kind, sandbox_id, container_id, reason)）；生产走 _emit → notifier
        self._notify_fn = notify_fn
        self._was_running: dict[str, bool] = {}
        self._notified_candidates: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sandbox-watcher", daemon=True)
        self._thread.start()
        logger.info("SandboxWatcher started interval=%ss", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _emit(self, kind: str, sandbox_id: str, container_id: str, reason: str) -> None:
        """发一条通用事件：解析该沙箱自带的 callback_url（无则用部署级默认），转发给 notifier。"""
        if self._notify_fn is not None:
            self._notify_fn(kind, sandbox_id, container_id, reason)
            return
        lease = self._pool.get_lease(sandbox_id)
        url = (lease.meta.get("callback_url") if lease is not None else None) or None
        self._notifier.notify(kind, sandbox_id, container_id, reason, url=url)

    def tick(self) -> None:
        # 1) 容器退出/死亡
        for sid, lease in self._pool.iter_leases():
            cid = lease.container_id
            try:
                st = self._pool.backend.inspect(cid)
                running = bool(st.running)
                exit_code = st.exit_code
                dead = st.state.value == "dead"
            except Exception:
                running, exit_code, dead = False, None, True
            prev = self._was_running.get(cid)
            self._was_running[cid] = running
            if prev is True and not running:
                if dead:
                    self._emit("dead", sid, cid, "health_probe_failed")
                else:
                    reason = "oom" if exit_code == 137 else "exit"
                    self._emit("exited", sid, cid, reason)

        # 2) TTL 逐出候选（reap 由池的 reaper 线程/本 tick 双通道均可触发）
        self._pool.reap_now()
        candidates = set(self._pool.stats()["evict_candidates"])
        for sid in candidates - self._notified_candidates:
            lease = self._pool.get_lease(sid)
            if lease is not None:
                self._emit("evict_candidate", sid, lease.container_id, "idle_ttl")
        self._notified_candidates = candidates

        # 2.5) opt-in 自动回收：candidate 超过宽限期仍无人认领 -> 销毁。
        #      默认关闭（evict_grace<=0），开启后反转「服务不自行销毁」前提。
        self._reap_expired_candidates()

        # 3) 清理不在池内的账本
        live = {lease.container_id for _, lease in self._pool.iter_leases()}
        for cid in list(self._was_running):
            if cid not in live:
                self._was_running.pop(cid, None)

        # 4) 周期性孤儿巡检：带本服务 label 却不在账本的容器一律回收。
        #    以前只在启动时扫一次，运行期漏掉的容器要等下次重启才有人管。
        self._sweep_orphans()

    def _sweep_orphans(self) -> None:
        if self._orphan_sweep_interval <= 0:
            return
        now = time.time()
        if (now - self._last_orphan_sweep) < self._orphan_sweep_interval:
            return
        self._last_orphan_sweep = now
        try:
            n = self._pool.reap_orphan_containers(min_age_seconds=self._orphan_min_age)
        except Exception:
            logger.exception("孤儿容器巡检失败")
            return
        if n:
            logger.warning("孤儿容器巡检回收 count=%d", n)

    def _reap_expired_candidates(self) -> None:
        """opt-in：销毁成为 evict_candidate 超过宽限期的沙箱并上报 ``evicted`` 事件。

        ``evict_grace<=0`` 时关闭（保持「服务不自行销毁」契约）。通知走部署级
        ``CALLBACK_URL``--此时租约已被 ``reap_expired_candidates`` 摘除，per-sandbox
        ``callback_url`` 取不到（已知限制，如需保留需让回收方法回传 meta）。
        """
        if self._evict_grace <= 0:
            return
        try:
            destroyed = self._pool.reap_expired_candidates(grace_seconds=self._evict_grace)
        except Exception:
            logger.exception("evict 超期回收异常")
            return
        for sid, cid in destroyed:
            self._emit("evicted", sid, cid, "idle_ttl_grace_expired")

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:
                logger.exception("watcher tick 异常")


__all__ = ["SandboxWatcher", "WebhookNotifier"]

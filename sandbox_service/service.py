"""服务装配：ServiceState（settings + pool + store + watcher）与公共辅助。"""

from __future__ import annotations

import logging
import shlex
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from sandbox_service.backend import ContainerBackend, ContainerSpec, DockerBackend
from sandbox_service.config import Settings, load_settings, session_paths
from sandbox_service.objectstore import ObjectStore, load_object_store
from sandbox_service.pool import SandboxPool
from sandbox_service.watcher import SandboxWatcher, WebhookNotifier

logger = logging.getLogger("sandbox_service.service")

#: 旧 uvicorn 注入入口（仅 AGENT_COMMAND=legacy 时使用；一个版本后删除）
_LEGACY_COMMAND = ["uvicorn", "sandbox_manager.app.agent_service:app", "--host", "0.0.0.0"]


class NotReadyError(RuntimeError):
    """就绪探测超时（映射 502 not_ready）。"""


@dataclass
class ServiceState:
    settings: Settings
    pool: SandboxPool
    store: ObjectStore
    watcher: SandboxWatcher

    # ── spec 装配 ────────────────────────────────────────────────────────────
    def default_command(self, port: int) -> Optional[list[str]]:
        raw = self.settings.agent_command
        if not raw:
            return None  # 镜像 CMD
        if raw == "legacy":
            return [*_LEGACY_COMMAND, "--port", str(port)]
        return shlex.split(raw)

    def code_mounts(self) -> list[tuple[str, str, str]]:
        mounts: list[tuple[str, str, str]] = []
        for item in (self.settings.agent_code_mounts or "").split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":")
            if len(parts) == 2:
                parts.append("ro")
            if len(parts) != 3 or parts[2] not in ("ro", "rw") or not parts[0].startswith("/"):
                logger.warning("AGENT_CODE_MOUNTS 条目非法，跳过: %r", item)
                continue
            mounts.append((parts[0], parts[1], parts[2]))
        return mounts

    def build_spec(
        self,
        sandbox_id: str,
        *,
        image: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        port: Optional[int] = None,
        cpu: Optional[float] = None,
        mem_mb: Optional[int] = None,
        egress_allow: Optional[list[str]] = None,
    ) -> ContainerSpec:
        s = self.settings
        workspace, debug = session_paths(s.workspace_root, sandbox_id)
        debug.mkdir(parents=True, exist_ok=True)
        p = int(port or s.agent_port)
        return ContainerSpec(
            sandbox_id=sandbox_id,
            image=image or s.agent_image,
            workspace_path=workspace,
            cpu_limit=float(cpu or s.agent_cpu),
            mem_mb=int(mem_mb or s.agent_mem_mb),
            env=dict(env or {}),
            network=s.agent_network or None,
            port=p,
            command=self.default_command(p),
            egress_allow=list(egress_allow if egress_allow is not None else s.agent_egress_allow),
            extra_mounts=self.code_mounts(),
        )

    # ── 就绪探测 ─────────────────────────────────────────────────────────────
    def wait_ready(
        self, sandbox_id: str, *, path: str = "/agent/health", timeout: float = 90.0, interval: float = 0.5
    ) -> dict:
        """轮询容器内 ``GET {path}`` 直至 HTTP 200（若为 JSON 且带 ok 字段则要求 ok 真值）。

        返回探测端点响应体（尽力解析 JSON）；超时抛 :class:`NotReadyError`。
        """
        deadline = time.time() + timeout
        last_err: Optional[Exception] = None
        with httpx.Client(timeout=5.0) as client:
            while time.time() < deadline:
                try:
                    base = self.pool.base_url(sandbox_id)
                    r = client.get(f"{base.rstrip('/')}{path}")
                    if r.status_code == 200:
                        try:
                            payload = r.json()
                        except Exception:
                            return {}
                        if not isinstance(payload, dict) or payload.get("ok", True):
                            return payload if isinstance(payload, dict) else {}
                except Exception as exc:
                    last_err = exc
                time.sleep(interval)
        raise NotReadyError(f"ready probe timeout sandbox={sandbox_id} path={path} last={last_err}")

    def shutdown(self) -> None:
        self.watcher.stop()
        self.pool.shutdown()


def build_state(
    settings: Optional[Settings] = None,
    *,
    backend: Optional[ContainerBackend] = None,
    store: Optional[ObjectStore] = None,
) -> ServiceState:
    s = settings or load_settings()
    pool = SandboxPool(
        backend or DockerBackend(),
        capacity=s.pool_capacity,
        idle_ttl=s.idle_ttl_seconds,
        reap_interval=s.reap_interval_seconds,
    )
    notifier = WebhookNotifier(s.callback_url, token=s.service_token)
    watcher = SandboxWatcher(
        pool,
        notifier,
        interval_seconds=s.watch_interval_seconds,
        orphan_sweep_seconds=s.orphan_sweep_seconds,
        orphan_min_age_seconds=s.orphan_min_age_seconds,
    )
    return ServiceState(
        settings=s,
        pool=pool,
        store=store if store is not None else load_object_store(),
        watcher=watcher,
    )


__all__ = ["ServiceState", "NotReadyError", "build_state"]

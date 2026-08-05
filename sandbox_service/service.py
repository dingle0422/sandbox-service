"""服务装配：ServiceState（settings + pool + store + watcher）与公共辅助。"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from dataclasses import dataclass, field
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
class ImagePull:
    """一次镜像就位过程的可观测状态（供 ``GET /images`` 查询）。"""

    image: str
    state: str  # pulling | present | failed
    started_at: float
    finished_at: Optional[float] = None
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "image": self.image,
            "state": self.state,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "error": self.error or None,
        }


@dataclass
class ServiceState:
    settings: Settings
    pool: SandboxPool
    store: ObjectStore
    watcher: SandboxWatcher

    #: 镜像就位状态（image → ImagePull），仅供观测
    _pulls: dict[str, ImagePull] = field(default_factory=dict)
    #: 保护 _pulls 与 _image_locks 两张表本身
    _table_lock: threading.Lock = field(default_factory=threading.Lock)
    #: 每镜像一把锁：预热线程与 create 路径可能同时要同一个镜像，
    #: 拿同一把锁 → 后到者阻塞等前者拉完，而不是并发拉两遍
    _image_locks: dict[str, threading.Lock] = field(default_factory=dict)

    # ── 镜像就位 ─────────────────────────────────────────────────────────────
    def _lock_for(self, image: str) -> threading.Lock:
        with self._table_lock:
            return self._image_locks.setdefault(image, threading.Lock())

    def ensure_image(self, image: str, *, policy: Optional[str] = None) -> str:
        """按策略确保镜像在本机就位（阻塞）。返回 present|pulled|skipped。

        跨机部署下镜像由 registry 分发，而 ``docker create`` 不会自动拉。这里是
        **兜底**：正常应由调用方在部署时先打 ``POST /images`` 预热，避免把几分钟的
        拉取压到某个用户的建沙箱请求上。
        """
        pol = (policy or self.settings.image_pull_policy).lower()
        if pol == "never":
            return "skipped"
        with self._lock_for(image):  # 与并发的预热/建沙箱串行
            if pol == "missing" and self.pool.backend.has_image(image):
                self._record(image, "present")
                return "present"
            self._record(image, "pulling")
            try:
                self.pool.backend.pull_image(image)
            except Exception as exc:  # noqa: BLE001
                self._record(image, "failed", error=str(exc))
                raise
            self._record(image, "present")
            return "pulled"

    def start_image_pull(self, image: str) -> ImagePull:
        """异步预热：立刻返回状态，后台线程拉取。幂等——已就位/在拉则不重复起线程。

        必须异步：拉 500MB 要数分钟，调用方（vm1 启动钩子）不能被卡住。
        """
        if self.pool.backend.has_image(image):
            return self._record(image, "present")
        with self._table_lock:
            cur = self._pulls.get(image)
            if cur is not None and cur.state == "pulling":
                return cur
            pull = ImagePull(image=image, state="pulling", started_at=time.time())
            self._pulls[image] = pull

        def _run() -> None:
            try:
                # policy=always：既然是显式预热请求，就别被 never 策略挡掉
                self.ensure_image(image, policy="always")
            except Exception:  # noqa: BLE001
                logger.exception("镜像预热失败 image=%s", image)

        threading.Thread(target=_run, name=f"image-pull-{image}", daemon=True).start()
        return pull

    def image_pull_status(self, image: str) -> Optional[ImagePull]:
        with self._table_lock:
            return self._pulls.get(image)

    def _record(self, image: str, state: str, *, error: str = "") -> ImagePull:
        now = time.time()
        with self._table_lock:
            cur = self._pulls.get(image)
            started = cur.started_at if cur is not None else now
            pull = ImagePull(
                image=image,
                state=state,
                started_at=started,
                finished_at=None if state == "pulling" else now,
                error=error,
            )
            self._pulls[image] = pull
        return pull

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
        agent_env = dict(env or {})
        agent_env.setdefault("WORKSPACE", "/session/workspace")
        agent_env.setdefault("DEBUG_DIR", "/session/debug")
        if agent_env["WORKSPACE"] == "/workspace":
            agent_env["WORKSPACE"] = "/session/workspace"
        if agent_env["DEBUG_DIR"] == "/tmp/debug":
            agent_env["DEBUG_DIR"] = "/session/debug"
        return ContainerSpec(
            sandbox_id=sandbox_id,
            image=image or s.agent_image,
            workspace_path=workspace,
            cpu_limit=float(cpu or s.agent_cpu),
            mem_mb=int(mem_mb or s.agent_mem_mb),
            env=agent_env,
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

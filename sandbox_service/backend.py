"""容器后端：`ContainerSpec` + `ContainerBackend` 协议 + Docker 实现。

移植自 backend/sandbox_manager/app/container/backend/（P2 greenfield），
去掉业务残留：不再有 default_agent_command 兜底（spec.command=None 即镜像 CMD）。

部署约束（经 docker.sock 起 sibling 容器）：``workspace_path`` 必须是宿主机真实路径，
且与本服务进程所见路径相同（compose 用同路径 host bind，勿用 named volume）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("sandbox_service.backend")

#: docker 的 Created 带纳秒（9 位小数），fromisoformat 只吃 3/6 位 → 截断到微秒
_TS_FRACTION = re.compile(r"\.(\d{1,9})")

#: 孤儿巡检默认最小年龄：``SandboxPool.acquire`` 在锁外 create/start、之后才登记租约，
#: 周期巡检若无此保护会把「在途创建」的容器当孤儿误杀。
DEFAULT_ORPHAN_MIN_AGE_SECONDS = 120.0


class ContainerStopError(RuntimeError):
    """停止/移除后容器仍然存在——必须让调用方感知并重试，不能静默当成功。"""


class ImagePullError(RuntimeError):
    """镜像拉取失败（registry 不可达 / tag 不存在 / 未放行明文 registry）。"""


#: 创建容器时剔除的敏感 env（禁止把真实 LLM key/平台凭据注入沙箱）
_ENV_DENYLIST = frozenset({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY_REAL"})

#: 容器 label 前缀（孤儿回收按此识别本服务所辖容器）
LABEL_ROLE = "sandbox-service.role"
LABEL_SANDBOX_ID = "sandbox-service.sandbox_id"
ROLE_VALUE = "sandbox"
#: 兼容旧 vm2 label（孤儿回收同时匹配，两代服务交接期不漏杀不误杀）
LEGACY_LABEL_FILTER = "tax-agent.role=sandbox-agent"


class ContainerState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    REMOVING = "removing"
    EXITED = "exited"
    DEAD = "dead"


@dataclass
class ContainerStatus:
    container_id: str
    state: ContainerState
    running: bool
    exit_code: Optional[int] = None
    started_at: Optional[str] = None


@dataclass
class ContainerSpec:
    """业务中立容器规格：镜像/命令/端口/资源/env（不透明）/挂载。

    ``command=None`` = 不注入命令，用镜像 CMD（镜像自带入口）。
    """

    sandbox_id: str
    image: str
    workspace_path: Path
    cpu_limit: float = 2.0
    mem_mb: int = 2048
    env: dict[str, str] = field(default_factory=dict)
    network: Optional[str] = None
    port: int = 8080
    command: Optional[list[str]] = None
    egress_allow: list[str] = field(default_factory=list)
    #: [(host_path, container_path, mode)]
    extra_mounts: list[tuple[str, str, str]] = field(default_factory=list)


@runtime_checkable
class ContainerBackend(Protocol):
    def create(self, spec: ContainerSpec) -> str: ...
    def start(self, container_id: str) -> None: ...
    def has_image(self, image: str) -> bool: ...
    def pull_image(self, image: str) -> None: ...
    def stop(self, container_id: str, timeout: float = 5.0) -> None: ...
    def stop_for_sandbox(self, sandbox_id: str) -> int: ...
    def inspect(self, container_id: str) -> ContainerStatus: ...
    def base_url(self, container_id: str, port: int) -> str: ...
    def reap_orphans(
        self,
        keep_ids: frozenset[str] = frozenset(),
        *,
        min_age_seconds: float = DEFAULT_ORPHAN_MIN_AGE_SECONDS,
    ) -> int: ...


class DockerBackend:
    """`ContainerBackend` 的 Docker（docker-py）实现。"""

    def __init__(self, client: Optional[Any] = None) -> None:
        self._client: Any = client  # docker.DockerClient，惰性创建

    def _cli(self) -> Any:
        if self._client is None:
            from docker import from_env

            self._client = from_env()
        return self._client

    # ── 镜像 ─────────────────────────────────────────────────────────────────
    def has_image(self, image: str) -> bool:
        """本机是否已有该镜像。``containers.create`` 不会自动拉，缺镜像会直接 ImageNotFound。"""
        from docker.errors import ImageNotFound

        try:
            self._cli().images.get(image)
            return True
        except ImageNotFound:
            return False

    def pull_image(self, image: str) -> None:
        """从 registry 拉镜像（阻塞，几百 MB 可能数分钟）。

        失败原因绝大多数是部署问题而非代码问题（registry 未起、tag 拼错、宿主机
        ``daemon.json`` 未把明文 registry 加进 ``insecure-registries``——后者报的是 TLS 错），
        所以原文透传进异常消息，别包装成笼统的「拉取失败」。
        """
        # docker-py 的 pull 要求 tag 与仓库名分开传，否则 "host:5000/x:tag" 里的端口冒号会被误当 tag
        repo, _, tag = image.rpartition(":")
        if not repo or "/" in tag:
            repo, tag = image, "latest"
        try:
            self._cli().images.pull(repo, tag=tag)
        except Exception as exc:  # noqa: BLE001
            raise ImagePullError(f"拉取 {image} 失败：{exc}") from exc
        logger.info("镜像已拉取 image=%s", image)

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def create(self, spec: ContainerSpec) -> str:
        workspace = Path(spec.workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        mem = f"{int(spec.mem_mb)}m"
        port = int(spec.port or 8080)
        command = list(spec.command) if spec.command else None  # None → 镜像 CMD
        labels = {LABEL_SANDBOX_ID: spec.sandbox_id, LABEL_ROLE: ROLE_VALUE}
        if spec.egress_allow:
            labels["sandbox-service.egress_allow"] = ",".join(spec.egress_allow)
        env = {k: v for k, v in dict(spec.env).items() if k.upper() not in _ENV_DENYLIST}
        volumes: dict[str, dict] = {str(workspace.parent): {"bind": "/session", "mode": "rw"}}
        for host, target, mode in spec.extra_mounts:
            volumes[host] = {"bind": target, "mode": mode}
        container = self._cli().containers.create(
            image=spec.image,
            command=command,
            working_dir="/session/workspace",
            volumes=volumes,
            environment=env,
            labels=labels,
            nano_cpus=int(spec.cpu_limit * 1e9),
            mem_limit=mem,
            memswap_limit=mem,  # 禁 swap，OOM 即 kill
            ports={f"{port}/tcp": None},
            detach=True,
            stdin_open=False,
            tty=False,
        )
        if spec.network:
            try:
                self._cli().networks.get(spec.network).connect(container)
            except Exception:
                logger.warning("容器加入网络失败 cid=%s network=%s", container.short_id, spec.network)
        cid = container.short_id
        logger.info(
            "容器已创建 sandbox=%s cid=%s image=%s cmd=%s cpu=%s mem=%s",
            spec.sandbox_id, cid, spec.image,
            command if command is not None else "<image CMD>", spec.cpu_limit, mem,
        )
        return cid

    def start(self, container_id: str) -> None:
        self._cli().containers.get(container_id).start()

    def stop(self, container_id: str, timeout: float = 5.0) -> None:
        """优雅停（SIGTERM）→ 超时强杀 → remove 容器层（不残留）。幂等：容器不存在即成功。

        收尾会**复查容器确实消失**，没消失就抛 :class:`ContainerStopError`。
        早先实现吞掉全部异常，stop 失败时上层照样把租约从账本里摘掉，
        于是容器还在跑、账本却认为已销毁，成为谁都不再管的孤儿。
        """
        not_found = self._not_found_cls()
        try:
            c = self._cli().containers.get(container_id)
        except not_found:
            return
        except Exception as exc:
            raise ContainerStopError(f"容器查询失败 cid={container_id}: {exc}") from exc

        steps = (("stop", lambda: c.stop(timeout=int(timeout))), ("remove", lambda: c.remove(force=True)))
        for op, run in steps:
            try:
                run()
            except not_found:
                return
            except Exception:
                # 不早退：stop 失败仍要试 remove(force)，最终以复查结果为准
                logger.warning("容器 %s 失败 cid=%s", op, container_id, exc_info=True)

        try:
            st = self.inspect(container_id)
        except not_found:
            return
        except Exception as exc:
            raise ContainerStopError(f"容器复查失败 cid={container_id}: {exc}") from exc
        if st.state == ContainerState.REMOVING:
            # 并发删除竞态：remove 返回 409 "removal already in progress" 后容器处于
            # removing 中间态仍可查到（单次 DELETE 内 terminate->stop_for_sandbox 两次
            # stop 同一容器也会撞上）。Docker 会在内部完成移除，不必误报失败触发调用方重试。
            logger.info("容器移除进行中，视为成功 cid=%s", container_id)
            return
        raise ContainerStopError(
            f"stop/remove 后容器仍存在 cid={container_id} state={st.state.value}"
        )

    def stop_for_sandbox(self, sandbox_id: str) -> int:
        """按 ``sandbox_id`` label 停掉该沙箱名下**全部**容器；返回处理数。

        销毁以 Docker 为准而非内存账本：账本会漂移（服务重启丢租约、stop 半途失败），
        只认账本时「查无租约 → 直接回成功」会把仍在跑的容器永久遗留。
        """
        containers = self._cli().containers.list(
            all=True, filters={"label": f"{LABEL_SANDBOX_ID}={sandbox_id}"}
        )
        for c in containers:
            self.stop(c.id)
        if containers:
            logger.info("按 label 清理沙箱容器 sandbox=%s count=%d", sandbox_id, len(containers))
        return len(containers)

    def reap_orphans(
        self,
        keep_ids: frozenset[str] = frozenset(),
        *,
        min_age_seconds: float = DEFAULT_ORPHAN_MIN_AGE_SECONDS,
    ) -> int:
        """回收带本服务 label（含旧 vm2 label）但不在账本的容器；返回清理数。

        ``min_age_seconds`` 内新建的容器一律跳过，避免误杀在途创建
        （见 :data:`DEFAULT_ORPHAN_MIN_AGE_SECONDS`）。
        """
        n = 0
        seen: set[str] = set()
        for label_filter in (f"{LABEL_ROLE}={ROLE_VALUE}", LEGACY_LABEL_FILTER):
            try:
                containers = self._cli().containers.list(all=True, filters={"label": label_filter})
            except Exception:
                logger.exception("列出孤儿容器失败 filter=%s", label_filter)
                continue
            for c in containers:
                if c.id in seen:
                    continue
                seen.add(c.id)
                if c.id in keep_ids or c.short_id in keep_ids:
                    continue
                if min_age_seconds > 0 and self._age_seconds(c) < min_age_seconds:
                    continue
                try:
                    self.stop(c.id)
                except Exception:
                    logger.exception("孤儿容器回收失败 cid=%s", c.short_id)
                    continue
                n += 1
        if n:
            logger.info("孤儿容器清理 count=%d", n)
        return n

    @staticmethod
    def _not_found_cls() -> type[BaseException]:
        from docker.errors import NotFound

        return NotFound

    @staticmethod
    def _age_seconds(container: Any) -> float:
        """容器创建至今秒数；时间戳解析不了按「足够老」处理，避免漏回收。

        ``Created`` 有两种形态：``containers.list()`` 给 Unix 秒（int），
        ``containers.get()``（inspect）给 RFC3339 纳秒字符串。两种都要认，
        只认字符串会让年龄保护在巡检路径上默默失效。
        """
        created = (getattr(container, "attrs", None) or {}).get("Created")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            return max(0.0, datetime.now(timezone.utc).timestamp() - float(created))
        if not isinstance(created, str) or not created:
            return float("inf")
        ts = _TS_FRACTION.sub(lambda m: "." + m.group(1)[:6], created.replace("Z", "+00:00"))
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return float("inf")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())

    def inspect(self, container_id: str) -> ContainerStatus:
        c = self._cli().containers.get(container_id)
        state = c.attrs.get("State", {})
        status = state.get("Status", "exited")
        try:
            cs = ContainerState(status)
        except ValueError:
            cs = ContainerState.EXITED
        return ContainerStatus(
            container_id=container_id,
            state=cs,
            running=bool(state.get("Running", False)),
            exit_code=state.get("ExitCode"),
            started_at=state.get("StartedAt"),
        )

    def base_url(self, container_id: str, port: int) -> str:
        """解析容器 bridge IP，返回 ``http://ip:port``。"""
        c = self._cli().containers.get(container_id)
        nets = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        ip = (c.attrs.get("NetworkSettings") or {}).get("IPAddress") or ""
        if not ip:
            for net in nets.values():
                ip = (net or {}).get("IPAddress") or ""
                if ip:
                    break
        if not ip:
            raise RuntimeError(f"container {container_id} has no IP")
        return f"http://{ip}:{int(port)}"


__all__ = [
    "ContainerBackend",
    "ContainerSpec",
    "ContainerStopError",
    "ContainerState",
    "ContainerStatus",
    "DockerBackend",
    "LABEL_ROLE",
    "LABEL_SANDBOX_ID",
]

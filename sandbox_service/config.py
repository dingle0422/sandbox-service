"""服务配置：全部来自环境变量（见 docs/protocol/sandbox-lifecycle.md §3）。

刻意不用 pydantic-settings/不读业务配置：本服务的配置面就是这十几个 env，
保持零依赖 backend/、零数据库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _f(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else default


def _i(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    port: int = 8001
    #: 北向 Bearer；空 = 不鉴权（本地开发/单测）
    service_token: str = ""
    workspace_root: Path = Path("/var/sandbox/workspaces")

    # 池治理
    pool_capacity: int = 8
    idle_ttl_seconds: float = 600.0
    reap_interval_seconds: float = 30.0
    watch_interval_seconds: float = 5.0
    #: 孤儿容器巡检周期（秒，<=0 关闭）：回收带本服务 label 却不在账本的容器
    orphan_sweep_seconds: float = 60.0
    #: 孤儿最小年龄（秒）：低于此年龄的容器视为「在途创建」，巡检放行
    orphan_min_age_seconds: float = 120.0

    # 容器缺省 spec（可被 POST /sandboxes 请求体覆盖）
    agent_image: str = "tax-agent:agent-latest"
    agent_port: int = 8080
    agent_cpu: float = 2.0
    agent_mem_mb: int = 2048
    agent_network: str = ""
    agent_egress_allow: list[str] = field(default_factory=list)
    #: "" = 用镜像 CMD；"legacy" = 注入旧 uvicorn 入口；其余 = 自定义命令行
    agent_command: str = ""
    #: 热挂载 host:container[:mode],…（legacy 回落 AGENT_CODE_DIR → /opt:ro）
    agent_code_mounts: str = ""

    #: 部署级默认事件通知 sink（空 = 不通知）；调用方也可在 POST /sandboxes 时按沙箱自带
    #: callback_url 覆盖之——服务不持有任何应用侧业务地址。格式见 sandbox-lifecycle.md §2.6。
    callback_url: str = ""

    #: 工作区骨架目录（ensure 时创建；逗号分隔；业务由调用方注入，服务不预设语义）
    workspace_skeleton_dirs: list[str] = field(default_factory=list)

    #: snapshot restore 时保留的顶层项
    restore_preserve: list[str] = field(default_factory=lambda: ["knowledge", ".agent"])

    #: 是否挂载本仓税务兼容层（shim.py）；纯通用部署可置 false。
    #: 注意：shim 只做「请求→翻译→转发→响应」的被动路由，不含任何主动回调应用后端的编排。
    enable_legacy_shim: bool = True


def load_settings() -> Settings:
    def _csv(name: str, default: str = "") -> list[str]:
        return [x.strip() for x in _s(name, default).split(",") if x.strip()]

    return Settings(
        port=_i("SANDBOX_SERVICE_PORT", 8001),
        # 兼容旧变量名 VM2_SERVICE_TOKEN，便于存量部署零改切换
        service_token=_s("SERVICE_TOKEN") or _s("VM2_SERVICE_TOKEN"),
        workspace_root=Path(_s("SANDBOX_WORKSPACE_ROOT", "/var/sandbox/workspaces")).resolve(),
        pool_capacity=_i("POOL_CAPACITY", 8),
        idle_ttl_seconds=_f("IDLE_TTL_SECONDS", 600.0),
        reap_interval_seconds=_f("REAP_INTERVAL_SECONDS", 30.0),
        watch_interval_seconds=_f("WATCH_INTERVAL_SECONDS", 5.0),
        orphan_sweep_seconds=_f("ORPHAN_SWEEP_SECONDS", 60.0),
        orphan_min_age_seconds=_f("ORPHAN_MIN_AGE_SECONDS", 120.0),
        agent_image=_s("AGENT_IMAGE") or _s("CONTAINER_IMAGE", "tax-agent:agent-latest"),
        agent_port=_i("AGENT_PORT", 8080),
        agent_cpu=_f("AGENT_CPU", 2.0),
        agent_mem_mb=_i("AGENT_MEM_MB", 2048),
        agent_network=_s("AGENT_NETWORK"),
        agent_egress_allow=_csv("AGENT_EGRESS_ALLOW"),
        agent_command=_s("AGENT_COMMAND"),
        agent_code_mounts=_s("AGENT_CODE_MOUNTS") or (
            f"{_s('AGENT_CODE_DIR')}:/opt:ro" if _s("AGENT_CODE_DIR") else ""
        ),
        callback_url=_s("CALLBACK_URL"),
        workspace_skeleton_dirs=_csv("WORKSPACE_SKELETON_DIRS"),
        restore_preserve=_csv("RESTORE_PRESERVE", "knowledge,.agent"),
        enable_legacy_shim=_s("ENABLE_LEGACY_SHIM", "1").lower() not in ("0", "false", "no"),
    )


def session_paths(root: Path, sandbox_id: str) -> tuple[Path, Path]:
    """``(workspace_dir, debug_dir)``，目录布局与旧 vm2 一致（<root>/<id>/workspace|debug）。"""
    base = Path(root) / sandbox_id
    return (base / "workspace").resolve(), (base / "debug").resolve()


__all__ = ["Settings", "load_settings", "session_paths"]

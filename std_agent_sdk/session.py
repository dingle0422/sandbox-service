"""容器侧会话替身 :class:`AgentSession`（P3 gap-c，plan §P3.1）。

现役 ``agent_service.py`` 在容器内直接构造 vm1 的 ``app.sessions.manager.Session``——而该
模块顶层 import 了 PG 索引 / 持久化 store / 知识库播种，属「vm1 内部件误用」。runtime 实际
只读写下列少量字段（见依赖图分析），故此处定义一个**结构兼容的纯 dataclass 替身**：

- 字段名是 vm1 ``Session`` 的**子集**（``tests`` 断言锁定），故对 Orchestrator/Context/Emitter
  的鸭子类型访问透明——切换后行为等价；
- 零 import ``app.*``，可独立进 agent 镜像。

``handle`` 保持 ``Any``（容器内传 SandboxHandle）；本 SDK 不绑定具体 sandbox 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class AgentSessionStatus(str, Enum):
    """镜像 vm1 ``SessionStatus`` 的取值（保持字符串等值兼容）。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    ERROR = "error"
    CLOSED = "closed"


@dataclass
class AgentSession:
    """容器内 run 的最小会话上下文（runtime 真正读写的字段子集）。"""

    session_id: str
    workspace: Path
    debug_dir: Path = Path("/tmp/debug")
    enterprise_id: str = ""
    period: str = ""
    handle: Any = None
    status: AgentSessionStatus = AgentSessionStatus.IDLE
    owner_id: Optional[str] = None
    llm_model: Optional[str] = None
    thread_id: Optional[str] = None
    project_id: Optional[str] = None
    payload_key: Optional[str] = None
    current_run_id: Optional[str] = None
    #: Orchestrator 记录中断归属；宿主据此把会话置 WAITING（finalize 回传 interrupt_id）
    current_interrupt_id: Optional[str] = None
    inputs: Optional[list[Any]] = field(default=None)


__all__ = ["AgentSession", "AgentSessionStatus"]

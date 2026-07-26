"""agent-contract v1.0 权威请求/响应模型（docs/protocol/agent-contract.md §2）。

字段严格对齐现役 ``sandbox_manager/app/agent_service.py`` 的等价模型（由
``tests/test_contract_parity.py`` 奇偶校验锁定），使其可作为无损替换。核心字段任何领域
agent 都须理解；``[tax] 扩展`` 字段为税务领域私有，合规 agent 应容忍未知扩展字段（模型
``extra="allow"``）。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class _Lenient(BaseModel):
    """容忍未知扩展字段（契约 §0.2：合规 agent 不得因未知字段报错）。"""

    model_config = ConfigDict(extra="allow")


class RunRequest(_Lenient):
    """``POST /agent/input`` / ``/agent/resume`` 请求体（§2.2）。"""

    run_id: str
    session_id: str
    conversation_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_text: str = ""
    history: Optional[list[dict[str, Any]]] = None
    model: Optional[str] = None
    enterprise_id: str = ""  # [tax] 扩展
    period: str = ""  # [tax] 扩展
    owner_id: Optional[str] = None
    workspace: str = "/workspace"
    viewing: Optional[str] = None  # [tax] 扩展
    mode: Optional[str] = None
    new_plan: bool = False
    references: Optional[list[Any]] = None  # [tax] 扩展
    #: resume 载荷（非空即 resume 语义）：容器内 apply_resume + build_instruction 合成续跑指令
    resume_item: Optional[dict[str, Any]] = None
    shell_timeout: float = 30.0
    output_limit: int = 8000


class MaterializeReq(_Lenient):
    """``POST /agent/materialize`` 请求体（§2.6，全部可缺省，缺省读容器 env）。"""

    owner_id: Optional[str] = None
    session_id: Optional[str] = None
    enterprise_id: Optional[str] = None  # [tax] 扩展
    period: Optional[str] = None  # [tax] 扩展
    payload_key: Optional[str] = None
    template: str = "demo"  # [tax] 扩展
    workspace: Optional[str] = None
    force: bool = False


class CancelReq(_Lenient):
    """``POST /agent/cancel`` 请求体（§2.4，可带 run_id 防误取消新一轮）。"""

    run_id: Optional[str] = None


class ArchiveReq(_Lenient):
    """``POST /agent/archive`` 请求体（§2.7）。"""

    kind: str = "draft"
    owner_id: Optional[str] = None
    session_id: Optional[str] = None
    version_id: Optional[str] = None  # [tax] 扩展
    draft_id: Optional[str] = None  # [tax] 扩展
    project_id: Optional[str] = None  # [tax] 扩展
    workspace: Optional[str] = None


# ── 响应模型（informative：宿主/测试据此断言，agent 可返回超集）────────────────


class HealthResponse(_Lenient):
    """``GET /agent/health`` 响应（§2.1）。"""

    ok: bool = True
    busy: bool = False
    run_id: Optional[str] = None
    contractVersion: str


class MaterializeResult(_Lenient):
    """``POST /agent/materialize`` 200 响应（§2.6）。"""

    mode: str  # "payload" | "enterprise" | "skipped"
    bytes_loaded: int = 0
    inputs: Optional[list[str]] = None
    payload_key: Optional[str] = None
    detail: str = ""


class ArchiveResult(_Lenient):
    """``POST /agent/archive`` 200 响应（§2.7）。"""

    payload_key: str
    changed: bool
    payload_bytes: int = 0
    kind: str = "draft"
    version_id: Optional[str] = None
    draft_id: Optional[str] = None
    project_id: Optional[str] = None


#: SSE 收尾信封类型（§2.5，非 AG-UI 事件；终止事件之后、流关闭之前必须发出）。
FINALIZE_TYPE = "__finalize__"


__all__ = [
    "RunRequest",
    "MaterializeReq",
    "CancelReq",
    "ArchiveReq",
    "HealthResponse",
    "MaterializeResult",
    "ArchiveResult",
    "FINALIZE_TYPE",
]

"""契约模型自证：字段容忍性、代表性响应、AgentSession 覆盖面。

本仓只能验 SDK 自身自洽的部分。「SDK 模型与某个具体 agent 实现逐字段一致」那类奇偶校验
天然是跨仓断言，留在消费方仓库（例如 single-enterprise-tax-agent 的
``std_agent_sdk/tests/test_contract_parity.py``）——那里同时能 import 到现役 agent 入口
与其宿主会话类型。本仓的判据是 ``tests/agent_conformance``：黑盒打 HTTP，不依赖任何实现。
"""

from __future__ import annotations

import dataclasses

from std_agent_sdk import CONTRACT_VERSION
from std_agent_sdk.contract import (
    ArchiveResult,
    HealthResponse,
    MaterializeResult,
    RunRequest,
)
from std_agent_sdk.session import AgentSession


# ── 未知扩展字段容忍（契约 §0.2 / §6.7）────────────────────────────────────────
def test_request_models_tolerate_unknown_extensions():
    """契约要求请求模型对未知字段宽容：新增业务字段不该把老 agent 打挂。"""
    r = RunRequest(run_id="r", session_id="s", enterprise_id="e", __unknown__={"a": 1})
    assert r.run_id == "r"
    assert r.model_dump().get("__unknown__") == {"a": 1}


# ── 响应模型能校验代表性返回 ──────────────────────────────────────────────────
def test_response_models_validate_representative_payloads():
    HealthResponse(ok=True, busy=False, run_id=None, contractVersion=CONTRACT_VERSION)
    MaterializeResult(mode="skipped", bytes_loaded=0, inputs=None, payload_key=None, detail="x")
    ArchiveResult(payload_key="users/u/sessions/s/payload/x.tar.gz", changed=True, payload_bytes=1)


# ── AgentSession 覆盖 run 路径实际读写的属性 ──────────────────────────────────
def test_agent_session_covers_runtime_accessed_attrs():
    """orchestrator 等 run 路径鸭子访问的属性都必须在替身上，否则容器内会 AttributeError。"""
    accessed = {
        "current_interrupt_id",
        "llm_model",
        "workspace",
        "debug_dir",
        "enterprise_id",
        "period",
        "thread_id",
    }
    stand_fields = {f.name for f in dataclasses.fields(AgentSession)}
    assert accessed <= stand_fields, accessed - stand_fields


def test_agent_session_covers_entrypoint_constructed_fields():
    """agent 入口构造会话时显式赋的字段都要在替身上。"""
    constructed = {
        "session_id",
        "enterprise_id",
        "period",
        "workspace",
        "debug_dir",
        "handle",
        "status",
        "owner_id",
        "llm_model",
        "thread_id",
    }
    stand_fields = {f.name for f in dataclasses.fields(AgentSession)}
    assert constructed <= stand_fields, constructed - stand_fields

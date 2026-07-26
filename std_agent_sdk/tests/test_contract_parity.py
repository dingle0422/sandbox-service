"""奇偶校验：std_agent_sdk 模型/替身 与现役 agent_service 一致——保证无损替换。

现役容器入口 ``sandbox_manager/app/agent_service.py`` 内联了等价的 pydantic 模型与
vm1 ``Session`` 构造。本测试把 SDK 版本与之锁定，使「待 Docker 起后把入口切到 SDK」是
可证明的无损操作，而非盲改。
"""

from __future__ import annotations

import dataclasses

import pytest

from std_agent_sdk import CONTRACT_VERSION
from std_agent_sdk.contract import (
    ArchiveReq,
    ArchiveResult,
    CancelReq,
    HealthResponse,
    MaterializeReq,
    MaterializeResult,
    RunRequest,
)
from std_agent_sdk.session import AgentSession


# ── 请求模型字段与现役完全一致 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "sdk_model, live_name",
    [
        (RunRequest, "RunRequest"),
        (MaterializeReq, "MaterializeReq"),
        (CancelReq, "CancelReq"),
        (ArchiveReq, "ArchiveReq"),
    ],
)
def test_request_model_fields_match_live(sdk_model, live_name):
    import sandbox_manager.app.agent_service as live

    live_model = getattr(live, live_name)
    assert set(sdk_model.model_fields) == set(live_model.model_fields), (
        f"{live_name} 字段漂移：仅 SDK={set(sdk_model.model_fields) - set(live_model.model_fields)}，"
        f"仅现役={set(live_model.model_fields) - set(sdk_model.model_fields)}"
    )


def test_request_model_defaults_match_live():
    """关键缺省值与现役一致（漂移会导致切换后行为变化）。"""
    import sandbox_manager.app.agent_service as live

    checks = [
        (RunRequest, live.RunRequest, {"workspace": "/workspace", "shell_timeout": 30.0, "output_limit": 8000, "new_plan": False, "user_text": "", "enterprise_id": "", "period": ""}),
        (MaterializeReq, live.MaterializeReq, {"template": "demo", "force": False}),
        (ArchiveReq, live.ArchiveReq, {"kind": "draft"}),
    ]
    for sdk_model, live_model, expected in checks:
        for name, val in expected.items():
            assert sdk_model.model_fields[name].default == val
            assert live_model.model_fields[name].default == val


# ── 未知扩展字段容忍（契约 §0.2 / §6.7）────────────────────────────────────────
def test_request_models_tolerate_unknown_extensions():
    r = RunRequest(run_id="r", session_id="s", enterprise_id="e", __unknown__={"a": 1})
    assert r.run_id == "r"
    # 未知字段被保留（extra="allow"），不报错
    assert r.model_dump().get("__unknown__") == {"a": 1}


# ── 响应模型能校验现役端点的代表性返回 ────────────────────────────────────────
def test_response_models_validate_representative_payloads():
    HealthResponse(ok=True, busy=False, run_id=None, contractVersion=CONTRACT_VERSION)
    MaterializeResult(mode="skipped", bytes_loaded=0, inputs=None, payload_key=None, detail="x")
    ArchiveResult(payload_key="users/u/sessions/s/payload/x.tar.gz", changed=True, payload_bytes=1)


# ── AgentSession 替身是 vm1 Session 的忠实子集 ────────────────────────────────
def test_agent_session_fields_subset_of_vm1_session():
    from app.sessions.manager import Session

    live_fields = {f.name for f in dataclasses.fields(Session)}
    stand_fields = {f.name for f in dataclasses.fields(AgentSession)}
    extra = stand_fields - live_fields
    assert not extra, f"AgentSession 含 vm1 Session 没有的字段（不兼容）：{extra}"


def test_agent_session_covers_runtime_accessed_attrs():
    """依赖图分析出的 runtime 读写属性都必须在替身上。"""
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
    """现役 agent_service._build_session 显式赋的字段都要在替身上。"""
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

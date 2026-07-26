"""echo-agent 进程内合约测试（不依赖 Docker）。

逐条覆盖 agent-contract.md §6 合规性判定，作为 p3 conformance 套件的先行验证：
用 FastAPI TestClient 直接打 echo app，确认端点信封/时序/幂等/容错符合契约。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "workspace"))
    # 每个测试重载模块，隔离进程级 _active/_seq 全局态
    import importlib

    import echo_agent.app as mod

    importlib.reload(mod)
    with TestClient(mod.app) as c:
        yield c


def _drain_events(client) -> list[dict]:
    events: list[dict] = []
    with client.stream("GET", "/agent/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for line in resp.iter_lines():
            if line and line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


# §6.1 健康 + 契约版本 ─────────────────────────────────────────────────────────
def test_health_reports_contract_version(client):
    r = client.get("/agent/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["busy"] is False
    assert body["contractVersion"].split(".")[0] == "1"


# §6.2 input 202 + 活跃期重复 409 run_busy ────────────────────────────────────
def test_input_accepts_then_busy(client):
    r = client.post("/agent/input", json={"run_id": "r1", "session_id": "s1", "user_text": "hi"})
    assert r.status_code == 202
    assert r.json() == {"status": "accepted", "run_id": "r1"}
    # 活跃期内二次提交应 409（run 尚未收尾）
    r2 = client.post("/agent/input", json={"run_id": "r2", "session_id": "s1", "user_text": "again"})
    assert r2.status_code == 409
    assert r2.json()["detail"] == "run_busy"


# §6.3 事件流：RUN_STARTED … 终止 → __finalize__ → 关闭 ───────────────────────
def test_event_stream_shape_and_echo(client):
    client.post("/agent/input", json={"run_id": "r1", "session_id": "s1", "user_text": "hello"})
    events = _drain_events(client)
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert "RUN_FINISHED" in types
    assert types[-1] == "__finalize__"
    fin = events[-1]
    assert fin["status"] == "completed"
    # RUN_FINISHED 必须在 __finalize__ 之前
    assert types.index("RUN_FINISHED") < types.index("__finalize__")
    # echo 语义：拼接的 assistant 文本还原为 "echo: hello"
    text = "".join(e.get("delta", "") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == "echo: hello"


def test_events_without_run_emits_single_run_error(client):
    events = _drain_events(client)
    assert len(events) == 1
    assert events[0]["type"] == "RUN_ERROR"
    assert events[0]["content"] == "no_active_run"


# §6.4 cancel 幂等 + RUN_CANCELLED 收尾 ───────────────────────────────────────
def test_cancel_idempotent_no_run(client):
    r = client.post("/agent/cancel", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cancelled": False}


def test_cancel_terminates_with_run_cancelled(client):
    client.post("/agent/input", json={"run_id": "r1", "session_id": "s1", "user_text": "x" * 200})
    r = client.post("/agent/cancel", json={"run_id": "r1"})
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    events = _drain_events(client)
    types = [e["type"] for e in events]
    assert "RUN_CANCELLED" in types
    assert types[-1] == "__finalize__"
    assert events[-1]["status"] == "cancelled"


# §6.5 materialize 幂等（二次 skipped）────────────────────────────────────────
def test_materialize_idempotent(client):
    r1 = client.post("/agent/materialize", json={})
    assert r1.status_code == 200
    assert r1.json()["mode"] == "skipped"
    r2 = client.post("/agent/materialize", json={})
    assert r2.json()["mode"] == "skipped"


# §6.6 archive 返回 payload_key + changed ─────────────────────────────────────
def test_archive_returns_payload_key(client):
    r = client.post("/agent/archive", json={"kind": "draft", "owner_id": "u1", "session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["payload_key"]
    assert body["changed"] is False
    assert body["kind"] == "draft"


# §6.7 未知扩展字段不报错 ──────────────────────────────────────────────────────
def test_tolerates_unknown_extension_fields(client):
    r = client.post(
        "/agent/input",
        json={
            "run_id": "r1",
            "session_id": "s1",
            "user_text": "hi",
            "enterprise_id": "e1",
            "period": "2026Q1",
            "totally_unknown_field": {"nested": [1, 2, 3]},
        },
    )
    assert r.status_code == 202

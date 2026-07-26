"""agent-contract v1.0 黑盒合规套件（docs/protocol/agent-contract.md §6）。

判定「合规 agent」的最小标准，对任意实现契约的 agent base_url 皆可跑。默认靶子为
自启的 echo-agent；生产可 ``AGENT_BASE_URL=http://<容器>:8080 pytest tests/agent_conformance``。
"""

from __future__ import annotations

import json
import time

import httpx


def _drain_events(agent: httpx.Client, timeout: float = 30.0) -> list[dict]:
    """消费一次 SSE 事件流到关闭，解析每帧 ``data:`` JSON。"""
    events: list[dict] = []
    with agent.stream("GET", "/agent/events", timeout=timeout) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def _wait_busy(agent: httpx.Client, want: bool, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if bool(agent.get("/agent/health").json().get("busy")) == want:
            return True
        time.sleep(0.02)
    return False


# §6.1 —— 健康 + 契约版本 major = 1 ──────────────────────────────────────────
def test_health_ok_and_contract_major_1(agent: httpx.Client):
    r = agent.get("/agent/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert str(body["contractVersion"]).split(".")[0] == "1"


# §6.2 —— input 202；活跃期重复 409 run_busy ─────────────────────────────────
def test_input_accepts_then_busy_409(agent: httpx.Client):
    r1 = agent.post("/agent/input", json={"run_id": "c-r1", "session_id": "s1", "user_text": "x" * 400})
    assert r1.status_code == 202
    assert r1.json()["status"] == "accepted"
    assert _wait_busy(agent, True), "提交后应进入 busy"
    r2 = agent.post("/agent/input", json={"run_id": "c-r2", "session_id": "s1", "user_text": "again"})
    assert r2.status_code == 409
    assert r2.json().get("detail") == "run_busy"


# §6.3 —— 事件流：RUN_STARTED … 终止事件 → __finalize__ → 关流 ─────────────────
def test_event_stream_terminates_with_finalize(agent: httpx.Client):
    agent.post("/agent/input", json={"run_id": "c-r3", "session_id": "s1", "user_text": "hello"})
    events = _drain_events(agent)
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "__finalize__"
    terminal = {"RUN_FINISHED", "RUN_ERROR", "RUN_CANCELLED"}
    assert terminal & set(types), "缺终止事件"
    # 终止事件必须在 __finalize__ 之前
    last_terminal = max(i for i, t in enumerate(types) if t in terminal)
    assert last_terminal < types.index("__finalize__")
    assert events[-1]["status"] in ("completed", "error", "cancelled")


# §6.4 —— cancel 幂等；取消后事件流以 RUN_CANCELLED 终止 ───────────────────────
def test_cancel_idempotent_noop(agent: httpx.Client):
    assert _wait_busy(agent, False)
    r = agent.post("/agent/cancel", json={})
    assert r.status_code == 200
    assert r.json()["cancelled"] is False


def test_cancel_terminates_run(agent: httpx.Client):
    agent.post("/agent/input", json={"run_id": "c-r4", "session_id": "s1", "user_text": "y" * 800})
    assert _wait_busy(agent, True)
    r = agent.post("/agent/cancel", json={"run_id": "c-r4"})
    assert r.status_code == 200 and r.json()["cancelled"] is True
    events = _drain_events(agent)
    assert "RUN_CANCELLED" in [e["type"] for e in events]
    assert events[-1]["type"] == "__finalize__"


# §6.5 —— materialize 幂等（二次 skipped）─────────────────────────────────────
def test_materialize_idempotent(agent: httpx.Client):
    agent.post("/agent/materialize", json={})
    r2 = agent.post("/agent/materialize", json={})
    assert r2.status_code == 200
    assert r2.json()["mode"] == "skipped"


# §6.6 —— archive 返回 payload_key + changed ──────────────────────────────────
def test_archive_returns_payload_key(agent: httpx.Client):
    r = agent.post("/agent/archive", json={"kind": "draft", "owner_id": "u1", "session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["payload_key"], str) and body["payload_key"]
    assert isinstance(body["changed"], bool)


# §6.7 —— 未知扩展字段不报错 ──────────────────────────────────────────────────
def test_tolerates_unknown_extension_fields(agent: httpx.Client):
    r = agent.post(
        "/agent/input",
        json={
            "run_id": "c-r5",
            "session_id": "s1",
            "user_text": "hi",
            "enterprise_id": "e1",
            "period": "2026Q1",
            "__unknown__": {"a": [1, 2]},
        },
    )
    assert r.status_code == 202

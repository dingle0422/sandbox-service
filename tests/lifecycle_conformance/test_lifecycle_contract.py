"""sandbox-lifecycle v1.0 黑盒合规套件（docs/protocol/sandbox-lifecycle.md）。

验证沙箱平台北向契约的业务中立形态：服务级探活/容量、沙箱 CRUD、通用端点代理
（含 SSE 透传）、工作区文件与快照恢复。对自托管 sandbox_service 或外部
``SANDBOX_SERVICE_URL`` 皆可跑——作为「未来换沙箱底座」的验收锚点。
"""

from __future__ import annotations

import json


# —— 服务级 ────────────────────────────────────────────────────────────────
def test_health_reports_api_version(lc):
    r = lc.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert str(body["apiVersion"]).split(".")[0] == "1"


def test_capacity_shape(lc):
    r = lc.get("/capacity")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# —— 沙箱 CRUD ────────────────────────────────────────────────────────────────
def test_create_status_and_delete_idempotent(lc, ready_sandbox):
    sid = ready_sandbox
    r = lc.get(f"/sandboxes/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["running"] is True
    assert body.get("probe", {}).get("ok") is True
    # 删除幂等：二次删除仍 200
    assert lc.delete(f"/sandboxes/{sid}").status_code == 200
    assert lc.delete(f"/sandboxes/{sid}").status_code == 200


def test_unknown_sandbox_404(lc):
    assert lc.get("/sandboxes/does-not-exist").status_code == 404


# —— 通用端点代理（对容器内协议零感知）────────────────────────────────────────
def test_proxy_passthrough_health(lc, ready_sandbox, agent_port):
    sid = ready_sandbox
    r = lc.get(f"/sandboxes/{sid}/proxy/{agent_port}/agent/health")
    assert r.status_code == 200
    assert str(r.json()["contractVersion"]).split(".")[0] == "1"


def test_proxy_sse_stream(lc, ready_sandbox, agent_port):
    sid = ready_sandbox
    r = lc.post(
        f"/sandboxes/{sid}/proxy/{agent_port}/agent/input",
        json={"run_id": "lc-run", "session_id": sid, "user_text": "hello"},
    )
    assert r.status_code == 202
    events = []
    with lc.stream("GET", f"/sandboxes/{sid}/proxy/{agent_port}/agent/events", timeout=30.0) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "__finalize__"


# —— 工作区 ───────────────────────────────────────────────────────────────────
def test_workspace_file_roundtrip_and_escape(lc, ready_sandbox):
    sid = ready_sandbox
    assert lc.put(
        f"/sandboxes/{sid}/workspace/files/note.txt", json={"content": "hi from lifecycle"}
    ).status_code == 200
    r = lc.get(f"/sandboxes/{sid}/workspace/files/note.txt")
    assert r.status_code == 200 and r.json()["content"] == "hi from lifecycle"
    # 路径逃逸拦截（%2e%2e 绕过客户端归一化，让字面 .. 抵达 safe_resolve）
    esc = lc.get(f"/sandboxes/{sid}/workspace/files/%2e%2e/%2e%2e/etc/passwd")
    assert esc.status_code == 403


def test_snapshot_restore_missing_404(lc, ready_sandbox):
    sid = ready_sandbox
    r = lc.post(
        f"/sandboxes/{sid}/workspace/snapshot/restore",
        json={"payload_key": "no/such/payload.tar.gz"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "payload_missing"

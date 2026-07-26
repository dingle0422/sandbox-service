"""Legacy shim：旧 /containers/* /workspaces/* /blobs 路由行为等价（vm1 零改可切）。"""

import json


def _start(client, sid="s1", **extra):
    body = {
        "session_id": sid,
        "owner_id": "u1",
        "token": "tok-1",
        "enterprise_id": "ent-1",
        "period": "2026Q2",
        "project_id": "p1",
        **extra,
    }
    return client.post("/containers/start", json=body)


def test_start_translates_business_env(client, state):
    r = _start(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready" and body["agent_materialize"] is True

    lease = state.pool.get_lease("s1")
    spec = state.pool.backend.containers[lease.container_id]["spec"]
    env = spec.env
    assert env["SESSION_ID"] == "s1"
    assert env["OWNER_ID"] == "u1"
    assert env["ENTERPRISE_ID"] == "ent-1"
    assert env["PERIOD"] == "2026Q2"
    assert env["PROJECT_ID"] == "p1"
    assert env["AGENT_TOKEN"] == "tok-1" and env["LLM_API_KEY"] == "tok-1"
    assert lease.meta == {"owner_id": "u1", "project_id": "p1"}


def test_start_contract_mismatch_rejected(client, fake_agent):
    fake_agent.server.RequestHandlerClass.contract_version = "2.0"
    try:
        r = _start(client, sid="s2")
        assert r.status_code == 502
        assert r.json()["detail"] == "agent_contract_mismatch"
    finally:
        fake_agent.server.RequestHandlerClass.contract_version = "1.0"


def test_input_resume_cancel_forwarding(client, fake_agent):
    _start(client)
    cid = client.get("/capacity").json()  # noqa: F841 - 触发路由存在性
    lease_cid = None
    r = client.post("/containers/s1/input", json={"run_request": {"run_id": "r1", "session_id": "s1"}})
    assert r.status_code == 202
    r = client.post("/containers/s1/resume", json={"run_request": {"run_id": "r2", "session_id": "s1"}})
    assert r.status_code == 202
    r = client.post("/containers/s1/cancel", json={"run_id": "r2"})
    assert r.status_code == 200

    paths = [p for p, _ in fake_agent.requests]
    assert "/agent/input" in paths and "/agent/resume" in paths and "/agent/cancel" in paths
    # 缺 run_request → 400
    assert client.post("/containers/s1/input", json={}).status_code == 400
    assert lease_cid is None


def test_events_relay_is_pure_passthrough(client, fake_agent):
    """SSE 纯中继：逐帧透传，无终态自动归档副作用（归档归 vm1 显式触发）。"""
    _start(client)
    with client.stream("GET", "/containers/s1/events") as resp:
        frames = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert [f["type"] for f in frames] == ["RUN_STARTED", "RUN_FINISHED"]
    # 中继本身不得触发任何 /agent/archive
    assert [p for p, _ in fake_agent.requests if p == "/agent/archive"] == []


def test_archive_uses_lease_meta_defaults(client, fake_agent):
    _start(client)
    r = client.post("/containers/s1/archive", json={"kind": "version", "version_id": "v9"})
    assert r.status_code == 200
    assert r.json()["payload_key"]
    body = [b for p, b in fake_agent.requests if p == "/agent/archive"][-1]
    assert body["version_id"] == "v9"
    assert body["owner_id"] == "u1" and body["project_id"] == "p1"


def test_terminate_and_health(client, state):
    _start(client)
    lease = state.pool.get_lease("s1")
    h = client.get(f"/containers/{lease.container_id}/health").json()
    assert h["running"] is True and h["agent"]["ok"] is True

    assert client.post(f"/containers/{lease.container_id}/terminate", json={}).json()["ok"] is True
    assert state.pool.get_lease("s1") is None


def test_blob_delete(client, state):
    state.store.objects["users/u1/blobs/ab/ab12cd"] = b"x"
    r = client.delete("/blobs/ab12cd", params={"owner_id": "u1"})
    assert r.status_code == 200
    assert state.store.deleted == ["users/u1/blobs/ab/ab12cd"]


def test_legacy_workspace_routes(client):
    assert client.post("/workspaces/s9/ensure").json()["ok"] is True
    assert client.put("/workspaces/s9/files/a.txt", json={"content": "x"}).status_code == 200
    assert client.get("/workspaces/s9/files/a.txt").json()["content"] == "x"
    assert client.get("/workspaces/s9/files/%2e%2e/escape").status_code == 403
    tree = client.get("/workspaces/s9/files").json()
    assert {"inputs", "knowledge", "uploads"} <= {n["name"] for n in tree["tree"]}


def test_legacy_restore_expands_blobs(client, state):
    import io
    import tarfile

    def _tar_gz(files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel, data in files.items():
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    log = json.dumps({"uploads/f.bin": "cd34ef"}).encode()
    state.store.objects["k.tar.gz"] = _tar_gz({"uploads-meta/upload-log.json": log, "doc.md": b"d"})
    state.store.objects["users/u1/blobs/cd/cd34ef"] = b"BLOB"

    r = client.post("/workspaces/s9/restore", json={"owner_id": "u1", "payload_key": "k.tar.gz"})
    assert r.status_code == 200
    assert client.get("/workspaces/s9/files/uploads/f.bin").status_code == 200
    # 缺失快照 → 旧错误码 version_payload_missing
    r = client.post("/workspaces/s9/restore", json={"owner_id": "u1", "payload_key": "none"})
    assert r.status_code == 404
    assert r.json()["detail"] == "version_payload_missing"

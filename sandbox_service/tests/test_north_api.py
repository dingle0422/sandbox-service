"""北向中立 API（sandbox-lifecycle.md §2）：生命周期/代理/工作区/快照。"""

import gzip
import io
import json
import tarfile

import pytest


def _tar_gz(files: dict[str, bytes], *, payload_v2: bool = False) -> bytes:
    buf = io.BytesIO()
    headers = {"payload-version": "2"} if payload_v2 else None
    with tarfile.open(fileobj=buf, mode="w:gz", pax_headers=headers) as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── 生命周期 ─────────────────────────────────────────────────────────────────


def test_health_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["apiVersion"] == "1.0"


def test_create_status_delete(client):
    r = client.post("/sandboxes", json={"id": "s1", "env": {"FOO": "bar"},
                                        "wait_ready": {"path": "/agent/health", "timeout_s": 5}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "s1"
    assert body["status"] == "ready"
    cid = body["container_id"]

    # 幂等复用
    r2 = client.post("/sandboxes", json={"id": "s1"})
    assert r2.json()["status"] == "reused"
    assert r2.json()["container_id"] == cid

    st = client.get("/sandboxes/s1").json()
    assert st["running"] is True
    assert st["probe"]["contractVersion"] == "1.0"

    # 也可按 container_id 定位
    st2 = client.get(f"/sandboxes/{cid}").json()
    assert st2["id"] == "s1"

    assert client.delete("/sandboxes/s1").json()["terminated"] is True
    # 删除幂等
    assert client.delete("/sandboxes/s1").json()["terminated"] is False


def test_delete_stops_container_even_without_lease(client, state):
    """回归：账本丢了租约（服务重启/漂移）也必须真停容器。

    旧实现「resolve 不到 → 直接回 ok」，容器于是永远留在 Docker 里没人认领。
    """
    client.post("/sandboxes", json={"id": "s1"})
    cid = state.pool.get_lease("s1").container_id
    state.pool.forget("s1")  # 模拟账本漂移
    assert client.get("/sandboxes/s1").status_code == 404

    assert client.delete("/sandboxes/s1").json()["terminated"] is True
    assert state.pool.backend.containers[cid]["running"] is False


def test_delete_surfaces_stop_failure(client, state):
    """回归：停不掉要报 500 让调用方重试，回 200 等于制造孤儿。"""
    client.post("/sandboxes", json={"id": "s1"})
    cid = state.pool.get_lease("s1").container_id
    state.pool.backend.stop_fails = True

    assert client.delete("/sandboxes/s1").status_code == 500
    assert state.pool.backend.containers[cid]["running"] is True


def test_capacity_full(client):
    assert client.post("/sandboxes", json={"id": "a"}).status_code == 200
    assert client.post("/sandboxes", json={"id": "b"}).status_code == 200
    assert client.post("/sandboxes", json={"id": "c"}).status_code == 503
    stats = client.get("/capacity").json()
    assert stats["live"] == 2 and stats["capacity"] == 2


def test_env_is_opaque_passthrough(client, state):
    client.post("/sandboxes", json={"id": "s1", "env": {"WHATEVER_BUSINESS_KEY": "42"}})
    lease = state.pool.get_lease("s1")
    spec = state.pool.backend.containers[lease.container_id]["spec"]
    assert spec.env["WHATEVER_BUSINESS_KEY"] == "42"


# ── 通用代理 ─────────────────────────────────────────────────────────────────


def test_proxy_roundtrip_and_sse(client):
    client.post("/sandboxes", json={"id": "s1"})

    r = client.post("/sandboxes/s1/proxy/8080/echo", json={"x": 1})
    assert r.status_code == 200
    assert r.json() == {"method": "POST", "path": "/echo", "body": {"x": 1}}

    # SSE 流式透传
    with client.stream("GET", "/sandboxes/s1/proxy/8080/agent/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert [f["type"] for f in frames] == ["RUN_STARTED", "RUN_FINISHED"]


def test_proxy_unknown_sandbox_404(client):
    assert client.post("/sandboxes/nope/proxy/8080/echo", json={}).status_code == 404


# ── 工作区 ───────────────────────────────────────────────────────────────────


def test_workspace_files_crud_and_escape(client):
    client.post("/sandboxes/s1/workspace/ensure")
    assert client.put("/sandboxes/s1/workspace/files/notes/a.md", json={"content": "hi"}).status_code == 200
    got = client.get("/sandboxes/s1/workspace/files/notes/a.md").json()
    assert got["content"] == "hi" and got["encoding"] == "utf-8"

    tree = client.get("/sandboxes/s1/workspace/files").json()
    names = {n["name"] for n in tree["tree"]}
    assert {"inputs", "knowledge", "uploads", "notes"} <= names

    # 「..」须 URL 编码：客户端/中间层会规范化明文 ../，编码形式才能到达服务端校验
    assert client.get("/sandboxes/s1/workspace/files/%2e%2e/%2e%2e/etc/passwd").status_code == 403
    assert client.delete("/sandboxes/s1/workspace/files/notes/a.md").json()["deleted"] is True
    assert client.get("/sandboxes/s1/workspace/files/notes/a.md").status_code == 404


def test_workspace_upload_dedup(client):
    client.post("/sandboxes/s1/workspace/ensure")
    f = {"file": ("r.csv", b"a,b\n1,2\n", "text/csv")}
    p1 = client.post("/sandboxes/s1/workspace/files", files=f).json()["path"]
    p2 = client.post("/sandboxes/s1/workspace/files", files=f).json()["path"]
    assert p1 == p2 == "uploads/r.csv"  # 同内容幂等复用
    f2 = {"file": ("r.csv", b"different", "text/csv")}
    assert client.post("/sandboxes/s1/workspace/files", files=f2).json()["path"] == "uploads/r (1).csv"


def test_snapshot_restore_and_missing(client, state):
    state.store.objects["snap/k1.tar.gz"] = _tar_gz({"report.md": b"# hello"})
    client.post("/sandboxes/s1/workspace/ensure")
    client.put("/sandboxes/s1/workspace/files/junk.txt", json={"content": "old"})
    client.put("/sandboxes/s1/workspace/files/knowledge/keep.md", json={"content": "keep"})

    r = client.post("/sandboxes/s1/workspace/snapshot/restore", json={"payload_key": "snap/k1.tar.gz"})
    assert r.status_code == 200
    assert client.get("/sandboxes/s1/workspace/files/report.md").json()["content"] == "# hello"
    assert client.get("/sandboxes/s1/workspace/files/junk.txt").status_code == 404  # 已清空
    assert client.get("/sandboxes/s1/workspace/files/knowledge/keep.md").status_code == 200  # preserve

    # 快照缺失：404 且不破坏现有工作区
    r = client.post("/sandboxes/s1/workspace/snapshot/restore", json={"payload_key": "snap/none.tar.gz"})
    assert r.status_code == 404
    assert client.get("/sandboxes/s1/workspace/files/report.md").status_code == 200


def test_snapshot_restore_payload_v2_keeps_session_files_outside_workspace_api(client, state):
    session = state.settings.workspace_root / "s1"
    (session / "debug").mkdir(parents=True)
    (session / "debug/stale.log").write_text("stale")
    (session / ".old").mkdir()
    (session / ".old/stale.json").write_text("stale")
    state.store.objects["snap/v2.tar.gz"] = _tar_gz(
        {
            "workspace/report.md": b"# v2",
            "debug/run.log": b"debug",
            ".debug/trace.json": b"trace",
        },
        payload_v2=True,
    )

    r = client.post("/sandboxes/s1/workspace/snapshot/restore", json={"payload_key": "snap/v2.tar.gz"})

    assert r.status_code == 200
    assert (session / "workspace/report.md").read_bytes() == b"# v2"
    assert (session / "debug/run.log").read_bytes() == b"debug"
    assert (session / ".debug/trace.json").read_bytes() == b"trace"
    assert not (session / "debug/stale.log").exists()
    assert not (session / ".old").exists()
    names = {node["name"] for node in client.get("/sandboxes/s1/workspace/files").json()["tree"]}
    assert "debug" not in names
    assert ".debug" not in names


@pytest.mark.parametrize(
    ("name", "linkname"),
    [("../escaped", ""), ("scratch/file", ""), ("workspace/link", "/etc/passwd")],
)
def test_snapshot_restore_rejects_unsafe_payload_v2_before_clearing(client, state, name, linkname):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", pax_headers={"payload-version": "2"}) as tar:
        info = tarfile.TarInfo(name)
        if linkname:
            info.type = tarfile.SYMTYPE
            info.linkname = linkname
            tar.addfile(info)
        else:
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    state.store.objects["snap/bad.tar.gz"] = buf.getvalue()
    client.post("/sandboxes/s1/workspace/ensure")
    client.put("/sandboxes/s1/workspace/files/keep.txt", json={"content": "keep"})

    with pytest.raises(ValueError):
        client.post("/sandboxes/s1/workspace/snapshot/restore", json={"payload_key": "snap/bad.tar.gz"})

    assert (state.settings.workspace_root / "s1/workspace/keep.txt").read_text() == "keep"


def test_snapshot_restore_expands_upload_blobs(client, state):
    upload_log = json.dumps({"uploads/v.csv": "ab12cd"}).encode()
    state.store.objects["snap/k2.tar.gz"] = _tar_gz({"uploads-meta/upload-log.json": upload_log})
    state.store.objects["users/u1/blobs/ab/ab12cd"] = b"col1\n9\n"

    r = client.post(
        "/sandboxes/s1/workspace/snapshot/restore",
        json={"payload_key": "snap/k2.tar.gz", "blob_key_template": "users/u1/blobs/{sha2}/{sha}"},
    )
    assert r.status_code == 200
    assert client.get("/sandboxes/s1/workspace/files/uploads/v.csv").json()["content"] == "col1\n9\n"


# ── 鉴权 ─────────────────────────────────────────────────────────────────────


def test_token_guard(state, fake_agent):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from sandbox_service.main import create_app

    st = replace(state, settings=replace(state.settings, service_token="sekrit"))
    with TestClient(create_app(st)) as c:
        assert c.get("/health").status_code == 200  # 公开
        assert c.get("/capacity").status_code == 401
        assert c.get("/capacity", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/capacity", headers={"Authorization": "Bearer sekrit"}).status_code == 200

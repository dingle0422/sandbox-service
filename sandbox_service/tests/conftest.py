"""sandbox_service 测试夹具：假容器后端 + 线程级假 agent HTTP 服务器。"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根

from sandbox_service.backend import ContainerState, ContainerStatus  # noqa: E402
from sandbox_service.config import Settings  # noqa: E402
from sandbox_service.objectstore import ObjectNotFoundError  # noqa: E402
from sandbox_service.pool import SandboxPool  # noqa: E402
from sandbox_service.service import ServiceState  # noqa: E402
from sandbox_service.watcher import SandboxWatcher, WebhookNotifier  # noqa: E402


class FakeBackend:
    """内存容器后端：所有容器的 base_url 指向 fake agent 服务器。"""

    def __init__(self, agent_url: str = "") -> None:
        self.agent_url = agent_url
        self.containers: dict[str, dict] = {}
        self.stop_fails = False  # 模拟 docker stop 失败，验证不会静默漏容器
        self.reaped: list[frozenset[str]] = []
        self._n = 0
        # 缺省「镜像都在」，免得每个建沙箱的用例都被拉取路径干扰；
        # 想验拉取的用例置 images_present=False 即可
        self.images_present = True
        self.images: set[str] = set()
        self.pulled: list[str] = []
        self.pull_fails = False

    def has_image(self, image: str) -> bool:
        return self.images_present or image in self.images

    def pull_image(self, image: str) -> None:
        if self.pull_fails:
            from sandbox_service.backend import ImagePullError

            raise ImagePullError(f"pull failed image={image}")
        self.pulled.append(image)
        self.images.add(image)

    def create(self, spec) -> str:
        self._n += 1
        cid = f"c{self._n:04d}"
        self.containers[cid] = {
            "spec": spec,
            "running": False,
            "exit_code": None,
            "sandbox_id": getattr(spec, "sandbox_id", ""),
        }
        return cid

    def start(self, container_id: str) -> None:
        self.containers[container_id]["running"] = True

    def stop(self, container_id: str, timeout: float = 5.0) -> None:
        if self.stop_fails:
            raise RuntimeError(f"stop failed cid={container_id}")
        c = self.containers.get(container_id)
        if c:
            c["running"] = False
            c["exit_code"] = 0

    def stop_for_sandbox(self, sandbox_id: str) -> int:
        # 只数还活着的：真 Docker 后端 stop 会 remove 容器，二次删除必然扫到 0
        hits = [
            cid
            for cid, c in self.containers.items()
            if c.get("sandbox_id") == sandbox_id and c["running"]
        ]
        for cid in hits:
            self.stop(cid)
        return len(hits)

    def inspect(self, container_id: str) -> ContainerStatus:
        c = self.containers[container_id]
        return ContainerStatus(
            container_id=container_id,
            state=ContainerState.RUNNING if c["running"] else ContainerState.EXITED,
            running=c["running"],
            exit_code=c["exit_code"],
            started_at="2026-07-24T00:00:00Z",
        )

    def base_url(self, container_id: str, port: int) -> str:
        if not self.agent_url:
            raise RuntimeError("no agent url")
        return self.agent_url

    def reap_orphans(self, keep_ids: frozenset[str] = frozenset(), *, min_age_seconds: float = 120.0) -> int:
        self.reaped.append(keep_ids)
        return 0


class FakeStore:
    """内存 ObjectStore。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class _AgentHandler(BaseHTTPRequestHandler):
    """假 agent：/agent/health、/agent/input、/agent/archive、/agent/events、/echo。"""

    server_version = "fake-agent"
    contract_version = "1.0"

    def log_message(self, *args) -> None:  # 静音
        pass

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.startswith("/agent/health"):
            self._json(200, {"ok": True, "busy": False, "contractVersion": type(self).contract_version})
        elif self.path.startswith("/agent/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in (
                {"type": "RUN_STARTED", "seq": 1},
                {"type": "RUN_FINISHED", "seq": 2},
            ):
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        elif self.path.startswith("/echo"):
            self._json(200, {"method": "GET", "path": self.path})
        else:
            self._json(404, {"detail": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append((self.path, body))  # type: ignore[attr-defined]
        if self.path.startswith("/agent/input") or self.path.startswith("/agent/resume"):
            self._json(202, {"status": "accepted", "run_id": body.get("run_id", "r-1")})
        elif self.path.startswith("/agent/cancel"):
            self._json(200, {"ok": True, "cancelled": False})
        elif self.path.startswith("/agent/archive"):
            self._json(200, {"payload_key": "users/u1/sessions/s1/payload/x.tar.gz", "changed": True})
        elif self.path.startswith("/echo"):
            self._json(200, {"method": "POST", "path": self.path, "body": body})
        else:
            self._json(404, {"detail": "not_found"})


@pytest.fixture()
def fake_agent():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield type("FakeAgent", (), {"url": url, "server": server, "requests": server.requests})
    server.shutdown()
    thread.join(timeout=2.0)


@pytest.fixture()
def state(tmp_path, fake_agent):
    settings = Settings(
        service_token="",
        workspace_root=tmp_path / "workspaces",
        pool_capacity=2,
        idle_ttl_seconds=600.0,
        reap_interval_seconds=0.0,
        watch_interval_seconds=999.0,
        agent_image="fake:latest",
        workspace_skeleton_dirs=["inputs", "knowledge", "uploads"],
    )
    backend = FakeBackend(agent_url=fake_agent.url)
    pool = SandboxPool(backend, capacity=settings.pool_capacity, idle_ttl=settings.idle_ttl_seconds)
    store = FakeStore()
    notifier = WebhookNotifier("", token="")
    watcher = SandboxWatcher(pool, notifier, interval_seconds=999.0)
    st = ServiceState(settings=settings, pool=pool, store=store, watcher=watcher)
    yield st
    pool.shutdown()


@pytest.fixture()
def client(state):
    from fastapi.testclient import TestClient

    from sandbox_service.main import create_app

    with TestClient(create_app(state)) as c:
        yield c


def wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False

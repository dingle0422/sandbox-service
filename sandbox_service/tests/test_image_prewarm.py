"""镜像就位：拉取策略、预热端点、并发去重。

背景：跨机部署下 agent 镜像由 registry 分发，而 ``docker create`` **不会**自动拉，
缺镜像直接 ImageNotFound。这组用例锁住「发版方打一次 ``POST /images``，沙箱机自动
把新 tag 拉好」这条链路，以及 create 路径的兜底与失败语义。
"""

from __future__ import annotations

import threading
import time

import pytest

from sandbox_service.backend import ImagePullError
from sandbox_service.config import Settings, load_settings
from sandbox_service.tests.conftest import wait_until


# ── 策略 ─────────────────────────────────────────────────────────────────────
def test_policy_missing_pulls_only_when_absent(state):
    backend = state.pool.backend
    backend.images_present = False

    assert state.ensure_image("reg:5000/a:1") == "pulled"
    assert backend.pulled == ["reg:5000/a:1"]
    # 已在本机 → 稳态零开销，不再打 registry
    assert state.ensure_image("reg:5000/a:1") == "present"
    assert backend.pulled == ["reg:5000/a:1"]


def test_policy_always_pulls_even_if_present(state):
    backend = state.pool.backend
    assert backend.has_image("reg:5000/a:1")  # 缺省「都在」
    assert state.ensure_image("reg:5000/a:1", policy="always") == "pulled"
    assert backend.pulled == ["reg:5000/a:1"]


def test_policy_never_skips(state):
    state.pool.backend.images_present = False
    assert state.ensure_image("reg:5000/a:1", policy="never") == "skipped"
    assert state.pool.backend.pulled == []


def test_pull_policy_env_parsing(monkeypatch):
    monkeypatch.setenv("AGENT_IMAGE_PULL_POLICY", "ALWAYS")
    assert load_settings().image_pull_policy == "always"
    # 配错不该让服务起不来，回落 missing
    monkeypatch.setenv("AGENT_IMAGE_PULL_POLICY", "sometimes")
    assert load_settings().image_pull_policy == "missing"
    monkeypatch.delenv("AGENT_IMAGE_PULL_POLICY")
    assert load_settings().image_pull_policy == "missing"


# ── 建沙箱路径的兜底 ─────────────────────────────────────────────────────────
def test_create_pulls_missing_image(client, state):
    backend = state.pool.backend
    backend.images_present = False

    r = client.post("/sandboxes", json={"id": "s1", "image": "reg:5000/tax-agent:abc"})
    assert r.status_code == 200, r.text
    assert backend.pulled == ["reg:5000/tax-agent:abc"]


def test_create_reports_502_on_pull_failure(client, state):
    backend = state.pool.backend
    backend.images_present = False
    backend.pull_fails = True

    r = client.post("/sandboxes", json={"id": "s1", "image": "reg:5000/nope:1"})
    assert r.status_code == 502
    assert r.json()["detail"] == "image_pull_failed"
    assert backend.containers == {}  # 镜像没到位就不该造容器


# ── 预热端点 ─────────────────────────────────────────────────────────────────
def test_prewarm_is_async_and_reports_state(client, state):
    backend = state.pool.backend
    backend.images_present = False
    gate = threading.Event()
    real_pull = backend.pull_image

    def slow_pull(image: str) -> None:
        gate.wait(timeout=3.0)
        real_pull(image)

    backend.pull_image = slow_pull  # type: ignore[method-assign]

    r = client.post("/images", json={"image": "reg:5000/tax-agent:abc"})
    assert r.status_code == 202  # 不阻塞：拉取还在后台跑
    assert r.json()["state"] == "pulling"

    assert client.get("/images", params={"image": "reg:5000/tax-agent:abc"}).json()["state"] == "pulling"
    gate.set()
    assert wait_until(
        lambda: client.get("/images", params={"image": "reg:5000/tax-agent:abc"}).json()["state"]
        == "present"
    )
    assert backend.pulled == ["reg:5000/tax-agent:abc"]


def test_prewarm_defaults_to_deployment_image(client, state):
    r = client.post("/images", json={})
    assert r.status_code == 202
    assert r.json()["image"] == state.settings.agent_image


def test_prewarm_idempotent_when_already_present(client, state):
    r = client.post("/images", json={"image": "reg:5000/a:1"})
    assert r.json()["state"] == "present"
    assert state.pool.backend.pulled == []  # 本机已有 → 不重复拉


def test_prewarm_failure_visible_in_status(client, state):
    backend = state.pool.backend
    backend.images_present = False
    backend.pull_fails = True

    client.post("/images", json={"image": "reg:5000/nope:1"})
    assert wait_until(
        lambda: client.get("/images", params={"image": "reg:5000/nope:1"}).json()["state"] == "failed"
    )
    body = client.get("/images", params={"image": "reg:5000/nope:1"}).json()
    assert "nope:1" in body["error"]


def test_status_of_never_pulled_image(client, state):
    state.pool.backend.images_present = False
    body = client.get("/images", params={"image": "reg:5000/x:1"}).json()
    assert body["state"] == "absent"


# ── 并发 ─────────────────────────────────────────────────────────────────────
def test_concurrent_ensure_pulls_once(state):
    """预热线程与建沙箱可能同时要同一镜像；必须只拉一次，后到者等前者。"""
    backend = state.pool.backend
    backend.images_present = False
    entered = threading.Event()
    real_pull = backend.pull_image

    def slow_pull(image: str) -> None:
        entered.set()
        time.sleep(0.15)
        real_pull(image)

    backend.pull_image = slow_pull  # type: ignore[method-assign]

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(state.ensure_image("reg:5000/a:1")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert entered.is_set()
    assert backend.pulled == ["reg:5000/a:1"]  # 4 个请求只拉一次
    assert sorted(results) == ["present", "present", "present", "pulled"]


def test_prewarm_twice_does_not_double_pull(client, state):
    backend = state.pool.backend
    backend.images_present = False
    real_pull = backend.pull_image
    backend.pull_image = lambda image: (time.sleep(0.1), real_pull(image))[-1]  # type: ignore[method-assign]

    client.post("/images", json={"image": "reg:5000/a:1"})
    client.post("/images", json={"image": "reg:5000/a:1"})  # 在拉 → 不再起线程
    assert wait_until(lambda: backend.images_present or "reg:5000/a:1" in backend.images)
    time.sleep(0.15)
    assert backend.pulled == ["reg:5000/a:1"]


# ── Docker 后端的 tag 切分 ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "image,expect",
    [
        ("tax-agent:abc", ("tax-agent", "abc")),
        # registry 带端口：冒号不能被当成 tag 分隔符
        ("10.199.5.51:5000/tax-agent:96fc68b", ("10.199.5.51:5000/tax-agent", "96fc68b")),
        ("10.199.5.51:5000/tax-agent", ("10.199.5.51:5000/tax-agent", "latest")),
        ("python", ("python", "latest")),
    ],
)
def test_docker_pull_splits_repo_and_tag(image, expect):
    from sandbox_service.backend import DockerBackend

    calls: list[tuple] = []

    class _Images:
        def pull(self, repo, tag=None):
            calls.append((repo, tag))

    class _Cli:
        images = _Images()

    be = DockerBackend.__new__(DockerBackend)
    be._cli = lambda: _Cli()  # type: ignore[method-assign]
    be.pull_image(image)
    assert calls == [expect]


def test_docker_pull_wraps_error():
    from sandbox_service.backend import DockerBackend

    class _Images:
        def pull(self, repo, tag=None):
            raise RuntimeError("server gave HTTP response to HTTPS client")

    class _Cli:
        images = _Images()

    be = DockerBackend.__new__(DockerBackend)
    be._cli = lambda: _Cli()  # type: ignore[method-assign]
    with pytest.raises(ImagePullError) as ei:
        be.pull_image("10.199.5.51:5000/a:1")
    # 明文 registry 未放行是最常见的部署坑，原文必须透传到日志/响应里
    assert "HTTP response to HTTPS" in str(ei.value)


def test_settings_defaults_pull_policy():
    assert Settings().image_pull_policy == "missing"

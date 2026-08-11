"""DockerBackend.stop() 收尾复查单测：聚焦 409 / removing 态假阳性修复。

stop() 在 stop+remove 后会复查容器是否真的消失。Docker 删除是异步的--remove 返回
后容器先进入 ``removing`` 中间态（仍可被 containers.get 查到），过一会儿才真正消失。
修复前代码不区分 ``removing`` 与 ``running``，一律抛 ContainerStopError -> 假 500。

本套用假 docker client 覆盖：容器消失、一开始就不存在、409 竞态 removing、stop 无效仍 running。
"""

from __future__ import annotations

from docker.errors import APIError, NotFound

from sandbox_service.backend import ContainerState, ContainerStopError, DockerBackend


class FakeContainer:
    """模拟 docker-py Container：attrs 随 stop/remove 调用迁移。"""

    def __init__(
        self,
        cid: str,
        *,
        state: str = "running",
        stop_effective: bool = True,
        remove_exc: APIError | None = None,
        post_remove_state: str = "removing",
    ) -> None:
        self.id = cid
        self.short_id = cid[:12]
        self.attrs: dict = {
            "State": {"Status": state, "Running": state == "running", "ExitCode": None}
        }
        self.stop_calls = 0
        self.remove_calls = 0
        self._stop_effective = stop_effective
        self._remove_exc = remove_exc
        self._post_remove_state = post_remove_state
        self._gone = False

    def stop(self, timeout=None) -> None:  # noqa: D401  对齐 docker-py 签名
        self.stop_calls += 1
        if self._stop_effective:
            self.attrs["State"]["Status"] = "exited"
            self.attrs["State"]["Running"] = False

    def remove(self, force=True) -> None:
        self.remove_calls += 1
        if self._remove_exc is not None:
            # 并发删除竞态：另一个调用正在删，本容器进入 removing 中间态
            exc = self._remove_exc
            self._remove_exc = None
            self.attrs["State"]["Status"] = self._post_remove_state
            self.attrs["State"]["Running"] = self._post_remove_state == "running"
            raise exc
        # 成功：容器即将消失
        self._gone = True


class FakeContainers:
    def __init__(self) -> None:
        self._store: dict[str, FakeContainer] = {}

    def get(self, cid: str) -> FakeContainer:
        c = self._store.get(cid)
        if c is None or c._gone:
            raise NotFound(f"no such container: {cid}")
        return c

    def add(self, c: FakeContainer) -> None:
        self._store[c.id] = c


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


def _backend_with(c: FakeContainer) -> DockerBackend:
    client = FakeClient()
    client.containers.add(c)
    return DockerBackend(client=client)


def test_stop_success_when_container_disappears():
    """stop+remove 后容器消失（NotFound）-> 成功。"""
    c = FakeContainer("c1")
    b = _backend_with(c)
    b.stop("c1")
    assert c.stop_calls == 1
    assert c.remove_calls == 1


def test_stop_idempotent_when_not_found():
    """容器一开始就不存在 -> 直接成功（幂等），不调 stop/remove。"""
    b = DockerBackend(client=FakeClient())
    b.stop("nope")  # 不抛


def test_stop_treats_removing_state_as_success():
    """409 竞态：remove 抛 409 但容器处于 removing 态 -> 视为成功，不抛 ContainerStopError。"""
    c = FakeContainer(
        "c1",
        remove_exc=APIError("409 Client Error: removal already in progress"),
        post_remove_state="removing",
    )
    b = _backend_with(c)
    b.stop("c1")  # 修复前这里会抛 ContainerStopError
    assert c.remove_calls == 1


def test_stop_raises_when_still_running():
    """stop 无效 + remove 失败后容器仍 running（非 removing）-> 抛 ContainerStopError。"""
    c = FakeContainer(
        "c1",
        state="running",
        stop_effective=False,  # stop 调了但容器没停
        remove_exc=APIError("500 something failed"),
        post_remove_state="running",  # remove 失败，容器没进 removing
    )
    b = _backend_with(c)
    try:
        b.stop("c1")
    except ContainerStopError as exc:
        assert "running" in str(exc)
    else:
        raise AssertionError("expected ContainerStopError for still-running container")


def test_stop_raises_when_still_exited():
    """容器 exited（非 removing）仍可查到 -> 抛 ContainerStopError（带 state）。"""
    from sandbox_service.backend import ContainerState as CS

    c = FakeContainer(
        "c1",
        state="exited",
        stop_effective=True,
        remove_exc=APIError("500 failed"),
        post_remove_state="exited",
    )
    b = _backend_with(c)
    try:
        b.stop("c1")
    except ContainerStopError as exc:
        assert "exited" in str(exc)
    else:
        raise AssertionError("expected ContainerStopError for still-exited container")

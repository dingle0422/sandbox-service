"""DockerBackend 销毁语义：停不掉必须报错，账本外容器也要停，在途容器不能误杀。

回归背景：曾出现「DELETE /sandboxes 回 200、账本 live=0，但 Docker 里容器一直在跑」
的孤儿——stop 吞掉全部异常、删除只认内存账本、孤儿回收只在启动时跑一次。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sandbox_service.backend import (
    LABEL_ROLE,
    LABEL_SANDBOX_ID,
    ROLE_VALUE,
    ContainerStopError,
    DockerBackend,
)


class _NotFound(Exception):
    """替身 docker.errors.NotFound（避免测试依赖 docker SDK）。"""


class _StubContainer:
    def __init__(
        self,
        cid: str,
        *,
        sandbox_id: str = "s1",
        age_seconds: float = 3600.0,
        undead: bool = False,
    ) -> None:
        self.id = cid
        self.short_id = cid[:12]
        self.labels = {LABEL_ROLE: ROLE_VALUE, LABEL_SANDBOX_ID: sandbox_id}
        created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        # 刻意造 docker 的纳秒精度时间戳，顺带覆盖小数截断
        self.attrs = {"Created": created.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"}
        self._age = age_seconds
        self.undead = undead
        self.gone = False
        self.stopped = False

    def stop(self, timeout: int = 5) -> None:
        if self.undead:
            raise RuntimeError("docker stop failed")
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        if self.undead:
            raise RuntimeError("docker remove failed")
        self.gone = True


class _StubContainers:
    def __init__(self, items: list[_StubContainer]) -> None:
        self.items = items

    def get(self, cid: str):
        for c in self.items:
            if not c.gone and cid in (c.id, c.short_id):
                return c
        raise _NotFound(cid)

    def list(self, all: bool = False, filters: dict | None = None):  # noqa: A002
        key, _, value = ((filters or {}).get("label") or "").partition("=")
        return [
            c for c in self.items if not c.gone and (c.labels.get(key) == value if value else key in c.labels)
        ]


class _StubClient:
    def __init__(self, items: list[_StubContainer]) -> None:
        self.containers = _StubContainers(items)


@pytest.fixture()
def make_backend(monkeypatch):
    monkeypatch.setattr(DockerBackend, "_not_found_cls", staticmethod(lambda: _NotFound))

    def _make(items: list[_StubContainer]) -> DockerBackend:
        return DockerBackend(client=_StubClient(items))

    return _make


def test_stop_removes_container(make_backend):
    c = _StubContainer("abcdef123456")
    make_backend([c]).stop(c.id)
    assert c.stopped and c.gone


def test_stop_is_idempotent_when_container_absent(make_backend):
    make_backend([]).stop("nope")  # 不抛即通过


def test_stop_raises_when_container_survives(make_backend):
    """核心回归：stop/remove 都失败时不能静默返回，否则上层摘掉租约就造出孤儿。"""
    c = _StubContainer("deadbeef0000", undead=True)
    with pytest.raises(ContainerStopError):
        make_backend([c]).stop(c.id)
    assert not c.gone


def test_stop_for_sandbox_covers_containers_missing_from_ledger(make_backend):
    """账本丢租约时的唯一救命稻草：按 label 找回该沙箱名下所有容器。"""
    a = _StubContainer("aaaaaaaaaaaa", sandbox_id="s1")
    b = _StubContainer("bbbbbbbbbbbb", sandbox_id="s1")
    other = _StubContainer("cccccccccccc", sandbox_id="s2")
    backend = make_backend([a, b, other])
    assert backend.stop_for_sandbox("s1") == 2
    assert a.gone and b.gone and not other.gone
    assert backend.stop_for_sandbox("s1") == 0  # 幂等


def test_reap_orphans_skips_in_flight_containers(make_backend):
    """acquire 先起容器后登记租约，巡检必须放过刚创建的，否则误杀在途沙箱。"""
    young = _StubContainer("111111111111", age_seconds=5)
    old = _StubContainer("222222222222", age_seconds=3600)
    backend = make_backend([young, old])
    assert backend.reap_orphans(min_age_seconds=120.0) == 1
    assert old.gone and not young.gone


def test_reap_orphans_reads_epoch_created_from_list_api(make_backend):
    """``containers.list()`` 的 Created 是 Unix 秒（只有 inspect 才是 ISO 串）。

    只认字符串时年龄保护会静默失效——刚创建的容器照样被当孤儿杀掉。
    """
    young = _StubContainer("444444444444", age_seconds=5)
    young.attrs = {"Created": datetime.now(timezone.utc).timestamp() - 5}
    backend = make_backend([young])
    assert backend.reap_orphans(min_age_seconds=120.0) == 0
    assert not young.gone


def test_reap_orphans_keeps_leased_containers(make_backend):
    leased = _StubContainer("333333333333", age_seconds=3600)
    backend = make_backend([leased])
    assert backend.reap_orphans(keep_ids=frozenset({leased.short_id}), min_age_seconds=0.0) == 0
    assert not leased.gone

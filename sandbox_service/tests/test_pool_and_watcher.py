"""池治理（TTL 逐出候选、僵尸清理）与 watcher 通知。"""

import time

from sandbox_service.watcher import SandboxWatcher, WebhookNotifier


def _spec(state, sid):
    return state.build_spec(sid)


def test_build_spec_sets_session_paths_and_normalizes_legacy_values(state):
    defaults = state.build_spec("defaults")
    legacy = state.build_spec(
        "legacy", env={"WORKSPACE": "/workspace", "DEBUG_DIR": "/tmp/debug", "KEEP": "yes"}
    )
    custom = state.build_spec("custom", env={"WORKSPACE": "/data", "DEBUG_DIR": "/logs"})

    assert defaults.env["WORKSPACE"] == "/session/workspace"
    assert defaults.env["DEBUG_DIR"] == "/session/debug"
    assert legacy.env == {
        "WORKSPACE": "/session/workspace",
        "DEBUG_DIR": "/session/debug",
        "KEEP": "yes",
    }
    assert custom.env == {"WORKSPACE": "/data", "DEBUG_DIR": "/logs"}


def test_ttl_marks_evict_candidate_without_stopping(state):
    state.pool._idle_ttl = 0.01
    cid, reused = state.pool.acquire("s1", _spec(state, "s1"))
    assert not reused
    state.pool.release("s1")
    time.sleep(0.05)
    marked = state.pool.reap_now()
    assert marked == ["s1"]
    # 只标记不销毁
    assert state.pool.backend.containers[cid]["running"] is True
    assert state.pool.stats()["evict_candidates"] == ["s1"]
    # 有活动即移出候选
    state.pool.touch("s1")
    assert state.pool.stats()["evict_candidates"] == []


def test_leased_sandbox_never_evicted(state):
    state.pool._idle_ttl = 0.01
    state.pool.acquire("s1", _spec(state, "s1"))  # leased=1，不 release
    time.sleep(0.05)
    assert state.pool.reap_now() == []


def test_forget_clears_zombie(state):
    state.pool.acquire("s1", _spec(state, "s1"))
    assert state.pool.forget("s1") is True
    assert state.pool.get_lease("s1") is None
    assert state.pool.forget("s1") is False


def test_watcher_sweeps_orphans_periodically(state):
    """回归：孤儿回收以前只在启动时跑一次，运行期漏掉的容器要等重启才有人管。"""
    backend = state.pool.backend
    watcher = SandboxWatcher(
        state.pool, WebhookNotifier("", token=""), interval_seconds=999.0, orphan_sweep_seconds=60.0
    )
    state.pool.acquire("s1", _spec(state, "s1"))

    watcher.tick()
    assert len(backend.reaped) == 1
    # 在租容器必须出现在 keep 名单里，避免自杀
    assert backend.reaped[0] == frozenset({state.pool.get_lease("s1").container_id})

    watcher.tick()  # 未到周期 → 不重复扫
    assert len(backend.reaped) == 1


def test_watcher_orphan_sweep_can_be_disabled(state):
    watcher = SandboxWatcher(
        state.pool, WebhookNotifier("", token=""), interval_seconds=999.0, orphan_sweep_seconds=0
    )
    watcher.tick()
    assert state.pool.backend.reaped == []


def test_watcher_notifies_exit_and_evict(state):
    events: list[tuple] = []
    watcher = SandboxWatcher(
        state.pool,
        WebhookNotifier("", token=""),
        interval_seconds=999.0,
        notify_fn=lambda *a: events.append(a),
    )
    state.pool._idle_ttl = 0.01
    cid, _ = state.pool.acquire("s1", _spec(state, "s1"))
    state.pool.release("s1")
    time.sleep(0.05)  # 越过 TTL

    watcher.tick()  # 记录 running 基线；TTL 已过 → evict candidate
    kinds = [e[0] for e in events]
    assert "evict_candidate" in kinds

    state.pool.backend.stop(cid)  # 容器退出
    watcher.tick()
    kinds = [e[0] for e in events]
    assert "exited" in kinds
    exited = [e for e in events if e[0] == "exited"][0]
    assert exited[1] == "s1" and exited[2] == cid


def test_per_sandbox_callback_url_overrides_default(state, monkeypatch):
    """按沙箱自带 callback_url 优先于部署级默认——服务无需持有全局应用地址。"""
    import httpx

    from sandbox_service.watcher import SandboxWatcher, WebhookNotifier

    posted: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append({"url": url, "json": json})

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(httpx, "post", fake_post)

    notifier = WebhookNotifier("http://default-sink/hook", token="")
    watcher = SandboxWatcher(state.pool, notifier, interval_seconds=999.0)

    # s1 自带 sink；s2 无，回落部署级默认
    cid1, _ = state.pool.acquire("s1", _spec(state, "s1"), meta={"callback_url": "http://app-a/cb"})
    cid2, _ = state.pool.acquire("s2", _spec(state, "s2"))
    watcher.tick()  # 建立 running 基线
    state.pool.backend.stop(cid1)
    state.pool.backend.stop(cid2)
    watcher.tick()

    sinks = {p["json"]["sandbox_id"]: p["url"] for p in posted}
    assert sinks["s1"] == "http://app-a/cb"
    assert sinks["s2"] == "http://default-sink/hook"


def test_no_callback_configured_is_silent(state, monkeypatch):
    import httpx

    from sandbox_service.watcher import SandboxWatcher, WebhookNotifier

    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(a))
    watcher = SandboxWatcher(state.pool, WebhookNotifier("", token=""), interval_seconds=999.0)
    cid, _ = state.pool.acquire("s1", _spec(state, "s1"))
    watcher.tick()
    state.pool.backend.stop(cid)
    watcher.tick()
    assert called == []  # 无 sink 即静默，不主动联系任何地址

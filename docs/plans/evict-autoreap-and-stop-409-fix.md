# Plan: evict_candidate 超期自动回收 + stop() 409 假阳性修复

## 背景与根因（已诊断确认）

1. **两个沙箱存活 25h/3h**：调用方做完归档任务后不发 `DELETE`；服务按设计「不自行销毁 evict_candidate」（`pool.py` / `watcher.py` 模块 docstring 明文），只标记 + 发 webhook；部署未配 `CALLBACK_URL`，evict 通知发不出去；回收器因这两个容器仍在账本（`keep_ids`）而跳过。→ 空闲沙箱永久存活，直到服务重启被启动孤儿扫描兜底。
2. **409 假阳性**：`DockerBackend.stop()` 收尾复查（`backend.py:218-224`）用 `containers.get()` 判断容器是否消失，但 Docker 删除是异步的——`remove(force=True)` 返回后容器先进入 `removing` 中间态，期间仍可被 `get()` 查到，于是误判「容器仍存在」抛 `ContainerStopError` → `delete_sandbox` 兜成 500。触发场景不止并发 DELETE（端口 45160/45166 竞态），**单次 DELETE 内 `terminate`→`stop_for_sandbox` 两次 `stop` 同一容器也会撞上**。

## 改动一：修复 stop() 的 409 假阳性

**文件**：`sandbox_service/backend.py`，`stop()` 方法收尾复查段（约 L218-224）

**现状**：
```python
try:
    self._cli().containers.get(container_id)
except not_found:
    return
except Exception as exc:
    raise ContainerStopError(f"容器复查失败 cid={container_id}: {exc}") from exc
raise ContainerStopError(f"stop/remove 后容器仍存在 cid={container_id}")
```

**改为**：复查时解析容器状态，`removing` 态视为成功（Docker 内部会完成移除），仅非 `removing` 仍存在才报错。复用已有的 `inspect()` 与 `ContainerState.REMOVING`：
```python
try:
    st = self.inspect(container_id)
except not_found:
    return
except Exception as exc:
    raise ContainerStopError(f"容器复查失败 cid={container_id}: {exc}") from exc
if st.state == ContainerState.REMOVING:
    # 并发删除竞态：remove 返回 409 "removal already in progress" 后容器处于
    # removing 中间态仍可查到。Docker 会在内部完成移除，不必误报失败触发调用方重试。
    logger.info("容器移除进行中，视为成功 cid=%s", container_id)
    return
raise ContainerStopError(f"stop/remove 后容器仍存在 cid={container_id} state={st.state.value}")
```

**不改**：`stop`/`remove` 步骤的 `except Exception` WARNING 日志保留（409 是有用的并发信号）。

## 改动二：evict_candidate 超期自动回收（opt-in，默认关闭）

### 2.1 `_evict_candidates` 由 `set` 改为 `dict[sid, marked_at]`
**文件**：`sandbox_service/pool.py`

| 行 | 现状 | 改为 |
|----|------|------|
| L56 | `self._evict_candidates: set[str] = set()` | `self._evict_candidates: dict[str, float] = {}` |
| L86,105,123,128,135 | `self._evict_candidates.discard(sandbox_id)` | `self._evict_candidates.pop(sandbox_id, None)` |
| L194-195 | `if sid not in self._evict_candidates:` + `self._evict_candidates.add(sid)` | `if sid not in self._evict_candidates:` + `self._evict_candidates[sid] = now` |

L220 `sorted(self._evict_candidates)`、L230 `.clear()`、L194 `in` 判断对 dict 与 set 行为一致，无需改。

### 2.2 新增 `SandboxPool.reap_expired_candidates`
**文件**：`sandbox_service/pool.py`（置于 `reap_orphan_containers` 附近）

```python
def reap_expired_candidates(self, *, grace_seconds: float) -> list[tuple[str, str]]:
    """opt-in 自动回收：成为 evict_candidate 超过 grace_seconds 的沙箱一律销毁。

    grace_seconds<=0 时不做任何事（保持「服务不自行销毁」契约）。
    返回本轮销毁的 (sandbox_id, container_id) 列表。

    原子性：check 年龄 + pop 租约/候选标记必须在同一把锁内完成，否则 ``touch``
    可能在锁间隙把一个正要回收的活跃沙箱移出候选——而租约还在——导致误杀。
    ``stop`` 是慢 Docker 调用，放到锁外执行；stop 失败则容器成孤儿，
    交由 ``reap_orphans`` 兜底（此时已不在 keep_ids）。
    """
    if grace_seconds <= 0:
        return []
    now = time.time()
    to_stop: list[tuple[str, Lease]] = []
    with self._lock:
        for sid, marked_at in list(self._evict_candidates.items()):
            if (now - marked_at) < grace_seconds:
                continue
            lease = self._leases.pop(sid, None)
            self._evict_candidates.pop(sid, None)
            if lease is not None:
                to_stop.append((sid, lease))
    destroyed: list[tuple[str, str]] = []
    for sid, lease in to_stop:
        try:
            self._backend.stop(lease.container_id)
        except Exception:
            logger.exception("evict 超期回收 stop 失败 sandbox=%s", sid)
        destroyed.append((sid, lease.container_id))
    if destroyed:
        logger.info("evict_candidate 超期自动回收 grace=%ss count=%d ids=%s",
                    grace_seconds, len(destroyed), [s for s, _ in destroyed])
    return destroyed
```

**不删工作区**：与 `terminate` 默认一致（工作区归属由调用方管理）。

### 2.3 watcher 集成
**文件**：`sandbox_service/watcher.py`

- `__init__` 新增形参 `evict_grace_seconds: float = 0.0`，存 `self._evict_grace`。
- `tick()` 在第 (2) 步 `reap_now` + 通知候选 之后、第 (4) 步 `sweep_orphans` 之前，插入 `self._reap_expired_candidates()`。
- 新增方法：
```python
def _reap_expired_candidates(self) -> None:
    if self._evict_grace <= 0:
        return
    try:
        destroyed = self._pool.reap_expired_candidates(grace_seconds=self._evict_grace)
    except Exception:
        logger.exception("evict 超期回收异常")
        return
    for sid, cid in destroyed:
        self._emit("evicted", sid, cid, "idle_ttl_grace_expired")
```

**已知限制（计划内标注，不本次做）**：租约已被 pop，`_emit` 取不到 per-sandbox `callback_url`，回落到部署级 `CALLBACK_URL`。如需保留 per-sandbox URL，`reap_expired_candidates` 需额外回传 `meta`——留作后续增强。

### 2.4 配置
**文件**：`sandbox_service/config.py`
- `Settings` 新增字段：
```python
#: evict_candidate 自动回收宽限期（秒）。<=0 关闭（默认，保持「服务不自行销毁」契约）。
#: 开启后：沙箱被标记为 evict_candidate 超过该时长仍无人认领（无流量、未 DELETE），
#: 由 watcher 自动 stop+摘租约。总存活 ≈ IDLE_TTL_SECONDS + EVICT_GRACE_SECONDS。
evict_grace_seconds: float = 0.0
```
- `load_settings` 新增 `evict_grace_seconds=_f("EVICT_GRACE_SECONDS", 0.0)`

**文件**：`sandbox_service/service.py`
- `build_state` 中 `SandboxWatcher(...)` 调用新增 `evict_grace_seconds=s.evict_grace_seconds`

### 2.5 部署配置
**文件**：`deploy/docker-compose.yml` — `environment` 新增：
```yaml
EVICT_GRACE_SECONDS: ${EVICT_GRACE_SECONDS:-0}
```
**文件**：`deploy/.env.example` — 新增带说明条目：
```env
# evict_candidate 自动回收宽限期（秒）。<=0 关闭（默认：服务不自行销毁空闲沙箱，等调用方 DELETE）。
# 开启后：空闲超 IDLE_TTL_SECONDS 被标记为候选，再超 EVICT_GRACE_SECONDS 仍无人认领则自动销毁。
# 总存活 ≈ IDLE_TTL_SECONDS + EVICT_GRACE_SECONDS。调用方不发 DELETE 的部署建议开启（如 3600）。
EVICT_GRACE_SECONDS=0
```

## 改动三：测试（新建 `tests/unit/`）

现有只有 conformance 套件（`_FakeBackend`），无 pool/backend 单测。竞态与时间逻辑用 mock 单测覆盖。

### `tests/unit/test_backend_stop.py`
用假 docker client（模拟 `containers.get` / `c.stop` / `c.remove`）覆盖 `DockerBackend.stop()`：
1. stop+remove 后 `get` 抛 `NotFound` → 成功（原有行为不回归）
2. stop+remove 后 `get` 返回 `State.Status=removing` 容器 → **不抛** ContainerStopError（修复点）
3. stop+remove 后容器仍 `running` → 抛 ContainerStopError（带 state）
4. `remove` 抛 409 但容器随后 `removing` → 不抛（并发场景）
5. 容器一开始就 `NotFound` → 直接成功（幂等）

### `tests/unit/test_pool_reap.py`
用实现 `ContainerBackend` 协议的假后端覆盖 `SandboxPool.reap_expired_candidates`：
1. `grace_seconds<=0` → 返回 `[]`，不动任何租约
2. candidate 年龄 `< grace` → 不回收
3. candidate 年龄 `>= grace` → 回收：租约被 pop、`backend.stop` 被调用、返回值含 (sid,cid)
4. **竞态安全**：标记为候选后、回收前调 `touch(sid)`（取消候选）→ 该 sid 不被回收（验证 check+pop 原子性）
5. `stats()["evict_candidates"]` 仍返回排序 sid 列表（dict 改造不回归）
6. `reap_now` 标记候选后，`_evict_candidates[sid]` 存的是时间戳

### `tests/unit/test_watcher_tick.py`（可选，优先级低）
- `evict_grace>0` 时 `tick` 触发 `reap_expired_candidates` 并 `_emit("evicted",...)`
- `evict_grace=0` 时 `tick` 不触发

## 改动四：文档

**文件**：`docs/sandbox-lifecycle.md`
- 治理章节补充 `EVICT_GRACE_SECONDS`：opt-in 自动回收语义、默认关闭、与 `IDLE_TTL_SECONDS` 的叠加关系（总存活 ≈ idle_ttl + evict_grace）、与 `CALLBACK_URL`/调用方 DELETE 的取舍。
- webhook 事件枚举新增 `evicted`（`reason=idle_ttl_grace_expired`），与现有 `evict_candidate`/`exited`/`dead` 并列。

**文件**：`docs/schemas/sandbox-lifecycle.schema.json` + `docs/fixtures/sandbox-lifecycle/WebhookNotification.evict.json`
- 若 schema 中 webhook `kind` 为枚举，追加 `evicted`；补一个 `evicted` fixture。

## 验证

- `pytest tests/unit/ -v` 全绿
- `pytest tests/lifecycle_conformance/ -v` 不回归（自托管 FakeBackend 路径）
- 手动：`EVICT_GRACE_SECONDS=60 IDLE_TTL_SECONDS=10` 起服务，建沙箱→空闲→约 70s 后容器被自动 stop，日志见 `evict_candidate 超期自动回收 grace=60.0s count=1`
- 手动：对已在册的两个历史孤儿，重启服务后由启动 `reap_orphan_containers` 兜底清掉（label 在、账本空、年龄足）

## 风险与取舍

- **设计前提反转**：开启 `EVICT_GRACE_SECONDS` 后服务会自行销毁空闲沙箱，与原 docstring「服务不自行销毁」相悖。以默认 0（关闭）+ opt-in 缓解，docstring 同步更新为「默认不自行销毁，开启 EVICT_GRACE_SECONDS 后例外」。
- **per-sandbox callback 丢失**：自动回收时租约已 pop，evicted 通知只能走部署级 CALLBACK_URL（见 2.3 已知限制）。
- **stop 失败的容器**：自动回收 stop 失败 → 容器成孤儿，由 60s 周期 `reap_orphans` 兜底（已验证该路径工作）。
- **不删工作区**：自动回收只 stop 容器+摘租约，工作区保留（调用方可事后排查/复用）。

## Obsidian 同步（规则一）

完成后向 `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/lebrain/projects/sandbox-service/` 写：
- `plans/prd_name_空漏回收与stop409修复_20260811.md`（本 plan 全文）
- `coding logs/log_name_空漏回收与stop409修复_20260811.md`（开发+测试结果+commit）

# 应用侧沙箱回收对接计划（Application-Side Sandbox Recycle Integration Plan）

> 状态：Draft v1 · 交付对象：应用层（vm1 会话管理服务）· 编写日期：2026-07-28
> 依据：[`docs/sandbox-lifecycle.md`](docs/sandbox-lifecycle.md) v1.0、`sandbox_service/` 现状代码
> 性质：本计划为**应用侧开发规范**，沙箱服务（本仓库）侧不改代码。

---

## 0. 文档定位

本计划规定应用层如何对接 `sandbox_service` 的沙箱回收机制，使「用户退出工作空间后沙箱被及时关闭」成为系统稳态保证。所有接口契约以 [`docs/sandbox-lifecycle.md`](docs/sandbox-lifecycle.md) 为准，本文摘录应用侧必须遵守的部分并标注代码溯源。

**前置事实**：沙箱服务是**被动组件**（契约 §0），不感知「工作空间」「会话」「用户」等业务语义。容器是否被关闭，最终由应用层是否调用 `DELETE /sandboxes/{id}` 决定。沙箱服务侧的空闲 TTL 机制**只通知不销毁**，详见 §1.2。

---

## 1. 背景与问题

### 1.1 现象

前端退出工作空间 20 余分钟后，对应沙箱容器仍在运行，未被关闭。

### 1.2 根因：三层防线与当前断点

沙箱服务的容器回收存在三层防线，当前三层均未命中该场景：

| 防线 | 职责 | 触发方 | 当前状态 |
| --- | --- | --- | --- |
| **L1** 退出即销毁 | 用户退出工作空间时，主动 `DELETE /sandboxes/{id}` | 应用层（业务事件驱动） | **未接**：应用层未在退出事件中调用 DELETE |
| **L2** 空闲 TTL 兜底 | 空闲超 `IDLE_TTL_SECONDS`（默认 600s）后，沙箱服务发 `evict_candidate` webhook，应用层消费后归档 + DELETE | 沙箱服务通知 + 应用层消费 | **失效**：`CALLBACK_URL` 为空、建沙箱未传 `callback_url`，webhook 无 sink；应用层亦未轮询 `/capacity` 兜底 |
| **L3** 孤儿巡检 | 回收「带本服务 label 但不在账本内」的残留容器 | 沙箱服务自行（`ORPHAN_SWEEP_SECONDS=60`） | **正常但无关**：该沙箱仍在账本内，L3 按 `keep_ids` 跳过，不会回收 |

关键代码事实：

- `reap_now()` 空闲超 TTL **只标记 `evict_candidates`，不 stop 容器**（[`pool.py:184-198`](sandbox_service/pool.py#L184-L198)）。
- webhook sink 优先级：按沙箱 `callback_url` > 部署级 `CALLBACK_URL`；**两者皆空则直接 return，不发任何通知**（[`watcher.py:52-54`](sandbox_service/watcher.py#L52-L54)）。
- 代理透传后会 `touch(sandbox_id)` 刷新 `last_active`（[`api.py:237`](sandbox_service/api.py#L237)）；若退出后仍有残留流量（SSE 未断、健康轮询），TTL 永不触发，L2 连候选都不会标记。
- L3 只回收 `keep_ids`（账本内 container_id 集合）之外的容器（[`backend.py:264`](sandbox_service/backend.py#L264)），账本内活沙箱不被回收。

**结论**：本仓库代码符合契约设计，无需修改。问题完全在应用侧：L1 未接、L2 无 sink 且未消费。本计划规定 L1/L2 的应用侧实现规范。

---

## 2. 目标与非目标

### 2.1 目标

- G1：用户退出工作空间后，对应沙箱在约定 SLA 内被关闭（DELETE 完成）。
- G2：应用层异常（崩溃、重启丢状态、DELETE 失败）导致 L1 漏删时，L2 兜底回收，无长期泄漏。
- G3：回收过程可观测，泄漏可被发现与告警。

### 2.2 非目标

- N1：不修改沙箱服务（本仓库）代码或契约。
- N2：不实现沙箱服务侧的「空闲硬回收」（即不让服务自行 stop 账本内沙箱）——这会破坏契约 §0 边界，不在本计划范围。
- N3：不改变 L3 孤儿巡检行为（沙箱服务已自管，应用层无需干预）。
- N4：不涵盖工作区数据归档的内容格式（归档协议见 [`docs/agent-contract.md`](docs/agent-contract.md) §2.7，由容器内 agent 实现，应用层仅经代理触发）。

---

## 3. 对接契约（应用侧必须遵守）

### 3.1 鉴权与基址

- 基址：`http://<sandbox-service-host>:<SANDBOX_SERVICE_PORT>`（默认 8001）。
- 鉴权：除 `GET /health` 外所有端点要求 `Authorization: Bearer <SERVICE_TOKEN>`（[`config.py:100`](sandbox_service/config.py#L100)，兼容旧 `VM2_SERVICE_TOKEN`）。
- token 等值校验，签发与轮换归应用层运维。

### 3.2 沙箱生命周期 API

| 方法 | 路径 | 语义 | 溯源 |
| --- | --- | --- | --- |
| POST | `/sandboxes` | 创建或幂等复用；body 含 `id`、`env`、`callback_url?` 等 | [`api.py:122-169`](sandbox_service/api.py#L122-L169) |
| GET | `/sandboxes/{sid}` | 状态：`{state, running, exit_code, started_at, probe?}` | [`api.py:171-197`](sandbox_service/api.py#L171-L197) |
| DELETE | `/sandboxes/{sid}?grace_seconds=5` | 停容器（SIGTERM→grace→SIGKILL+remove）；幂等；返回 `{ok, terminated}` | [`api.py:199-217`](sandbox_service/api.py#L199-L217) |

DELETE 契约要点（契约 §2.2）：

- **销毁结果以 Docker 为准，不以服务内存账本为准**：摘租约后仍按 `sandbox_id` label 扫一遍容器（[`api.py:213`](sandbox_service/api.py#L213) `stop_for_sandbox`）。
- 扫尾失败返回 **500 `terminate_failed`**，应用层**必须重试**（[`api.py:214-216`](sandbox_service/api.py#L214-L216)）。
- **回 200 却留容器在跑是契约违规**——应用层不得将 200 视为绝对真相，需以验证为准（见 §7）。

POST `/sandboxes` 的 `callback_url` 字段（[`api.py:64`](sandbox_service/api.py#L64)）：本沙箱事件 sink，缺省回落部署级 `CALLBACK_URL`。**建议每次创建时显式传入**，使服务无需持有全局应用地址（契约 §2.6）。

### 3.3 webhook 事件

沙箱服务在下列时刻 POST 通知 sink（[`watcher.py:43-71`](sandbox_service/watcher.py#L43-L71)、[`watcher.py:113-149`](sandbox_service/watcher.py#L113-L149)）：

```jsonc
// POST {sink}   Header: Authorization: Bearer <SERVICE_TOKEN>
{
  "kind": "evict_candidate" | "dead" | "exited",
  "sandbox_id": "s-123",
  "container_id": "…",
  "reason": "idle_ttl" | "health_probe_failed" | "oom" | "exit",
  "ts": "2026-07-28T09:00:00Z"
}
```

投递语义：

- sink 优先级：按沙箱 `callback_url` > 部署级 `CALLBACK_URL`；**皆空则不通知**（[`watcher.py:52-54`](sandbox_service/watcher.py#L52-L54)）。
- **尽力而为**：失败重试 3 次，退避 1s/2s/4s，放弃后仅记日志（[`watcher.py:63-71`](sandbox_service/watcher.py#L63-L71)）。应用层**不能假设 webhook 必达**，必须以轮询 `/capacity` 作为兜底（R2.3）。
- `evict_candidate`：沙箱服务**不自行销毁**，应用层收到后决策（建议先归档再 DELETE，见 §5 T3）。
- `dead` / `exited`：容器已死亡/退出（健康探测连续失败 / OOM / exit），应用层据此清理本地账本与残留状态（R4）。

### 3.4 容量与状态查询

`GET /capacity`（[`api.py:91-93`](sandbox_service/api.py#L91-L93)、[`pool.py:208-221`](sandbox_service/pool.py#L208-L221)）：

```jsonc
{
  "live": 3, "leased": 1, "idle": 2, "capacity": 8,
  "idleTtl": 600,
  "evict_candidates": ["s-123", "s-456"]
}
```

应用层以此轮询发现 L1 漏删的空闲沙箱（R2.3）。

### 3.5 L3 边界说明（应用层无需实现，需知晓）

- L3 由沙箱服务自行执行：周期 `ORPHAN_SWEEP_SECONDS=60`，启动时额外跑一次（[`main.py:49`](sandbox_service/main.py#L49)、[`watcher.py:161-174`](sandbox_service/watcher.py#L161-L174)）。
- 只回收「带本服务 label、不在 `keep_ids`（账本内）、年龄 > `ORPHAN_MIN_AGE_SECONDS=120`」的容器（[`backend.py:241-276`](sandbox_service/backend.py#L241-L276)）。
- **账本内的活沙箱不会被 L3 回收**——这是 L1/L2 必须存在的根本原因：只要应用层没摘租约（没调 DELETE），沙箱对 L3 不可见。
- 年龄保护 120s 是为放行「在途创建」（`acquire` 在锁外 create/start 后才登记租约，[`pool.py:90-104`](sandbox_service/pool.py#L90-L104)），应用层无需关心。

---

## 4. 需求规格

### R1 退出即销毁（L1 主路径）

用户退出工作空间的业务事件触发后，应用层须在 SLA 内对对应沙箱调用 `DELETE /sandboxes/{id}`。

- **R1.1** 应用层须维护「工作空间/会话 → sandbox_id」映射。建沙箱时 `POST /sandboxes` 的 `id` 由调用方给定（契约 §1：`id` = 调用方稳定标识，本仓场景 = session_id），退出时据此定位。
- **R1.2** DELETE 须幂等重试：网络失败 / 5xx / 超时按退避重试，至少 3 次；最终失败须告警并保留待补偿任务，不得静默丢弃。
- **R1.3** DELETE 返回 500 `terminate_failed` 时，必须重试（扫尾失败，容器可能仍在，[`api.py:214-216`](sandbox_service/api.py#L214-L216)）。
- **R1.4** 退出事件与 DELETE 解耦：若退出事件处理异步化，DELETE 失败不得阻塞用户退出流程，但须进入补偿队列。
- **R1.5** SLA：退出触发后，p95 ≤ 30s 发起首次 DELETE；最终一致（容器确认消失）p95 ≤ 5min（含重试）。

### R2 evict_candidate 兜底消费（L2）

- **R2.1（sink 配置）** 须配置 callback sink：部署级 `CALLBACK_URL` 或建沙箱时传 `callback_url`（推荐后者，见 R3）。二者至少满足其一，否则 webhook 不发。
- **R2.2（webhook 接收）** 应用层须实现 webhook 接收端点，校验 Bearer token，处理 `evict_candidate` / `dead` / `exited` 三类事件。
- **R2.3（轮询兜底）** 因 webhook 尽力而为可能丢失，应用层须定时轮询 `GET /capacity`（建议周期 ≤ `IDLE_TTL_SECONDS` 的一半，即 ≤ 300s），对 `evict_candidates` 中未被 webhook 处理的沙箱执行归档 + DELETE。
- **R2.4（先归档后销毁）** 处理 `evict_candidate` 时，**先经代理触发归档，再 DELETE**（契约 §5 时序）：
  1. `POST /sandboxes/{id}/proxy/8080/agent/archive`（抢救工作区数据，见 [`docs/agent-contract.md`](docs/agent-contract.md) §2.7）
  2. 归档成功（或确认无需归档）后 `DELETE /sandboxes/{id}`
  - 归档失败时**不得直接 DELETE**，须重试或转人工，避免数据丢失。
- **R2.5（去重）** 同一 sandbox_id 的 evict_candidate 可能被 webhook 与轮询双通道重复触发，须以 sandbox_id 去重，避免并发归档/DELETE 冲突（DELETE 本身幂等，但归档须串行）。

### R3 callback sink 配置策略

- **R3.1** 优先「按沙箱自带 `callback_url`」：建沙箱时在 `POST /sandboxes` body 传 `callback_url`（[`api.py:64`](sandbox_service/api.py#L64)），指向应用层自己的 webhook 接收端点。使沙箱服务不持有全局应用地址，符合契约 §2.6。
- **R3.2** 部署级 `CALLBACK_URL` 作为兜底：当历史代码路径建沙箱未传 `callback_url` 时，由部署级配置兜底。**当前部署 `CALLBACK_URL` 为空（[`.env.example:46`](deploy/.env.example#L46)），须补充配置**。
- **R3.3** sink 地址须高可用：若应用层多实例，sink 须经过负载均衡或共享队列，避免单实例宕机导致 webhook 全丢。

### R4 dead / exited 事件处理

- **R4.1** 收到 `dead`（`reason=health_probe_failed`）或 `exited`（`reason=oom|exit`）时，容器已非运行态。应用层须：
  1. 清理本地对该 sandbox_id 的账本与引用（标记为已终止，停止向其代理转发）。
  2. 若该沙箱仍有未归档数据且业务需要，触发一次归档尝试（容器已死，归档可能失败，记日志）。
  3. 调用 `DELETE /sandboxes/{id}` 摘租约 + 扫尾（确保服务侧账本与 Docker 一致）。
- **R4.2** 不得对 `dead`/`exited` 的沙箱再发业务流量（代理会返回 502 `sandbox_unreachable`）。

### R5 防泄漏可观测

- **R5.1** 指标：暴露 `sandbox_delete_total`（含成功/失败/重试次数）、`sandbox_delete_latency_seconds`、`evict_candidate_received_total`、`sandbox_leak_gauge`（已退出但容器仍存的沙箱数）。
- **R5.2** 泄漏检测：定时（建议 ≤ 60s）对账——遍历应用层「已退出工作空间」列表，对每个调用 `GET /sandboxes/{id}` 或 `GET /capacity`，若容器仍 `running` 且超过 R1.5 SLA，置 `sandbox_leak_gauge` 并告警。
- **R5.3** 告警：`sandbox_leak_gauge > 0` 持续 ≥ 5min 触发告警；DELETE 连续失败 ≥ 阈值触发告警。

---

## 5. 开发任务分解

> 每个任务标注：依赖、产出、验收点。T0 为前置，T1–T5 可部分并行，T6 为端到端验收。

### T0 前置：映射与配置基线
- **依赖**：无
- **内容**：
  1. 确认应用层已持久化「工作空间/会话 → sandbox_id」映射（若未持久化，补建；进程重启后须可恢复，否则 L1 重启即丢）。
  2. 与运维确认 `SANDBOX_SERVICE_HOST`、`SERVICE_TOKEN`、`CALLBACK_URL`（或应用层 webhook 端点地址）。
  3. 确认 `IDLE_TTL_SECONDS`（默认 600）作为 L2 时序设计的输入。
- **验收**：映射可在应用层重启后恢复；配置项就绪。

### T1 实现 L1：退出事件 → DELETE
- **依赖**：T0
- **内容**：在「退出工作空间」业务处理流程中，异步调用 `DELETE /sandboxes/{id}`；实现幂等重试（R1.2/R1.3）与失败补偿队列（R1.4）。
- **产出**：退出处理模块 + DELETE 客户端（带重试/退避）+ 补偿队列。
- **验收**：见 §7 验收用例 AC1–AC3。

### T2 实现 L2 sink：webhook 接收端点
- **依赖**：T0
- **内容**：实现 `callback_url` 接收端点，校验 Bearer，分发 `evict_candidate`/`dead`/`exited`（R2.2、R4）。
- **产出**：webhook 接收路由 + 事件分发器。
- **验收**：见 AC4。

### T3 实现 L2 消费：归档 + DELETE 编排
- **依赖**：T2
- **内容**：`evict_candidate` 处理器按 R2.4 编排「代理归档 → DELETE」，含去重（R2.5）、归档失败保护（R2.4）。
- **产出**：归档-销毁编排器（串行锁 per sandbox_id）。
- **验收**：见 AC5、AC6。

### T4 实现 L2 轮询兜底
- **依赖**：T0
- **内容**：定时轮询 `GET /capacity`，对 `evict_candidates` 执行 T3 同一套编排（R2.3），与 webhook 通道共享去重表。
- **产出**：容量轮询定时器 + 共享去重状态。
- **验收**：见 AC7。

### T5 实现 R3 sink 配置 + 建沙箱传 callback_url
- **依赖**：T0
- **内容**：建沙箱调用处补传 `callback_url`（R3.1）；协调运维补 `CALLBACK_URL`（R3.2）；多实例下 sink 经 LB/队列（R3.3）。
- **产出**：建沙箱调用改造 + 部署配置变更。
- **验收**：见 AC8。

### T6 实现 R5 可观测
- **依赖**：T1–T4
- **内容**：指标埋点、泄漏对账定时器、告警规则（R5.1–R5.3）。
- **产出**：指标导出 + 对账任务 + 告警配置。
- **验收**：见 AC9、AC10。

---

## 6. 数据契约详表

### 6.1 DELETE 请求

```
DELETE /sandboxes/{sid}?grace_seconds=5
Authorization: Bearer <SERVICE_TOKEN>
```

成功响应（200）：
```json
{"ok": true, "terminated": true}
```
- `terminated=false`：本次幂等重复删（无租约且无残留容器），仍为 200。

失败响应：
- 500 `terminate_failed`：扫尾失败，容器可能仍在，**必须重试**。

### 6.2 webhook payload（沙箱服务 → 应用层）

见 §3.3。应用层接收端点须：
- 校验 `Authorization: Bearer <SERVICE_TOKEN>`。
- 返回 2xx 表示已收到（沙箱服务对 <500 视为成功，[`watcher.py:66`](sandbox_service/watcher.py#L66)）。
- 处理异步化：接收后立即回 2xx，编排（归档+DELETE）异步进行，避免阻塞投递。

### 6.3 建沙箱传 callback_url

```jsonc
POST /sandboxes
Authorization: Bearer <SERVICE_TOKEN>
{
  "id": "s-123",
  "env": { "SESSION_ID": "s-123", "AGENT_TOKEN": "…" },
  "callback_url": "https://app.example.com/hooks/sandbox"
}
```

### 6.4 归档（经代理）

```
POST /sandboxes/{id}/proxy/8080/agent/archive
Authorization: Bearer <SERVICE_TOKEN>
```
透传到容器内 agent（[`api.py:220-238`](sandbox_service/api.py#L220-L238)）。归档协议详见 [`docs/agent-contract.md`](docs/agent-contract.md) §2.7。

---

## 7. 验收标准

> 所有用例在沙箱服务 `IDLE_TTL_SECONDS=600`、`CALLBACK_URL` 已配或已传 `callback_url` 的环境下执行。

- **AC1（L1 基本通路）**：用户退出工作空间 → 5min 内 `GET /sandboxes/{id}` 返回 404 或 `running=false`。
- **AC2（L1 幂等）**：对同一沙箱连续两次退出/重复 DELETE，第二次返回 `terminated=false`，无副作用。
- **AC3（L1 失败重试）**：模拟 DELETE 返回 500，应用层按退避重试 ≥ 3 次；最终成功后容器消失；最终失败产生告警且任务进入补偿队列。
- **AC4（L2 webhook 通路）**：沙箱空闲 > `IDLE_TTL_SECONDS` 后，应用层 webhook 端点收到 `evict_candidate`（`reason=idle_ttl`）。
- **AC5（L2 先归档后删）**：收到 evict_candidate 后，先发 `/proxy/8080/agent/archive`，成功后再 DELETE；归档失败时不 DELETE，转入重试/人工。
- **AC6（L2 去重）**：webhook 与轮询同触发同一 sandbox_id，归档仅执行一次，DELETE 幂等。
- **AC7（L2 轮询兜底）**：在 webhook 全丢（sink 不可达）场景下，轮询 `/capacity` 仍能在 ≤ `IDLE_TTL` + 300s 内发现并回收 evict_candidate。
- **AC8（R3 sink 生效）**：建沙箱传 `callback_url` 后，`GET /sandboxes/{id}` 或服务日志确认该沙箱事件投递到应用层端点；`CALLBACK_URL` 为空时单沙箱 callback_url 仍生效。
- **AC9（泄漏检测）**：人为制造 L1 漏删（跳过 DELETE），`sandbox_leak_gauge` 在 SLA 超时后置位并告警；L2 随后回收使指标归零。
- **AC10（dead/exited）**：手动 `docker stop` 沙箱容器模拟退出，应用层收到 `exited` 事件后清理本地账本并对该 sandbox_id 不再转发业务流量。
- **AC11（无残留）**：连续退出 50 个工作空间后，`GET /capacity` 的 `live` 与应用层活跃会话数一致，无「已退出但仍 live」的沙箱。

---

## 8. 风险与边界

- **风险 1：退出后残留流量刷新 `last_active`**。若前端 SSE 未断、健康轮询未停，`touch()`（[`api.py:237`](sandbox_service/api.py#L237)）使 TTL 永不触发，L2 不标记候选。**对策**：L1 为主，L2 为辅；L1 不依赖 TTL。同时在应用层退出时主动断开对该沙箱的 SSE / 轮询连接。
- **风险 2：归档与 DELETE 的数据安全**。归档失败贸然 DELETE 会丢数据。**对策**：R2.4 强制归档先行且失败不删；归档超时转人工。
- **风险 3：webhook 丢失**。沙箱服务尽力而为，重试 3 次后放弃。**对策**：R2.3 轮询兜底；不得仅依赖 webhook。
- **风险 4：应用层重启丢映射**。若「工作空间→sandbox_id」映射仅在内存，重启后 L1 无法定位。**对策**：T0 要求持久化。
- **边界 1**：本计划不修改沙箱服务。若 L1/L2 均未接，沙箱将长期存活直至 L3——但 L3 不回收账本内沙箱，故仍会泄漏。L1/L2 是必需项，非可选。
- **边界 2**：`IDLE_TTL_SECONDS` 调小可缩短 L2 触发时延，但会增加误逐出（短暂空闲被标候选）。本计划不要求调整，以部署现值 600s 为准。

---

## 9. 附录：排查与验证命令

> 在沙箱服务所在机器执行，`$SERVICE_TOKEN` 为北向 token，`$BASE` 为服务基址。

```bash
# A1. 查池统计与逐出候选
curl -s -H "Authorization: Bearer $SERVICE_TOKEN" $BASE/capacity | jq .

# A2. 查指定沙箱状态（确认是否仍 running）
curl -s -H "Authorization: Bearer $SERVICE_TOKEN" $BASE/sandboxes/<sandbox_id> | jq .

# A3. Docker 侧确认容器存活与 label
docker ps --filter "label=sandbox-service.sandbox_id=<sandbox_id>"

# A4. 主动触发回收（验收用）
curl -s -X DELETE -H "Authorization: Bearer $SERVICE_TOKEN" \
  "$BASE/sandboxes/<sandbox_id>?grace_seconds=5" | jq .

# A5. 查沙箱服务日志（webhook 投递、reaper 标记、孤儿巡检）
# 日志 logger: sandbox_service.pool / sandbox_service.watcher / sandbox_service.api
```

判读：
- `/capacity` 中 `evict_candidates` 含某 id → L2 已标记，断点在应用侧消费（webhook sink 空 / 未轮询）。
- 不含某 id 且容器仍 running → `last_active` 仍被刷新（见风险 1），或 `IDLE_TTL` 未到；L1 未触发是根因。

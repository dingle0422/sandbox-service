# sandbox_service — 通用沙箱服务

业务中立的容器沙箱管理服务（旧 vm2 `backend/sandbox_manager` 的继任者，greenfield 开发）。

- **契约**：北向 API 见 `docs/protocol/sandbox-lifecycle.md`；容器内 agent 协议见
  `docs/protocol/agent-contract.md`（本服务对其零感知，经通用代理透传）。
- **边界**：只认识镜像/容器/端口/资源/env（不透明 map）/工作区/不透明快照 key；
  零业务语义、零 Postgres、ObjectStore 可插拔（默认 MinIO/S3 兼容）。
- **被动**：唯一的出站是可选的通用事件 webhook（`CALLBACK_URL`，或调用方在
  `POST /sandboxes` 时按沙箱自带 `callback_url`）。服务**不持有任何应用侧业务地址**、
  不主动驱动应用后端。
- **兼容**：内置旧 `/containers/*`、`/workspaces/*`、`/blobs/*` shim（`shim.py`）做
  请求/响应 API 面翻译，vm1 改个 URL 即可切换。推送型行为（草稿定时/终态归档、
  退出入账）归 vm1（p2-vm1）：vm1 自行定时或消费终态事件后调 `/containers/{cid}/archive`，
  退出/逐出经 webhook 消费。

## 运行

```bash
pip install -r sandbox_service/requirements.txt
SERVICE_TOKEN=xxx SANDBOX_WORKSPACE_ROOT=/var/sandbox/workspaces \
  uvicorn sandbox_service.main:app --host 0.0.0.0 --port 8001
```

容器化部署见 `deploy/sandbox_service/`。

## 模块

| 模块 | 职责 |
| --- | --- |
| `config.py` | 全 env 配置面（见 sandbox-lifecycle.md §3） |
| `backend.py` | ContainerSpec/Backend 协议 + DockerBackend |
| `pool.py` | 容量 N + 空闲 TTL + LRU 账本（keep-warm 池） |
| `proxy.py` | 通用端点代理（含 SSE 流式透传） |
| `workspace.py` | 骨架/导入/快照恢复/文件 CRUD |
| `objectstore.py` | ObjectStore 协议 + MinIO 默认实现（`OBJECT_STORE=module:factory` 可换） |
| `watcher.py` | 退出/死亡/TTL 逐出候选 → webhook（`CALLBACK_URL`） |
| `api.py` | 中立北向 API（/sandboxes…） |
| `shim.py` | 旧 vm2 路由**被动**兼容层（请求→翻译→转发→响应；不主动回调应用后端。`ENABLE_LEGACY_SHIM=0` 可关） |

## 测试

```bash
python -m pytest sandbox_service/tests -q
```

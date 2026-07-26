# echo-agent

`agent-contract` v1.0 的**最小零业务参考实现**。把用户输入原样 echo 回来，走完整
事件流并收尾。不依赖 LLM / MinIO / 数据库 —— 用来：

1. **冒烟/集成载体**：验证 `sandbox_service` 的通用路径（create/就绪探测/通用代理/SSE/
   workspace/lifecycle），不被业务依赖污染；
2. **解耦证明**：任何遵循 `docs/protocol/agent-contract.md` 的镜像都能接入沙箱，无需
   改平台代码；
3. **conformance 基准**：p3 黑盒合规套件的参照 agent。

## 端点（严格对齐 agent-contract v1.0）

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/agent/health` | `{ok, busy, run_id, contractVersion:"1.0"}` |
| POST | `/agent/input` | 202；活跃期重复 → 409 `run_busy` |
| POST | `/agent/resume` | 同 input |
| POST | `/agent/cancel` | 幂等；命中 → 事件流以 `RUN_CANCELLED` 收尾 |
| GET | `/agent/events` | SSE：`RUN_STARTED … RUN_FINISHED → __finalize__` |
| POST | `/agent/materialize` | 幂等；echo 不播种，`mode="skipped"` |
| POST | `/agent/archive` | stub：返回稳定 `payload_key`、`changed=false`（不接对象存储） |

## 本地跑（不依赖 Docker）

```bash
python -m pytest echo_agent/tests -q          # 进程内合约测试
uvicorn echo_agent.app:app --port 8080        # 手动起服务
```

## 构建镜像

```bash
docker build -t echo-agent:latest -f echo_agent/Dockerfile .
```

镜像自带 `CMD`（自启 uvicorn），对应 `sandbox_service` 默认 `AGENT_COMMAND` 为空即
「用镜像 CMD」。

## 端到端冒烟

见 `deploy/sandbox_service/docker-compose.smoke.yml` 与 `deploy/sandbox_service/smoke.sh`。

## 做你自己的 agent

以本目录为模板接入自有 agent，见 `docs/protocol/agent-onboarding.md`（从零实现 → 黑盒验证 → 只改 .env 接入）。

# sandbox-service

业务中立的通用 agent 沙箱服务。给 agent 一个隔离的容器 + 一块工作区，把流量原样代理进去，
其余一概不管——**对 agent 的协议内容零感知**，因此任何遵守契约的 agent 都能直接跑。

## 它做什么 / 不做什么

做：容器生命周期（建/查/销毁/空闲逐出/孤儿回收）、通用端点代理（含 SSE 流式）、
工作区快照与恢复（对象存储可插拔）、事件被动通知。

不做：不认识任何业务概念，不解析 agent 的请求体，不主动调用应用后端。业务编排（什么时候
归档、逐出后做什么）属于调用方，服务只在事件发生时按配置的 webhook 或每沙箱 `callback_url`
被动通知。这是它能被任意应用复用的前提。

## 组成

| 目录 | 说明 |
| --- | --- |
| `sandbox_service/` | 服务本体。北向 `/sandboxes` 生命周期 API + 通用代理 + 工作区快照 |
| `sdk/std_agent_sdk/` | agent 契约边界 SDK：契约模型 + `AgentSession` 会话替身，零外部依赖。**独立分发**（`std-agent-sdk`），消费方 pip 装它进自己的 agent 镜像 |
| `echo_agent/` | 零业务的最小合规 agent，参考实现兼冒烟靶子，接入时可直接照抄 |
| `tests/agent_conformance/` | agent 侧契约黑盒套件——第三方 agent 的自证工具 |
| `tests/lifecycle_conformance/` | 沙箱侧契约黑盒套件——换底座（如 OpenSandbox）时的验收锚点 |
| `docs/` | 两份契约、JSON Schema、样例 fixture、接入文档 |
| `deploy/` | compose、冒烟编排与 smoke 脚本 |

## 快速开始

```bash
pip install -e ".[dev]" -e sdk            # 服务本体 + SDK（两个独立分发）
pytest                                    # 单测 + 两套 conformance（自托管 echo，无需 Docker）
python scripts/validate_contracts.py      # 契约 schema 与 fixture 校验
```

真 Docker 端到端冒烟（用自带的 echo-agent，不需要任何业务镜像）：

```bash
docker compose -f deploy/docker-compose.smoke.yml up -d --build
bash deploy/smoke.sh
docker compose -f deploy/docker-compose.smoke.yml down
```

生产部署：

```bash
cp deploy/.env.example deploy/.env        # 填 SERVICE_TOKEN / AGENT_IMAGE / MinIO / 工作区路径
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
```

`AGENT_IMAGE` 指向的 agent 镜像由各自的应用仓库构建，本服务不参与构建。

## 接入一个新 agent

新手入门先看 [`docs/integration/beginner-integration-guide.md`](docs/integration/beginner-integration-guide.md)(线性旅程)或 [按角色版](docs/integration/integration-guide-by-role.md)。
实现契约要求的七个端点，通过 `tests/agent_conformance`，然后把 `AGENT_IMAGE` 指向你的镜像
——沙箱服务与调用方都不用改代码。完整步骤见 [docs/agent-onboarding.md](docs/agent-onboarding.md)，
协议细节见 [docs/agent-contract.md](docs/agent-contract.md) 与
[docs/sandbox-lifecycle.md](docs/sandbox-lifecycle.md)。

## 换掉沙箱底座

调用方对沙箱的依赖收敛在 `sandbox-lifecycle` 这一份契约上。想换成 OpenSandbox 之类的现成
平台，只要在其之上补一层满足该契约的薄适配层，用 `SANDBOX_SERVICE_URL` 指向它跑通
`tests/lifecycle_conformance` 即可——agent 镜像与调用方零改动。端点映射见
`docs/sandbox-lifecycle.md`。

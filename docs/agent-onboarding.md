# Agent 接入指南（Agent Onboarding）

> 状态：Informative · 读者：想把**自己的 agent** 接入本沙箱系统的开发者
> 新手入门:先看 [`integration/beginner-integration-guide.md`](integration/beginner-integration-guide.md),再回本文看协议细节。
> 权威契约：[`agent-contract.md`](agent-contract.md)（Normative）· 生命周期：[`sandbox-lifecycle.md`](sandbox-lifecycle.md)
> 参考实现：仓库根 [`echo_agent/`](../../echo_agent/)（零业务最小合规 agent）

本系统里 **agent 与沙箱/会话完全解耦**：任何实现 `agent-contract v1.0` 的容器镜像都能接入，**无需改动平台代码**——生产上线只是把 `AGENT_IMAGE` 指向你的镜像。本文给出「从零做一个合规 agent → 验证 → 只改 .env 接入」的最短路径。

---

## 1. 你要交付什么

一个**自带入口（CMD/ENTRYPOINT）**的容器镜像，容器内起一个 HTTP 服务，监听 `0.0.0.0:${AGENT_PORT}`（默认 8080），实现下表 7 个端点（详见 `agent-contract.md` §2）：

| 方法 | 路径 | 必须行为 |
| --- | --- | --- |
| GET | `/agent/health` | 返回 `{ok, busy, run_id, contractVersion:"1.0"}` |
| POST | `/agent/input` | 收 `RunRequest`，**立即 202**，后台执行；活跃期重复 → 409 `run_busy` |
| POST | `/agent/resume` | 同 input（续跑，宿主已把审批编进 `resume_item`） |
| POST | `/agent/cancel` | 幂等；命中置取消信号，事件流以 `RUN_CANCELLED` 收尾 |
| GET | `/agent/events` | SSE：`RUN_STARTED … 终止事件 → __finalize__ → 关流` |
| POST | `/agent/materialize` | 工作区物化，**幂等**（二次 `mode="skipped"`） |
| POST | `/agent/archive` | 工作区归档到对象存储，返回 `payload_key` + 内容级 `changed` |

**核心 vs 扩展**：核心字段任何 agent 都要理解；`enterprise_id`/`period`/`template`/`viewing`/`references` 等是本仓税务领域的 `[tax]` 扩展——你的 agent **可以忽略未知扩展字段，但不得因其报错**（`agent-contract.md` §0.2）。

---

## 2. 起步：抄 echo_agent

`echo_agent/app.py`（~150 行）是 v1.0 的**可运行最小骨架**：全 7 端点、正确的 SSE 帧序与 `__finalize__` 收尾、单容器单活跃 run、materialize 幂等。建议直接以它为模板，把「echo 一句话」换成你的编排逻辑。

```bash
python -m pytest echo_agent/tests -q       # 进程内合约测试（不需 Docker）
uvicorn echo_agent.app:app --port 8080     # 手动起来点一点
```

你的 agent 通常只需替换三处：
- `/agent/input` 后台 worker：把 echo 换成真正的 run（调 LLM、跑工具、发 AG-UI 事件）；
- `/agent/materialize`：新会话按需播种、旧会话按 `payload_key` 从对象存储恢复（echo 直接 `skipped`）；
- `/agent/archive`：把 `/workspace` 打包上传对象存储并返回 `payload_key`（echo 是 stub）。

事件结构（`RUN_STARTED`/`TEXT_MESSAGE_*`/`TOOL_CALL_*`/`RUN_FINISHED`…）见 `agent-protocol.md`；`__finalize__` 信封见 `agent-contract.md` §2.5。

---

## 3. 运行环境（沙箱注入，agent 只读）

容器启动时由沙箱服务注入 env（`agent-contract.md` §4）。核心的几类：

- 身份：`SESSION_ID` / `OWNER_ID` / `PROJECT_ID` / `PAYLOAD_KEY`；
- 路径：`WORKSPACE=/workspace`、`DEBUG_DIR=/tmp/debug`；
- LLM（经宿主 proxy，**永不给真实 key**）：`LLM_BASE_URL`、`LLM_API_KEY`(= `AGENT_TOKEN`)、`LLM_MODEL`/`LLM_DIALECT`/`LLM_TEMPERATURE`；
- 对象存储（数据面直连）：`MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_SECURE`/`MINIO_DEFAULT_BUCKET`/`MINIO_REGION`。

**文件系统契约**：`/workspace` 是唯一持久面（宿主 bind mount）；其余易失。骨架目录与归档范围见 `agent-contract.md` §5。你的 agent **不自行持久化对话历史**（宿主经 `history` 装载、`__finalize__` 回收）。

> 若你的 agent 不需要 LLM/MinIO/税务扩展（如纯工具型），忽略对应 env 即可——沙箱不强制。

---

## 4. 构建镜像（关键：自带入口）

镜像必须**自启服务**，沙箱默认不注入启动命令（`AGENT_COMMAND` 留空 = 用镜像 CMD）。参照 `echo_agent/Dockerfile`：

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY your_agent/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
COPY your_agent /srv/your_agent
ENV PYTHONPATH=/srv AGENT_PORT=8080 WORKSPACE=/workspace
EXPOSE 8080
CMD ["sh", "-c", "uvicorn your_agent.app:app --host 0.0.0.0 --port ${AGENT_PORT:-8080}"]
```

```bash
docker build -t your-agent:latest -f your_agent/Dockerfile .
```

---

## 5. 验证合规（黑盒，换靶子即可）

平台提供两套黑盒 conformance，**直接打你的 agent**，无需信任你的内部实现：

```bash
# ① agent 契约：起你的容器后，指向它
docker run -d --name my-agent -p 8080:8080 your-agent:latest
AGENT_BASE_URL=http://localhost:8080 python -m pytest tests/agent_conformance -q

# ② 端到端冒烟：换 AGENT_IMAGE 走沙箱通用路径（create/代理/SSE/workspace/lifecycle）
#    仿 deploy/docker-compose.smoke.yml，把 echo-agent 换成 your-agent:latest
docker compose -f deploy/docker-compose.smoke.yml up -d --build
bash deploy/smoke.sh
```

合规最小标准见 `agent-contract.md` §6（health/202/409、SSE 收尾、cancel 幂等、materialize 幂等、archive 可恢复 + 查重、容忍未知字段）。全绿即「合规 agent」。

---

## 6. 接入生产（只改 .env）

平台代码零改动，切换只动部署变量：

**新路径（推荐，业务中立 `sandbox_service`）** —— 改 `deploy/.env`：

```dotenv
AGENT_IMAGE=your-agent:latest
AGENT_COMMAND=          # 留空 = 用镜像 CMD
AGENT_PORT=8080
# 非税务 agent 可清空骨架目录：
WORKSPACE_SKELETON_DIRS=
```

重启 `sandbox_service` 即生效；vm1 侧无需改动（会话/归档/webhook 全走通用协议）。

> 版本协商：`/agent/health` 的 `contractVersion` major 必须 = 1，否则宿主判 `agent_contract_mismatch` 拒绝接入（`agent-contract.md` §0.1）。

---

## 7. 接入检查清单

- [ ] 镜像自带 CMD，起容器 90s 内 `/agent/health` `ok=true` 且 `contractVersion` major=1
- [ ] `/agent/input` 202；活跃期重复 409 `run_busy`
- [ ] `/agent/events` 帧序合法，终止事件 + `__finalize__` 收尾、正常关流
- [ ] `/agent/cancel` 幂等，取消后 `RUN_CANCELLED`
- [ ] `/agent/materialize` 幂等（二次 `skipped`）
- [ ] `/agent/archive` 返回 `payload_key`，以该 key 重物化可恢复等价工作区；无变化 `changed=false`
- [ ] 忽略未知扩展字段不报错
- [ ] `tests/agent_conformance`（打你的镜像）全绿 + `smoke.sh` 全绿
- [ ] 只改 `AGENT_IMAGE` 即接入，平台代码零改动

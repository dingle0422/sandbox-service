# std_agent_sdk

容器内 agent 的**标准契约边界 SDK**（P3 解耦）。把「agent 与宿主之间的 HTTP/SSE 契约」
从纯业务 runtime 里剥出来，做成零 `app.*` 依赖、可独立进镜像的包——任意合规 agent
（税务 agent / `echo_agent` / 第三方）都可依赖。

## 内容

- **`contract`**：agent-contract v1.0 的权威 pydantic 模型
  - 请求：`RunRequest` / `MaterializeReq` / `CancelReq` / `ArchiveReq`
  - 响应：`HealthResponse` / `MaterializeResult` / `ArchiveResult`
  - `FINALIZE_TYPE`（SSE 收尾信封类型）
  - 全部 `extra="allow"`，容忍未知扩展字段（契约 §0.2）
- **`session.AgentSession`**：容器侧会话替身，取代对 vm1 `app.sessions.manager.Session`
  （顶层拖 PG 索引/持久化/知识库播种）的误用；字段是 vm1 `Session` 的忠实子集。

## 与现役代码的关系

- `sandbox_manager/app/agent_service.py` 已用 `AgentSession` 构造容器内会话。
- `contract` 模型与 `agent_service` 内联模型由 `tests/test_contract_parity.py` 奇偶校验锁定
  （字段集、关键缺省值、`AgentSession ⊆ Session`）。
- 待 Docker：把 `contract` 模型也切到本 SDK + 裁 `Dockerfile.agent` 纳入本包。

## 安装

消费方仓库（把本包装进自己的 agent 镜像）钉 tag 装：

```bash
pip install "std-agent-sdk @ git+https://github.com/dingle0422/sandbox-service.git@sdk-v1.0.0#subdirectory=sdk"
```

本仓开发：

```bash
pip install -e sdk        # 从仓库根执行；包名 std-agent-sdk
```

## 测试

```bash
python -m pytest sdk/std_agent_sdk/tests -q
```

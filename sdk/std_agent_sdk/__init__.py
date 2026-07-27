"""std_agent_sdk：容器内 agent 的契约边界 SDK（P3 解耦，plan §P3）。

职责（业务无关的「契约面」，与纯业务 runtime 分离）：
- ``contract``：agent-contract v1.0 的权威请求/响应 pydantic 模型（RunRequest / Materialize /
  Archive / Cancel + Health/Materialize/Archive 结果 + SSE 收尾信封）。
- ``session``：容器侧 :class:`AgentSession` 替身——只含 runtime 真正读写的字段，取代对
  vm1 ``app.sessions.manager.Session``（顶层拖 PG 索引/持久化/知识库播种）的误用。

本包**零 import ``app.*`` / ``sandbox_manager.*`` / ``session_manager.*``**，可独立打进 agent 镜像。
现役 ``sandbox_manager/app/agent_service.py`` 的等价模型受 tests 奇偶校验锁定，待 Docker 起后低风险切换。
"""

__version__ = "1.0.0"

#: agent-contract 版本（docs/protocol/agent-contract.md）；health 响应回带，宿主按 major 判兼容。
CONTRACT_VERSION = "1.0"

__all__ = ["CONTRACT_VERSION", "__version__"]

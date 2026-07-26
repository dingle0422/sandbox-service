"""通用沙箱服务（sandbox_service）。

业务中立的容器沙箱管理：镜像/容器/端口/资源/env（不透明）/工作区/不透明快照 key。
北向契约见 docs/protocol/sandbox-lifecycle.md；对容器内协议零感知（经通用代理透传）。

独立于 backend/（vm1 业务层与旧 vm2）：零业务语义、零 Postgres、ObjectStore 可插拔。
"""

__version__ = "1.0.0"

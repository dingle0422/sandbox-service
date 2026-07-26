"""FastAPI 入口：`create_app()` 工厂 + uvicorn 模块级 `app`。

启动：``uvicorn sandbox_service.main:app --host 0.0.0.0 --port 8001``
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI

from sandbox_service.api import build_router, health_payload
from sandbox_service.auth import make_token_guard
from sandbox_service.service import ServiceState, build_state
from sandbox_service.shim import build_shim_router

logger = logging.getLogger("sandbox_service.main")


def _configure_logging() -> None:
    """给 ``sandbox_service.*`` 挂一个 stderr handler。

    uvicorn 只配置 ``uvicorn.*`` 系列 logger，root 没有 handler，本服务的
    ``logger.exception`` 全被丢弃——容器 stop 失败这类关键错误因此完全不可见，
    排查时只能看到一条「DELETE ... 200 OK」。
    """
    root = logging.getLogger("sandbox_service")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    root.propagate = False


def create_app(state: Optional[ServiceState] = None) -> FastAPI:
    _configure_logging()
    st = state or build_state()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            # 重启后按 label 兜底回收孤儿容器；年龄保护同样适用——重启瞬间可能
            # 正有别的实例在建容器，刚出生的不碰，留给后续周期巡检收尾。
            st.pool.reap_orphan_containers(min_age_seconds=st.settings.orphan_min_age_seconds)
        except Exception:
            logger.exception("启动孤儿容器回收失败")
        st.watcher.start()
        yield
        st.shutdown()

    app = FastAPI(title="sandbox_service", lifespan=lifespan)
    app.state.service = st

    guard = Depends(make_token_guard(st.settings.service_token))

    @app.get("/health")
    def health() -> dict:
        return health_payload(st)

    app.include_router(build_router(st), dependencies=[guard])
    # 本仓税务兼容层：纯被动路由翻译，不主动回调应用后端（见 shim.py 模块说明）。
    if st.settings.enable_legacy_shim:
        app.include_router(build_shim_router(st), dependencies=[guard])
    return app


#: uvicorn 入口（惰性构造 + 缓存；单测请直接用 create_app(state)，避免读生产 env）
_app_singleton: Optional[FastAPI] = None


def __getattr__(name: str):
    if name == "app":
        global _app_singleton
        if _app_singleton is None:
            _app_singleton = create_app()
        return _app_singleton
    raise AttributeError(name)


__all__ = ["create_app", "app"]

"""北向 Bearer 鉴权：单一共享服务 token（等值校验；签发轮换归调用方运维）。"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException


def make_token_guard(expected: str):
    """返回 FastAPI 依赖：校验 ``Authorization: Bearer <token>``。空 token = 不鉴权（本地/单测）。"""

    def _guard(authorization: Optional[str] = Header(default=None)) -> None:
        token = (expected or "").strip()
        if not token:
            return
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing_service_token")
        got = authorization.split(" ", 1)[1].strip()
        if got != token:
            raise HTTPException(status_code=401, detail="invalid_service_token")

    return _guard


__all__ = ["make_token_guard"]

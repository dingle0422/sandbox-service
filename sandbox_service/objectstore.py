"""ObjectStore：可插拔对象存储协议 + 默认 MinIO/S3 兼容实现。

本服务只对快照 key 做**不透明存取**（下载 tar.gz），不解读 key 语义、不产生快照
（快照由容器内数据面直连对象存储生成，见 agent-contract.md §2.7）。

默认实现读 ``MINIO_*`` env，与现网 MinIO 部署完全兼容；接其它 S3 兼容存储
（AWS S3 / OSS / COS…）只需换 endpoint/凭据。自定义后端实现 :class:`ObjectStore` 即可。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("sandbox_service.objectstore")


class ObjectStoreError(RuntimeError):
    pass


class ObjectNotFoundError(ObjectStoreError):
    pass


@runtime_checkable
class ObjectStore(Protocol):
    def get_bytes(self, key: str) -> bytes:
        """下载对象；不存在抛 :class:`ObjectNotFoundError`。"""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None:
        """删除对象（幂等；仅 legacy shim 的 /blobs 删除用）。"""
        ...


class MinioObjectStore:
    """默认实现：MinIO/S3 兼容（minio SDK，惰性连接）。

    env：``MINIO_ENDPOINT`` / ``MINIO_ACCESS_KEY`` / ``MINIO_SECRET_KEY`` /
    ``MINIO_SECURE`` / ``MINIO_DEFAULT_BUCKET`` / ``MINIO_REGION``。
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        secure: Optional[bool] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint or os.getenv("MINIO_ENDPOINT", "")
        self._access_key = access_key or os.getenv("MINIO_ACCESS_KEY", "")
        self._secret_key = secret_key or os.getenv("MINIO_SECRET_KEY", "")
        self._secure = (
            secure
            if secure is not None
            else (os.getenv("MINIO_SECURE", "false").strip().lower() in ("1", "true", "yes"))
        )
        self._bucket = bucket or os.getenv("MINIO_DEFAULT_BUCKET", "analysis-platform")
        self._region = region or os.getenv("MINIO_REGION") or None
        self._client: Any = None

    def _cli(self) -> Any:
        if self._client is None:
            from minio import Minio

            if not self._endpoint:
                raise ObjectStoreError("MINIO_ENDPOINT 未配置")
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key or None,
                secret_key=self._secret_key or None,
                secure=self._secure,
                region=self._region,
            )
        return self._client

    def get_bytes(self, key: str) -> bytes:
        from minio.error import S3Error

        resp = None
        try:
            resp = self._cli().get_object(self._bucket, key)
            return resp.read()
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise ObjectNotFoundError(key) from exc
            raise ObjectStoreError(str(exc)) from exc
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._cli().stat_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise ObjectStoreError(str(exc)) from exc

    def delete(self, key: str) -> None:
        from minio.error import S3Error

        try:
            self._cli().remove_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return
            raise ObjectStoreError(str(exc)) from exc


def load_object_store() -> ObjectStore:
    """按 ``OBJECT_STORE`` env 装配（默认 minio）；自定义后端填 ``module:factory``。"""
    kind = (os.getenv("OBJECT_STORE") or "minio").strip()
    if kind == "minio":
        return MinioObjectStore()
    if ":" in kind:
        import importlib

        mod_name, factory_name = kind.split(":", 1)
        factory = getattr(importlib.import_module(mod_name), factory_name)
        store = factory()
        if not isinstance(store, ObjectStore):
            raise ObjectStoreError(f"{kind} 未实现 ObjectStore 协议")
        return store
    raise ObjectStoreError(f"未知 OBJECT_STORE: {kind}")


__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "ObjectNotFoundError",
    "MinioObjectStore",
    "load_object_store",
]

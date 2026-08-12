"""本地 agent 镜像 last-used 账本 + 僵尸镜像 GC。

只管理本服务登记过的镜像 ref（pull/create）；超 ``IMAGE_IDLE_TTL_SECONDS`` 未再用来起
容器且非保护/在用则 ``rmi`` 本地 tag。registry 仍是真相源，误删后靠 ensure_image 再拉。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger("sandbox_service.image_gc")


def split_image_ref(image: str) -> tuple[str, str]:
    """``host:port/repo:tag`` → ``(repo, tag)``；无合法 tag 时 tag=latest。"""
    repo, _, tag = image.rpartition(":")
    if not repo or "/" in tag:
        return image, "latest"
    return repo, tag


def image_repo(image: str) -> str:
    return split_image_ref(image)[0]


class ImageBackend(Protocol):
    def remove_image(self, image: str) -> bool: ...
    def list_repo_tags(self, repo: str) -> list[tuple[str, float]]: ...
    def list_in_use_images(self) -> frozenset[str]: ...
    def image_idents(self, image: str) -> frozenset[str]: ...


class ImageUsageStore:
    """``{path}`` JSON：``{"images": {"repo:tag": {"last_used": <unix>}}}``。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._images: dict[str, float] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            images = raw.get("images") if isinstance(raw, dict) else None
            if not isinstance(images, dict):
                return
            for ref, meta in images.items():
                if not isinstance(ref, str) or not ref:
                    continue
                if isinstance(meta, dict) and "last_used" in meta:
                    self._images[ref] = float(meta["last_used"])
                elif isinstance(meta, (int, float)):
                    self._images[ref] = float(meta)
        except Exception:
            logger.exception("读取镜像用量账本失败 path=%s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"images": {k: {"last_used": v} for k, v in sorted(self._images.items())}}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    def register(self, image: str, *, now: Optional[float] = None) -> None:
        """首次见到镜像：写入 last_used=now；已存在不改。"""
        ref = (image or "").strip()
        if not ref:
            return
        ts = float(now if now is not None else time.time())
        with self._lock:
            if ref in self._images:
                return
            self._images[ref] = ts
            self._save()

    def touch(self, image: str, *, now: Optional[float] = None) -> None:
        """容器 create 成功：刷新 last_used。"""
        ref = (image or "").strip()
        if not ref:
            return
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._images[ref] = ts
            self._save()

    def seed(self, image: str, last_used: float) -> None:
        """bootstrap：仅当账本无此 ref 时写入给定时间。"""
        ref = (image or "").strip()
        if not ref:
            return
        with self._lock:
            if ref in self._images:
                return
            self._images[ref] = float(last_used)
            self._save()

    def forget(self, image: str) -> None:
        ref = (image or "").strip()
        if not ref:
            return
        with self._lock:
            if self._images.pop(ref, None) is None:
                return
            self._save()

    def last_used(self, image: str) -> Optional[float]:
        with self._lock:
            return self._images.get(image)

    def known(self) -> dict[str, float]:
        with self._lock:
            return dict(self._images)

    def candidates(self, *, now: float, ttl: float) -> list[str]:
        if ttl < 0:
            return []
        with self._lock:
            return [ref for ref, ts in self._images.items() if (now - ts) >= ttl]


def default_usage_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / ".sandbox-service" / "image_usage.json"


def bootstrap_repo_images(
    backend: ImageBackend,
    store: ImageUsageStore,
    *,
    agent_image: str,
) -> int:
    """把与 AGENT_IMAGE 同 repo 的本地 tag 按 Created 时间 seed 进账本；返回新登记数。"""
    repo = image_repo(agent_image)
    if not repo:
        return 0
    n = 0
    try:
        tags = backend.list_repo_tags(repo)
    except Exception:
        logger.exception("列出同仓库镜像失败 repo=%s", repo)
        return 0
    known = store.known()
    for ref, created in tags:
        if ref in known:
            continue
        store.seed(ref, created)
        n += 1
    return n


def sweep_idle_images(
    backend: ImageBackend,
    store: ImageUsageStore,
    *,
    protected: frozenset[str],
    ttl: float,
    agent_image: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """清僵尸镜像。返回 ``{removed, skipped_protected, skipped_in_use, failed, bootstrapped}``。"""
    ts = float(now if now is not None else time.time())
    bootstrapped = 0
    if agent_image:
        bootstrapped = bootstrap_repo_images(backend, store, agent_image=agent_image)

    try:
        in_use = backend.list_in_use_images()
    except Exception:
        logger.exception("列出在用镜像失败，本轮跳过 GC")
        return {
            "removed": 0,
            "skipped_protected": 0,
            "skipped_in_use": 0,
            "failed": 0,
            "bootstrapped": bootstrapped,
        }

    prot = {p.strip() for p in protected if p and p.strip()}
    removed = skipped_protected = skipped_in_use = failed = 0

    for ref in store.candidates(now=ts, ttl=ttl):
        if ref in prot:
            skipped_protected += 1
            continue
        idents = frozenset()
        try:
            idents = backend.image_idents(ref)
        except Exception:
            logger.exception("解析镜像标识失败 image=%s", ref)
        if ref in in_use or (idents & in_use):
            skipped_in_use += 1
            continue
        try:
            gone = backend.remove_image(ref)
        except Exception:
            logger.exception("删除镜像失败 image=%s", ref)
            failed += 1
            continue
        store.forget(ref)
        if gone:
            removed += 1
            logger.info("僵尸镜像已删除 image=%s", ref)
        else:
            # 本地已无：账本仍清掉
            removed += 1

    if removed or skipped_protected or skipped_in_use or failed or bootstrapped:
        logger.info(
            "image_gc removed=%d skipped_protected=%d skipped_in_use=%d failed=%d bootstrapped=%d",
            removed, skipped_protected, skipped_in_use, failed, bootstrapped,
        )
    return {
        "removed": removed,
        "skipped_protected": skipped_protected,
        "skipped_in_use": skipped_in_use,
        "failed": failed,
        "bootstrapped": bootstrapped,
    }


__all__ = [
    "ImageUsageStore",
    "ImageBackend",
    "bootstrap_repo_images",
    "default_usage_path",
    "image_repo",
    "split_image_ref",
    "sweep_idle_images",
]

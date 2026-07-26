"""工作区管理：骨架/导入/快照恢复/文件 CRUD（业务中立）。

移植自旧 vm2 workspace_api + copy_loader，去业务化：
- 骨架目录来自配置（``WORKSPACE_SKELETON_DIRS``），服务不预设 inputs/knowledge 等语义。
- 快照恢复按**不透明 payload_key** 下载 tar.gz 解包；tar 内若带
  ``uploads-meta/upload-log.json``（快照格式约定：``{rel: sha}``），且请求给了
  ``blob_key_template``（如 ``users/u1/blobs/{sha2}/{sha}``），则逐条拉回 blob——
  模板由调用方提供，服务不解读归属语义。
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
from pathlib import Path
from typing import Optional

from sandbox_service.objectstore import ObjectNotFoundError, ObjectStore

logger = logging.getLogger("sandbox_service.workspace")


class PathEscapeError(ValueError):
    """路径越出工作区（映射 403 path_escape）。"""


class SnapshotMissingError(RuntimeError):
    """payload_key 指向的快照不存在（映射 404 payload_missing）。"""


def safe_resolve(workspace: Path, rel: str) -> Path:
    """把工作区相对路径解析为绝对路径；禁 ``..``/绝对/符号链接逃逸。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise PathEscapeError(rel)
    target = (workspace / rel).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise PathEscapeError(rel) from exc
    return target


def ensure_workspace(workspace: Path, skeleton_dirs: list[str] | None = None) -> Path:
    """建工作区目录 + 配置化骨架（幂等）。"""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for rel in skeleton_dirs or []:
        safe_resolve(workspace, rel).mkdir(parents=True, exist_ok=True)
    return workspace


def import_tar(workspace: Path, data: bytes, skeleton_dirs: list[str] | None = None) -> int:
    """用 tar.gz（根 = workspace 内容）覆盖导入工作区，返回字节数。"""
    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    ensure_workspace(workspace, skeleton_dirs)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(path=workspace)
    return len(data)


def restore_snapshot(
    workspace: Path,
    store: ObjectStore,
    payload_key: str,
    *,
    preserve: list[str] | None = None,
    blob_key_template: Optional[str] = None,
    skeleton_dirs: list[str] | None = None,
) -> int:
    """把工作区重置为快照内容，返回物化字节数。

    先探测快照存在再清空（避免「清空后才发现快照缺失」把现场清没）；
    ``preserve`` 中的顶层项不清除。
    """
    if not store.exists(payload_key):
        raise SnapshotMissingError(payload_key)

    workspace = Path(workspace)
    keep = frozenset(preserve or [])
    if workspace.is_dir():
        for child in workspace.iterdir():
            if child.name in keep:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    ensure_workspace(workspace, skeleton_dirs)

    try:
        data = store.get_bytes(payload_key)
    except ObjectNotFoundError as exc:
        raise SnapshotMissingError(payload_key) from exc
    total = len(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(path=workspace)

    total += _expand_upload_blobs(workspace, store, blob_key_template)
    return total


def _expand_upload_blobs(workspace: Path, store: ObjectStore, template: Optional[str]) -> int:
    """快照格式约定：tar 内 ``uploads-meta/upload-log.json``（``{rel: sha}``）+ 调用方
    key 模板 → 逐条拉回上传文件字节。无 log/无模板则跳过。"""
    if not template:
        return 0
    log_path = workspace / "uploads-meta" / "upload-log.json"
    if not log_path.is_file():
        return 0
    import json

    try:
        upload_log = json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("upload-log 解析失败 ws=%s", workspace)
        return 0
    total = 0
    for rel, sha in upload_log.items():
        key = template.format(sha=sha, sha2=sha[:2])
        try:
            data = store.get_bytes(key)
        except Exception:
            logger.exception("uploads blob 下载失败 rel=%s sha=%s", rel, sha[:12])
            continue
        target = safe_resolve(workspace, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += len(data)
    return total


# ── 文件树与单文件 ────────────────────────────────────────────────────────────

def build_file_tree(workspace: Path) -> dict:
    """目录在前、文件在后、按名排序；隐藏点开头条目（内部产物不暴露）。"""
    workspace = Path(workspace)

    def _node(path: Path) -> dict:
        rel = path.relative_to(workspace).as_posix()
        if path.is_dir():
            return {
                "name": path.name,
                "path": rel,
                "type": "dir",
                "children": [_node(c) for c in _sorted_visible(path.iterdir())],
            }
        stat = path.stat()
        return {
            "name": path.name,
            "path": rel,
            "type": "file",
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    return {"root": workspace.name, "tree": [_node(c) for c in _sorted_visible(workspace.iterdir())]}


def _sorted_visible(children) -> list[Path]:
    visible = [c for c in children if not c.name.startswith(".")]
    return sorted(visible, key=lambda p: (not p.is_dir(), p.name))


def read_file(workspace: Path, rel: str) -> dict:
    """读文件：UTF-8 可解码按文本返回，否则 base64。"""
    import base64

    target = safe_resolve(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    raw = target.read_bytes()
    try:
        content = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    return {
        "path": rel,
        "content": content,
        "encoding": encoding,
        "size": len(raw),
        "mtime": target.stat().st_mtime,
    }


def write_file(workspace: Path, rel: str, content: str) -> None:
    target = safe_resolve(workspace, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def delete_path(workspace: Path, rel: str) -> None:
    target = safe_resolve(workspace, rel)
    if not target.exists():
        raise FileNotFoundError(rel)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def save_upload(workspace: Path, filename: str, data: bytes, *, subdir: str = "uploads") -> str:
    """保存上传文件到 ``{subdir}/``：重名先比内容（同内容幂等复用），不同则 `` (n)`` 后缀。"""
    name = Path(filename or "").name
    if not name:
        raise ValueError("invalid_filename")
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 0
    while True:
        candidate = name if n == 0 else f"{stem} ({n}){suffix}"
        rel = f"{subdir}/{candidate}" if subdir else candidate
        target = safe_resolve(workspace, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            break
        if target.read_bytes() == data:
            return rel
        n += 1
    target.write_bytes(data)
    return rel


__all__ = [
    "PathEscapeError",
    "SnapshotMissingError",
    "safe_resolve",
    "ensure_workspace",
    "import_tar",
    "restore_snapshot",
    "build_file_tree",
    "read_file",
    "write_file",
    "delete_path",
    "save_upload",
]

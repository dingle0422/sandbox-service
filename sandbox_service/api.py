"""北向中立 API（docs/protocol/sandbox-lifecycle.md §2）。

资源：Sandbox（id 由调用方给定，服务零语义）。业务字段一律不出现在本文件——
应用层把业务上下文编进 ``env``（不透明 map），容器内协议经通用代理透传。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import sandbox_service
from sandbox_service.config import session_paths
from sandbox_service.pool import SandboxCreateError
from sandbox_service.proxy import UpstreamUnreachableError, proxy_request
from sandbox_service.service import NotReadyError, ServiceState
from sandbox_service.workspace import (
    PathEscapeError,
    SnapshotMissingError,
    build_file_tree,
    delete_path,
    ensure_workspace,
    import_tar,
    read_file,
    restore_snapshot,
    save_upload,
    write_file,
)

logger = logging.getLogger("sandbox_service.api")

API_VERSION = "1.0"


class WaitReady(BaseModel):
    path: str = "/agent/health"
    timeout_s: float = 90.0


class ResourceLimits(BaseModel):
    cpu: Optional[float] = None
    mem_mb: Optional[int] = None


class CreateSandboxReq(BaseModel):
    id: str = Field(min_length=1)
    image: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    port: Optional[int] = None
    resource_limits: Optional[ResourceLimits] = None
    egress_allow: Optional[list[str]] = None
    wait_ready: Optional[WaitReady] = None
    #: 本沙箱的事件通知 sink（evict/dead/exited）。调用方自带 → 服务不需持有全局应用地址；
    #: 缺省回落部署级 CALLBACK_URL。见 sandbox-lifecycle.md §2.6。
    callback_url: Optional[str] = None


class SnapshotScope(BaseModel):
    preserve: Optional[list[str]] = None


class SnapshotRestoreReq(BaseModel):
    payload_key: str = Field(min_length=1)
    scope: Optional[SnapshotScope] = None
    #: 快照格式约定的可选 blob key 模板（如 ``users/u1/blobs/{sha2}/{sha}``），调用方提供
    blob_key_template: Optional[str] = None


class FileWrite(BaseModel):
    content: str


def build_router(state: ServiceState) -> APIRouter:
    router = APIRouter()
    s = state.settings

    def _workspace(sandbox_id: str):
        workspace, _debug = session_paths(s.workspace_root, sandbox_id)
        return workspace

    # ── 服务级 ───────────────────────────────────────────────────────────────
    @router.get("/capacity")
    def capacity() -> dict:
        return state.pool.stats()

    # ── 沙箱生命周期 ─────────────────────────────────────────────────────────
    @router.post("/sandboxes")
    def create_sandbox(req: CreateSandboxReq) -> dict:
        workspace = _workspace(req.id)
        ensure_workspace(workspace, s.workspace_skeleton_dirs)
        limits = req.resource_limits or ResourceLimits()
        spec = state.build_spec(
            req.id,
            image=req.image,
            env=req.env,
            port=req.port,
            cpu=limits.cpu,
            mem_mb=limits.mem_mb,
            egress_allow=req.egress_allow,
        )
        meta = {"callback_url": req.callback_url} if req.callback_url else None
        try:
            result = state.pool.acquire(req.id, spec, meta=meta)
        except SandboxCreateError as exc:
            detail = "image_pull_failed" if "pull" in str(exc).lower() else "container_create_failed"
            raise HTTPException(status_code=502, detail=detail) from exc
        if result is None:
            raise HTTPException(status_code=503, detail="capacity_full")
        cid, reused = result

        if req.wait_ready is not None and not reused:
            try:
                state.wait_ready(req.id, path=req.wait_ready.path, timeout=req.wait_ready.timeout_s)
            except NotReadyError as exc:
                logger.error("沙箱就绪超时 id=%s: %s", req.id, exc)
                state.pool.terminate(req.id)
                raise HTTPException(status_code=502, detail="not_ready") from exc

        # keep-warm：起好即归还租约计数，空闲回收从 last_active + TTL 起算
        state.pool.release(req.id)
        return {
            "id": req.id,
            "container_id": cid,
            "status": "reused" if reused else "ready",
            "workspace": str(workspace),
        }

    @router.get("/sandboxes/{sid}")
    def sandbox_status(sid: str) -> dict:
        found = state.pool.resolve(sid)
        if found is None:
            raise HTTPException(status_code=404, detail="sandbox_not_found")
        sandbox_id, lease = found
        try:
            st = state.pool.backend.inspect(lease.container_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"inspect_failed: {exc}") from exc
        out: dict[str, Any] = {
            "id": sandbox_id,
            "container_id": lease.container_id,
            "state": st.state.value,
            "running": st.running,
            "exit_code": st.exit_code,
            "started_at": st.started_at,
        }
        try:
            import httpx

            base = state.pool.base_url(sandbox_id)
            r = httpx.get(f"{base}/agent/health", timeout=3.0)
            out["probe"] = r.json() if r.status_code == 200 else {"ok": False, "status": r.status_code}
        except Exception as exc:
            out["probe"] = {"ok": False, "error": str(exc)}
        return out

    @router.delete("/sandboxes/{sid}")
    def delete_sandbox(sid: str, grace_seconds: float = 5.0) -> dict:
        """销毁沙箱（幂等）。销毁结果以 Docker 为准，不以内存账本为准。

        账本会和真实容器漂移（服务重启丢租约、stop 半途失败），所以摘完租约后
        总要再按 sandbox label 扫一遍容器。扫尾失败就报 500 让调用方重试——
        回 200 却留着容器在跑，等于制造无人认领的孤儿。
        """
        found = state.pool.resolve(sid)
        sandbox_id = found[0] if found is not None else sid
        had_lease = found is not None
        if had_lease:
            state.pool.terminate(sandbox_id, grace_seconds=grace_seconds)
        try:
            swept = state.pool.backend.stop_for_sandbox(sandbox_id)
        except Exception as exc:
            logger.exception("沙箱销毁扫尾失败 sandbox=%s", sandbox_id)
            raise HTTPException(status_code=500, detail=f"terminate_failed: {exc}") from exc
        return {"ok": True, "terminated": bool(had_lease or swept)}

    # ── 通用端点代理 ─────────────────────────────────────────────────────────
    @router.api_route(
        "/sandboxes/{sid}/proxy/{port}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy(sid: str, port: int, path: str, request: Request):
        found = state.pool.resolve(sid)
        if found is None:
            raise HTTPException(status_code=404, detail="sandbox_not_found")
        sandbox_id, _lease = found
        try:
            base = state.pool.base_url(sandbox_id, port)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="sandbox_unreachable") from exc
        try:
            response = await proxy_request(request, base, path)
        except UpstreamUnreachableError as exc:
            raise HTTPException(status_code=502, detail="sandbox_unreachable") from exc
        state.pool.touch(sandbox_id)  # 有流量即视为活跃
        return response

    # ── 工作区 ───────────────────────────────────────────────────────────────
    @router.post("/sandboxes/{sid}/workspace/ensure")
    def ws_ensure(sid: str) -> dict:
        workspace = ensure_workspace(_workspace(sid), s.workspace_skeleton_dirs)
        _ws, debug = session_paths(s.workspace_root, sid)
        debug.mkdir(parents=True, exist_ok=True)
        return {"id": sid, "workspace": str(workspace), "ok": True}

    @router.post("/sandboxes/{sid}/workspace/import")
    async def ws_import(sid: str, file: UploadFile = File(...)) -> dict:
        data = await file.read()
        n = import_tar(_workspace(sid), data, s.workspace_skeleton_dirs)
        return {"ok": True, "bytes": n}

    @router.post("/sandboxes/{sid}/workspace/snapshot/restore")
    def ws_restore(sid: str, req: SnapshotRestoreReq) -> dict:
        preserve = (req.scope.preserve if req.scope and req.scope.preserve is not None
                    else s.restore_preserve)
        try:
            total = restore_snapshot(
                _workspace(sid),
                state.store,
                req.payload_key,
                preserve=preserve,
                blob_key_template=req.blob_key_template,
                skeleton_dirs=s.workspace_skeleton_dirs,
            )
        except SnapshotMissingError as exc:
            raise HTTPException(status_code=404, detail="payload_missing") from exc
        return {"ok": True, "bytes": total}

    @router.get("/sandboxes/{sid}/workspace/files")
    def ws_tree(sid: str) -> dict:
        workspace = _workspace(sid)
        if not workspace.is_dir():
            raise HTTPException(status_code=404, detail="workspace_not_found")
        return build_file_tree(workspace)

    @router.get("/sandboxes/{sid}/workspace/files/{file_path:path}")
    def ws_read(sid: str, file_path: str) -> dict:
        try:
            return read_file(_workspace(sid), file_path)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="file_not_found") from exc

    @router.put("/sandboxes/{sid}/workspace/files/{file_path:path}")
    def ws_write(sid: str, file_path: str, body: FileWrite) -> dict:
        workspace = _workspace(sid)
        if not workspace.is_dir():
            raise HTTPException(status_code=404, detail="workspace_not_found")
        try:
            write_file(workspace, file_path, body.content)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        return {"path": file_path, "ok": True}

    @router.delete("/sandboxes/{sid}/workspace/files/{file_path:path}")
    def ws_delete(sid: str, file_path: str) -> dict:
        try:
            delete_path(_workspace(sid), file_path)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="file_not_found") from exc
        return {"path": file_path, "deleted": True}

    @router.post("/sandboxes/{sid}/workspace/files", status_code=201)
    async def ws_upload(sid: str, file: UploadFile = File(...)) -> dict:
        workspace = ensure_workspace(_workspace(sid), s.workspace_skeleton_dirs)
        data = await file.read()
        try:
            rel = save_upload(workspace, file.filename or "", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_filename") from exc
        return {"path": rel, "size": len(data)}

    return router


def health_payload(state: ServiceState) -> dict:
    return {
        "ok": True,
        "apiVersion": API_VERSION,
        "version": sandbox_service.__version__,
        "image": state.settings.agent_image,
    }


__all__ = ["build_router", "health_payload", "API_VERSION"]

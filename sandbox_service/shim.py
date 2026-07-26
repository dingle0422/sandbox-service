"""Legacy shim：旧 vm2（sandbox_manager）路由的**被动**兼容层。

本层只做「请求 → 翻译 → 转发容器内协议 → 响应」，对每个路由都是被调用才动作，
**不含任何主动回调应用后端的编排**（周期归档循环 / 终态自动归档 / 把结果 POST 回
vm1 的 /internal/* 全部移除——那些是税务业务编排，归应用层 vm1，见 p2-vm1）。

因此 vm1 切到本服务：请求/响应 API 面零改（start/input/resume/cancel/events/
archive/terminate/workspace/blobs 形状不变）；但**推送型行为**（草稿定时/终态归档、
容器退出入账）由 vm1 自己承担——归档由 vm1 定时或在消费到终态事件时显式调
`/containers/{cid}/archive`；容器退出/逐出经通用 webhook（CALLBACK_URL 或按沙箱
callback_url）消费。

保留的 legacy 被动语义（标注 [tax legacy]，v1 生命周期后随 vm1 切新路由删除）：
- ``/containers/start`` 把业务字段翻译成容器 env（ENTERPRISE_ID 等）+ MinIO 凭据注入；
- ``/containers/{cid}/events`` SSE 纯中继；
- ``/containers/{cid}/archive`` 用租约元数据补全 owner/project 后转发容器；
- ``DELETE /blobs/{sha}`` 直删对象存储 blob；
- ``/workspaces/*`` 文件与快照恢复（恢复带 uploads blob 展开）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sandbox_service.config import session_paths
from sandbox_service.pool import SandboxCreateError
from sandbox_service.service import NotReadyError, ServiceState
from sandbox_service.workspace import (
    SnapshotMissingError,
    ensure_workspace,
    import_tar,
    restore_snapshot,
)

logger = logging.getLogger("sandbox_service.shim")

#: 本服务期望的 agent 契约 major（docs/protocol/agent-contract.md §0.1）
EXPECTED_CONTRACT_MAJOR = 1


def _legacy_blob_key(owner_id: str, sha: str) -> str:
    return f"users/{owner_id}/blobs/{sha[:2]}/{sha}"


def _legacy_blob_template(owner_id: str) -> str:
    return f"users/{owner_id}/blobs/{{sha2}}/{{sha}}"


# ── 请求模型（与旧 sandbox_manager/app/api.py 同形）─────────────────────────────


class StartReq(BaseModel):
    session_id: str
    owner_id: str
    workspace: Optional[str] = None
    payload_key: Optional[str] = None
    token: Optional[str] = None
    run_config: Optional[dict[str, Any]] = None
    resource_limits: Optional[dict[str, Any]] = None
    egress_allow: Optional[list[str]] = None
    project_id: Optional[str] = None
    enterprise_id: Optional[str] = None
    period: Optional[str] = None
    template: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)


class InputReq(BaseModel):
    run_id: Optional[str] = None
    message: Optional[Any] = None
    run_request: Optional[dict[str, Any]] = None


class ResumeReq(BaseModel):
    interrupt_id: Optional[str] = None
    resume_item: Optional[Any] = None
    run_request: Optional[dict[str, Any]] = None


class ArchiveReq(BaseModel):
    kind: str = "draft"
    version_id: Optional[str] = None
    draft_id: Optional[str] = None
    owner_id: Optional[str] = None
    project_id: Optional[str] = None


class CancelReq(BaseModel):
    run_id: Optional[str] = None


class TerminateReq(BaseModel):
    grace_seconds: float = 5.0


class RestoreReq(BaseModel):
    owner_id: str
    payload_key: str


class FileWrite(BaseModel):
    content: str


# ── agent 通道辅助（同步 httpx，语义对齐旧 InputDownlink）───────────────────────


class _AgentChannel:
    def __init__(self, state: ServiceState) -> None:
        self._state = state

    def base(self, sid_or_cid: str) -> tuple[str, str]:
        """返回 ``(sandbox_id, agent_base_url)``；找不到抛 404。"""
        found = self._state.pool.resolve(sid_or_cid)
        if found is None:
            raise HTTPException(status_code=404, detail="session not in pool")
        sandbox_id, _lease = found
        try:
            return sandbox_id, self._state.pool.base_url(sandbox_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def post(self, sid_or_cid: str, path: str, body: dict, *, timeout: float = 30.0) -> dict:
        sandbox_id, base = self.base(sid_or_cid)
        url = f"{base.rstrip('/')}{path}"
        try:
            r = httpx.post(url, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"POST {url} failed: {exc}") from exc
        if r.status_code == 409:
            raise HTTPException(status_code=409, detail="run_busy")
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"POST {url} -> {r.status_code}") from None
        self._state.pool.touch(sandbox_id)
        return r.json() if r.content else {}


# ── 路由 ─────────────────────────────────────────────────────────────────────


def build_shim_router(state: ServiceState) -> APIRouter:
    router = APIRouter()
    s = state.settings
    channel = _AgentChannel(state)

    def _default_egress() -> list[str]:
        return list(s.agent_egress_allow) if s.agent_egress_allow else ["vm1"]

    def _minio_env() -> dict[str, str]:
        # [tax legacy] agent 数据面直连对象存储：把本服务的 MinIO 凭据注入容器
        out = {}
        for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
                  "MINIO_SECURE", "MINIO_DEFAULT_BUCKET", "MINIO_REGION"):
            v = os.getenv(k)
            if v is not None:
                out[k] = v
        return out

    # ── containers ───────────────────────────────────────────────────────────
    @router.post("/containers/start")
    def start_container(req: StartReq) -> dict:
        workspace, debug_dir = session_paths(s.workspace_root, req.session_id)
        ensure_workspace(workspace, s.workspace_skeleton_dirs)
        debug_dir.mkdir(parents=True, exist_ok=True)

        rc = req.run_config or {}
        enterprise_id = req.enterprise_id or str(rc.get("enterprise_id") or "")
        period = req.period or str(rc.get("period") or "")
        template = req.template or str(rc.get("template") or "demo")

        env = dict(req.env)
        env.setdefault("SESSION_ID", req.session_id)
        env.setdefault("OWNER_ID", req.owner_id)
        env.setdefault("WORKSPACE", "/workspace")
        env.setdefault("DEBUG_DIR", "/tmp/debug")
        if req.project_id:
            env.setdefault("PROJECT_ID", req.project_id)
        if req.payload_key:
            env.setdefault("PAYLOAD_KEY", req.payload_key)
        if enterprise_id:
            env.setdefault("ENTERPRISE_ID", enterprise_id)
        if period:
            env.setdefault("PERIOD", period)
        if template:
            env.setdefault("TEMPLATE", template)
        if req.token:
            env.setdefault("AGENT_TOKEN", req.token)
            env.setdefault("LLM_API_KEY", req.token)
        for k, v in _minio_env().items():
            env.setdefault(k, v)

        egress = list(req.egress_allow) if req.egress_allow else _default_egress()
        if egress and not s.agent_network:
            logger.warning("egress_allow 已设但 AGENT_NETWORK 为空，出网隔离无法落地")

        limits = req.resource_limits or {}
        spec = state.build_spec(
            req.session_id,
            env=env,
            cpu=limits.get("cpu"),
            mem_mb=limits.get("mem_mb"),
            egress_allow=egress,
        )
        try:
            result = state.pool.acquire(
                req.session_id,
                spec,
                meta={"owner_id": req.owner_id, "project_id": req.project_id or ""},
            )
        except SandboxCreateError as exc:
            detail = "image_pull_failed" if "pull" in str(exc).lower() else "container_create_failed"
            raise HTTPException(status_code=502, detail=detail) from exc
        if result is None:
            raise HTTPException(status_code=503, detail="capacity_full")
        cid, _reused = result

        try:
            payload = state.wait_ready(req.session_id, timeout=90.0)
        except NotReadyError as exc:
            logger.exception("agent health wait failed cid=%s", cid)
            state.pool.terminate(req.session_id)
            raise HTTPException(status_code=502, detail="agent_not_ready") from exc

        version = payload.get("contractVersion")
        if isinstance(version, str) and version.split(".", 1)[0] != str(EXPECTED_CONTRACT_MAJOR):
            logger.error("agent 契约不兼容 cid=%s version=%s", cid, version)
            state.pool.terminate(req.session_id)
            raise HTTPException(status_code=502, detail="agent_contract_mismatch")

        state.pool.release(req.session_id)
        return {
            "container_id": cid,
            "status": "ready",
            "materialized_bytes": 0,
            "workspace": str(workspace),
            "agent_materialize": True,
        }

    @router.post("/containers/{cid}/input", status_code=202)
    def forward_input(cid: str, req: InputReq) -> dict:
        if not req.run_request:
            raise HTTPException(status_code=400, detail="run_request_required")
        channel.post(cid, "/agent/input", dict(req.run_request))
        return {"status": "accepted"}

    @router.post("/containers/{cid}/resume", status_code=202)
    def forward_resume(cid: str, req: ResumeReq) -> dict:
        if not req.run_request:
            raise HTTPException(status_code=400, detail="run_request_required")
        channel.post(cid, "/agent/resume", dict(req.run_request))
        return {"status": "accepted"}

    @router.post("/containers/{cid}/cancel")
    def cancel_run(cid: str, req: CancelReq | None = None) -> dict:
        channel.post(cid, "/agent/cancel", {"run_id": req.run_id} if req and req.run_id else {})
        return {"ok": True}

    @router.get("/containers/{cid}/events")
    def stream_events(cid: str):
        """SSE 纯中继：把容器 /agent/events 逐帧透传。

        无副作用——终态归档由 vm1 在消费到终态事件时自行触发（调 /containers/{cid}/archive）。
        """
        try:
            sandbox_id, base = channel.base(cid)
        except HTTPException:
            def _err():
                err = {"type": "RUN_ERROR", "content": "session not in pool", "seq": 1}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            return StreamingResponse(_err(), media_type="text/event-stream")

        state.pool.touch(sandbox_id)  # 有消费流量即视为活跃，刷新 TTL

        def gen():
            url = f"{base.rstrip('/')}/agent/events"
            try:
                with httpx.stream("GET", url, timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            yield f"{line}\n\n"
            except httpx.HTTPError as exc:
                err = {"type": "RUN_ERROR", "content": str(exc), "seq": 1}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.post("/containers/{cid}/archive")
    def archive_container(cid: str, req: ArchiveReq) -> dict:
        found = state.pool.resolve(cid)
        if found is None:
            raise HTTPException(status_code=404, detail="session not in pool")
        sandbox_id, lease = found
        body = {
            "kind": req.kind,
            "owner_id": req.owner_id or lease.meta.get("owner_id", ""),
            "session_id": sandbox_id,
            "version_id": req.version_id,
            "draft_id": req.draft_id or sandbox_id,
            "project_id": req.project_id or lease.meta.get("project_id") or None,
        }
        try:
            return channel.post(sandbox_id, "/agent/archive", body, timeout=120.0)
        except HTTPException as exc:
            if exc.status_code == 502:
                raise HTTPException(status_code=502, detail="archive_failed") from exc
            raise

    @router.post("/containers/{cid}/terminate")
    def terminate_container(cid: str, req: TerminateReq | None = None) -> dict:
        grace = req.grace_seconds if req else 5.0
        found = state.pool.resolve(cid)
        try:
            if found is not None:
                sandbox_id = found[0]
                state.pool.terminate(sandbox_id, grace_seconds=grace)
                # 摘租约后按 label 复扫：stop 半途失败不能静默留下容器（见 api.delete_sandbox）
                state.pool.backend.stop_for_sandbox(sandbox_id)
            else:
                # 账本无此租约（重启丢账本 / forget 后残留）：直接按 container id 停
                state.pool.backend.stop(cid, timeout=grace)
        except Exception as exc:
            logger.exception("容器销毁失败 cid=%s", cid)
            raise HTTPException(status_code=500, detail=f"terminate_failed: {exc}") from exc
        return {"ok": True}

    @router.get("/containers/{cid}/health")
    def container_health(cid: str) -> dict:
        found = state.pool.resolve(cid)
        real_cid = found[1].container_id if found else cid
        try:
            st = state.pool.backend.inspect(real_cid)
            out: dict[str, Any] = {
                "state": st.state.value,
                "running": st.running,
                "exit_code": st.exit_code,
                "started_at": st.started_at,
            }
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"inspect_failed: {exc}") from exc
        try:
            if found is None:
                raise RuntimeError("not in pool")
            base = state.pool.base_url(found[0])
            r = httpx.get(f"{base}/agent/health", timeout=3.0)
            out["agent"] = r.json() if r.status_code == 200 else {"ok": False, "status": r.status_code}
        except Exception as exc:
            out["agent"] = {"ok": False, "error": str(exc)}
        return out

    # ── blobs ────────────────────────────────────────────────────────────────
    @router.delete("/blobs/{sha}")
    def delete_blob(sha: str, owner_id: str = Query(...)) -> dict:
        try:
            state.store.delete(_legacy_blob_key(owner_id, sha))
        except Exception as exc:
            logger.exception("delete blob failed sha=%s", sha)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    # ── workspaces（旧路径 → 新 workspace 实现）───────────────────────────────
    @router.post("/workspaces/{session_id}/ensure")
    def ws_ensure(session_id: str) -> dict:
        workspace, debug = session_paths(s.workspace_root, session_id)
        ensure_workspace(workspace, s.workspace_skeleton_dirs)
        debug.mkdir(parents=True, exist_ok=True)
        return {"session_id": session_id, "workspace": str(workspace), "ok": True}

    @router.post("/workspaces/{session_id}/import")
    async def ws_import(session_id: str, file: UploadFile = File(...)) -> dict:
        workspace, debug = session_paths(s.workspace_root, session_id)
        data = await file.read()
        n = import_tar(workspace, data, s.workspace_skeleton_dirs)
        debug.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "bytes": n}

    @router.post("/workspaces/{session_id}/restore")
    def ws_restore(session_id: str, req: RestoreReq) -> dict:
        workspace, debug = session_paths(s.workspace_root, session_id)
        debug.mkdir(parents=True, exist_ok=True)
        try:
            total = restore_snapshot(
                workspace,
                state.store,
                req.payload_key,
                preserve=s.restore_preserve,
                blob_key_template=_legacy_blob_template(req.owner_id),
                skeleton_dirs=s.workspace_skeleton_dirs,
            )
        except SnapshotMissingError as exc:
            raise HTTPException(status_code=404, detail="version_payload_missing") from exc
        return {"ok": True, "bytes": total}

    # 文件 CRUD 与新 API 同实现，挂旧前缀
    from sandbox_service.workspace import (
        PathEscapeError,
        build_file_tree,
        delete_path,
        read_file,
        save_upload,
        write_file,
    )

    def _ws(session_id: str):
        workspace, _debug = session_paths(s.workspace_root, session_id)
        return workspace

    @router.get("/workspaces/{session_id}/files")
    def ws_tree(session_id: str) -> dict:
        workspace = _ws(session_id)
        if not workspace.is_dir():
            raise HTTPException(status_code=404, detail="workspace_not_found")
        return build_file_tree(workspace)

    @router.get("/workspaces/{session_id}/files/{file_path:path}")
    def ws_read(session_id: str, file_path: str) -> dict:
        try:
            return read_file(_ws(session_id), file_path)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="file_not_found") from exc

    @router.put("/workspaces/{session_id}/files/{file_path:path}")
    def ws_write(session_id: str, file_path: str, body: FileWrite) -> dict:
        workspace = _ws(session_id)
        if not workspace.is_dir():
            raise HTTPException(status_code=404, detail="workspace_not_found")
        try:
            write_file(workspace, file_path, body.content)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        return {"path": file_path, "ok": True}

    @router.delete("/workspaces/{session_id}/files/{file_path:path}")
    def ws_delete(session_id: str, file_path: str) -> dict:
        try:
            delete_path(_ws(session_id), file_path)
        except PathEscapeError as exc:
            raise HTTPException(status_code=403, detail="path_escape") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="file_not_found") from exc
        return {"path": file_path, "deleted": True}

    @router.post("/workspaces/{session_id}/files", status_code=201)
    async def ws_upload(session_id: str, file: UploadFile = File(...)) -> dict:
        workspace = ensure_workspace(_ws(session_id), s.workspace_skeleton_dirs)
        data = await file.read()
        try:
            rel = save_upload(workspace, file.filename or "", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_filename") from exc
        return {"path": rel, "size": len(data)}

    return router


__all__ = [
    "build_shim_router",
    "EXPECTED_CONTRACT_MAJOR",
]

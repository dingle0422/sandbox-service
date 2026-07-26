"""echo-agent：agent-contract v1.0 的最小零业务参考实现。

用途：
1. 沙箱服务（sandbox_service）通用路径的冒烟/集成载体——不拖 LLM / MinIO / 企业播种；
2. 证明「任何遵循 agent-contract 的镜像都能接入」这一解耦承诺；
3. p3 conformance 套件的基准 agent。

行为：把 ``user_text`` 原样 echo 回一条 assistant 消息，走完整事件流并收尾。
所有端点严格对齐 docs/protocol/agent-contract.md；对未知扩展字段一律容忍。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONTRACT_VERSION = "1.0"

app = FastAPI(title="echo-agent")


def _workspace() -> Path:
    """每次调用现读 ``WORKSPACE`` env（便于同进程多实例/测试注入，不锁死在 import 期）。"""
    return Path(os.getenv("WORKSPACE", "/workspace"))


class _Run:
    """单容器单活跃 run 的事件缓冲（list + 条件变量，支持流式增量读 + 取消）。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[dict] = []
        self._closed = False
        self._cancel = threading.Event()
        self._cond = threading.Condition()

    def emit(self, event: dict) -> None:
        with self._cond:
            self._events.append(event)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def done(self) -> bool:
        return self._closed

    def stream(self):
        """从头 yield 事件，直到 close()；边界等待新事件。"""
        idx = 0
        while True:
            with self._cond:
                while idx >= len(self._events) and not self._closed:
                    self._cond.wait(timeout=1.0)
                batch = self._events[idx:]
                idx = len(self._events)
                closed = self._closed
            for ev in batch:
                yield ev
            if closed and idx >= len(self._events):
                return


_active: Optional[_Run] = None
_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _run_worker(run: _Run, user_text: str, thread_id: Optional[str]) -> None:
    """后台执行：RUN_STARTED → TEXT_MESSAGE(echo) → RUN_FINISHED → __finalize__。"""
    try:
        run.emit({"type": "RUN_STARTED", "threadId": thread_id, "seq": _next_seq()})
        reply = f"echo: {user_text}" if user_text else "echo: (empty)"
        msg_id = f"m-{run.run_id}"
        run.emit({"type": "TEXT_MESSAGE_START", "messageId": msg_id, "role": "assistant", "seq": _next_seq()})
        for chunk in (reply[i : i + 8] for i in range(0, len(reply), 8)):
            if run.cancelled:
                break
            run.emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": chunk, "seq": _next_seq()})
            time.sleep(0.05)
        run.emit({"type": "TEXT_MESSAGE_END", "messageId": msg_id, "seq": _next_seq()})

        if run.cancelled:
            run.emit({"type": "RUN_CANCELLED", "seq": _next_seq()})
            status = "cancelled"
        else:
            run.emit({"type": "RUN_FINISHED", "threadId": thread_id, "seq": _next_seq()})
            status = "completed"

        message = {
            "info": {"id": msg_id, "role": "assistant"},
            "parts": [{"id": f"p-{run.run_id}", "messageId": msg_id, "type": "text", "text": reply}],
        }
        run.emit(
            {
                "type": "__finalize__",
                "status": status,
                "message": message,
                "transcript": [{"role": "assistant", "content": reply}],
                "interrupt_id": None,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 错误入流，不改 HTTP 状态
        run.emit({"type": "RUN_ERROR", "content": str(exc), "seq": _next_seq()})
        run.emit({"type": "__finalize__", "status": "error", "message": {}, "transcript": [], "interrupt_id": None})
    finally:
        run.close()


def _start_run(body: dict) -> JSONResponse:
    global _active
    with _lock:
        if _active is not None and not _active.done:
            return JSONResponse({"detail": "run_busy"}, status_code=409)
        run_id = str(body.get("run_id") or f"r-{int(time.time()*1000)}")
        _ensure_materialized()  # 幂等兜底：接受 run 前保证工作区已物化
        run = _Run(run_id)
        _active = run
    threading.Thread(
        target=_run_worker,
        args=(run, str(body.get("user_text") or ""), body.get("thread_id")),
        daemon=True,
    ).start()
    return JSONResponse({"status": "accepted", "run_id": run_id}, status_code=202)


def _ensure_materialized(force: bool = False) -> None:
    """写幂等标记（echo 不播种任何数据；mode 恒 "skipped"）。"""
    workspace = _workspace()
    marker = workspace / ".agent" / "materialized.json"
    if marker.is_file() and not force:
        return
    workspace.mkdir(parents=True, exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"agent": "echo", "ts": time.time()}), encoding="utf-8")


# ── 端点 ─────────────────────────────────────────────────────────────────────


@app.get("/agent/health")
def health() -> dict[str, Any]:
    busy = _active is not None and not _active.done
    return {
        "ok": True,
        "busy": busy,
        "run_id": _active.run_id if busy and _active else None,
        "contractVersion": CONTRACT_VERSION,
    }


@app.post("/agent/input")
async def submit_input(request: Request):
    return _start_run(await request.json())


@app.post("/agent/resume")
async def submit_resume(request: Request):
    return _start_run(await request.json())


@app.post("/agent/cancel")
async def cancel(request: Request) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    run = _active
    if run is None or run.done:
        return {"ok": True, "cancelled": False}
    want = body.get("run_id")
    if want and want != run.run_id:
        return {"ok": True, "cancelled": False}
    run.cancel()
    return {"ok": True, "cancelled": True, "run_id": run.run_id}


@app.get("/agent/events")
def events():
    run = _active

    def gen():
        if run is None:
            yield f"data: {json.dumps({'type': 'RUN_ERROR', 'content': 'no_active_run', 'seq': 1})}\n\n"
            return
        for ev in run.stream():
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/agent/materialize")
async def materialize(request: Request) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    _ensure_materialized(force=bool(body.get("force")))
    return {"mode": "skipped", "bytes_loaded": 0, "inputs": None, "payload_key": None, "detail": "echo"}


@app.post("/agent/archive")
async def archive(request: Request) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    sid = body.get("session_id") or os.getenv("SESSION_ID") or "s"
    owner = body.get("owner_id") or os.getenv("OWNER_ID") or "u"
    # echo 不接对象存储：返回稳定 stub key，changed=false（无副作用）。
    return {
        "payload_key": f"users/{owner}/sessions/{sid}/payload/echo.tar.gz",
        "changed": False,
        "payload_bytes": 0,
        "kind": body.get("kind", "draft"),
        "version_id": body.get("version_id"),
        "draft_id": body.get("draft_id") or sid,
        "project_id": body.get("project_id"),
    }


__all__ = ["app"]

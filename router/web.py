from __future__ import annotations

import json
import os
import threading
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from modeldeck.mtplx import fetch_json, fetch_sse_snapshot, installed_models
from modeldeck.prompts import PROMPTS
from modeldeck.secrets import get_openai_api_key, set_openai_api_key
from modeldeck.state import (
    activate_local,
    activate_planner,
    load_state,
    save_state,
)

router = APIRouter()

# Shared Pipeline instance -- set by router.main at startup
_pipeline: Any = None
_manager: Any = None


def init(pipeline: Any, manager: Any) -> None:
    """Called from router.main to wire the shared Pipeline and ProcessManager."""
    global _pipeline, _manager
    _pipeline = pipeline
    _manager = manager


def _get_pipeline() -> Any:
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    return _pipeline


# ---------------------------------------------------------------------------
# SSE event broadcast -- the web UI subscribes to /api/pipeline/events
# ---------------------------------------------------------------------------

_event_subscribers: list[queue.Queue] = []  # type: ignore[name-defined]
import queue  # noqa: E402

_event_lock = threading.Lock()


def _broadcast_event(event: dict[str, Any]) -> None:
    """Fan out a Pipeline event to all connected SSE subscribers."""
    with _event_lock:
        for q in list(_event_subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def _make_event_sink() -> Any:
    """Return an event sink callable that broadcasts to SSE subscribers."""
    def sink(event: dict[str, Any]) -> None:
        _broadcast_event(event)
    return sink


# ---------------------------------------------------------------------------
# State endpoints
# ---------------------------------------------------------------------------

@router.get("/api/state")
async def get_state() -> dict[str, Any]:
    return load_state()


@router.put("/api/state/roles/{phase}")
async def update_role(phase: str, request: Request) -> dict[str, Any]:
    if phase not in ("scout", "builder", "renovator", "auditor"):
        raise HTTPException(status_code=400, detail=f"Unknown role: {phase}")
    body = await request.json()
    state = load_state()
    role = state["roles"][phase]
    allowed_fields = {
        "model", "reasoning", "context_window", "depth", "sampling_mode",
        "temperature", "top_p", "top_k", "min_p", "presence_penalty",
        "repetition_penalty",
    }
    for key, value in body.items():
        if key in allowed_fields:
            role[key] = value
    save_state(state)
    return {"ok": True, "role": role}


@router.put("/api/state/planner")
async def update_planner(request: Request) -> dict[str, Any]:
    body = await request.json()
    state = load_state()
    planner = state["planner"]
    # local_phase is gone: the planner has its own role, edited like any
    # other role rather than as a pointer at someone else's.
    allowed_fields = {"kind", "model", "base_url", "reasoning_effort"}
    for key, value in body.items():
        if key in allowed_fields:
            planner[key] = value
    save_state(state)
    return {"ok": True, "planner": planner}


@router.put("/api/state/active")
async def update_active(request: Request) -> dict[str, Any]:
    body = await request.json()
    phase = body.get("phase")
    if phase not in ("scout", "planner", "builder", "auditor"):
        raise HTTPException(status_code=400, detail=f"Unknown phase: {phase}")
    state = load_state()
    if phase == "planner":
        activate_planner(state)
    else:
        activate_local(state, phase)
    save_state(state)
    return {"ok": True, "active": state["active"]}


# ---------------------------------------------------------------------------
# Pipeline control endpoints
# ---------------------------------------------------------------------------

@router.post("/api/pipeline/start")
async def pipeline_start(request: Request) -> dict[str, Any]:
    p = _get_pipeline()
    body = await request.json()
    mode = body.get("mode", "full")
    path = body.get("path", "")
    task = body.get("task", "")
    start_phase = body.get("start_phase", "scout")
    result = p.start(mode, path, task, start_phase)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/api/pipeline/stop")
async def pipeline_stop() -> dict[str, Any]:
    p = _get_pipeline()
    p.stop()
    return {"ok": True}


@router.get("/api/pipeline/status")
async def pipeline_status() -> dict[str, Any]:
    p = _get_pipeline()
    return p.status()


@router.post("/api/pipeline/answer")
async def pipeline_answer(request: Request) -> dict[str, Any]:
    p = _get_pipeline()
    body = await request.json()
    phase = body.get("phase", "")
    answer = body.get("answer", "")
    p.answer(phase, answer)
    return {"ok": True}


@router.get("/api/pipeline/events")
async def pipeline_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of Pipeline events."""
    q: queue.Queue = queue.Queue(maxsize=1024)
    with _event_lock:
        _event_subscribers.append(q)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Send a keepalive comment
                    yield ": keepalive\n\n"
        finally:
            with _event_lock:
                if q in _event_subscribers:
                    _event_subscribers.remove(q)

    return StreamingResponse(generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Telemetry endpoint
# ---------------------------------------------------------------------------

@router.get("/api/telemetry")
async def telemetry() -> dict[str, Any]:
    state = load_state()
    active = state["active"]
    result: dict[str, Any] = {
        "active": active,
        "health": None,
        "metrics": None,
        "flight": None,
    }
    if active["kind"] != "local":
        return result
    base = str(active["base_url"]).removesuffix("/v1")
    try:
        result["health"] = fetch_json(f"{base}/health", timeout=0.3)
    except Exception:
        pass
    try:
        result["metrics"] = fetch_json(f"{base}/metrics", timeout=0.3)
    except Exception:
        pass
    try:
        result["flight"] = fetch_json(f"{base}/v1/mtplx/flight", timeout=0.3)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Loop alert (alias existing endpoints under /api/)
# ---------------------------------------------------------------------------

@router.get("/api/loop-alert")
async def loop_alert() -> dict[str, Any]:
    from . import loop_guard
    return loop_guard.status()


# ---------------------------------------------------------------------------
# Prompt override endpoints
# ---------------------------------------------------------------------------

@router.get("/api/prompts")
async def get_prompts() -> dict[str, Any]:
    state = load_state()
    overrides = state.get("prompt_overrides") or {}
    result: dict[str, Any] = {}
    for phase, (document, default_prompt) in PROMPTS.items():
        override = overrides.get(phase)
        is_override = isinstance(override, str) and bool(override.strip())
        result[phase] = {
            "document": document,
            "default": default_prompt,
            "override": override if is_override else None,
            "is_override": is_override,
        }
    return result


@router.put("/api/prompts/{phase}")
async def put_prompt(phase: str, request: Request) -> dict[str, Any]:
    if phase not in PROMPTS:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {phase}")
    body = await request.json()
    text = body.get("text", "")
    state = load_state()
    overrides = dict(state.get("prompt_overrides") or {})
    if text.strip():
        overrides[phase] = text
    else:
        overrides.pop(phase, None)
    state["prompt_overrides"] = overrides
    save_state(state)
    return {"ok": True}


@router.delete("/api/prompts/{phase}")
async def delete_prompt(phase: str) -> dict[str, Any]:
    if phase not in PROMPTS:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {phase}")
    state = load_state()
    overrides = dict(state.get("prompt_overrides") or {})
    overrides.pop(phase, None)
    state["prompt_overrides"] = overrides
    save_state(state)
    entry = PROMPTS.get(phase)
    return {"ok": True, "default": entry[1] if entry else ""}


# ---------------------------------------------------------------------------
# Secrets endpoints
# ---------------------------------------------------------------------------

@router.put("/api/secrets/openai")
async def set_secret(request: Request) -> dict[str, Any]:
    body = await request.json()
    key = body.get("key", "")
    if not key.strip():
        raise HTTPException(status_code=400, detail="Key cannot be empty")
    try:
        set_openai_api_key(key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@router.get("/api/secrets/openai/status")
async def secret_status() -> dict[str, Any]:
    has_key = get_openai_api_key() is not None
    return {"has_key": has_key}


# ---------------------------------------------------------------------------
# Models inventory
# ---------------------------------------------------------------------------

@router.get("/api/models")
async def models_list() -> dict[str, Any]:
    try:
        models = installed_models()
    except Exception:
        models = []
    return {"models": models}


# ---------------------------------------------------------------------------
# Static file serving (mobile UI)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


@router.get("/static/{filename}")
async def static_file(filename: str) -> FileResponse:
    path = _STATIC_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }
    ext = path.suffix
    return FileResponse(path, media_type=media_types.get(ext, "application/octet-stream"))

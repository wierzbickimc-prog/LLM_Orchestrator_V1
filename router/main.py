from __future__ import annotations

import json
import os
import time
from ipaddress import IPv4Address, IPv4Network
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from modeldeck.mtplx import ProcessManager, RUN_DIR
from modeldeck.pipeline import Pipeline
from modeldeck.prompts import effective_prompt
from modeldeck.secrets import get_openai_api_key
from modeldeck.state import load_state

from . import loop_guard, web
from .settings import Backend, load_settings

# How much of each message's content to keep verbatim in the capture file
# before truncating -- enough to see what's actually in a system prompt or
# an environment_details block without risking a multi-MB dump.
_CAPTURE_CONTENT_PREVIEW_CHARS = 4000

settings = load_settings()
app = FastAPI(title="Model Deck Router", version="0.1.0")

# ---------------------------------------------------------------------------
# Tailscale source-IP allowlist middleware
# ---------------------------------------------------------------------------

# Tailscale CGNAT range (100.64.0.0/10) is always allowed for non-loopback.
_TAILSCALE_CGNAT = IPv4Network("100.64.0.0/10")


def _load_allowed_cidrs() -> list[IPv4Network]:
    """Parse the TAILSCALE_CIDRS env var (comma-separated CIDRs)."""
    raw = os.getenv("TAILSCALE_CIDRS", "").strip()
    if not raw:
        return []
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(IPv4Network(part, strict=False))
        except ValueError:
            pass
    return networks


def _is_allowed_source(host: str | None) -> bool:
    """Loopback is always allowed. Otherwise the source must be within the
    Tailscale CGNAT range or an operator-configured TAILSCALE_CIDRS entry."""
    if host is None:
        return False
    # Always allow loopback
    if host in ("127.0.0.1", "::1"):
        return True
    try:
        addr = IPv4Address(host)
    except ValueError:
        # IPv6 or unparseable -- reject (conservative)
        return False
    if addr in _TAILSCALE_CGNAT:
        return True
    for network in _load_allowed_cidrs():
        if addr in network:
            return True
    return False


@app.middleware("http")
async def allowlist_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if not _is_allowed_source(client_host):
        return JSONResponse(status_code=403, content={"detail": "Access denied: not from Tailnet"})
    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Shared Pipeline instance (used by both the web router and available to GUI)
# ---------------------------------------------------------------------------

_manager = ProcessManager()
_pipeline = Pipeline(_manager, lambda: load_state(), web._make_event_sink())
web.init(_pipeline, _manager)


# Mount the web API router
app.include_router(web.router, tags=["web"])



def backend_for_model(model: str) -> Backend:
    if model == "local":
        workflow = load_state()
        active = workflow["active"]
        if active["kind"] == "openai":
            api_key = get_openai_api_key()
            if not api_key:
                raise HTTPException(
                    status_code=503,
                    detail="Planner is active, but no OpenAI API key is stored in Model Deck.",
                )
            return Backend(
                alias="local",
                base_url=str(active["base_url"]).rstrip("/"),
                model_id=str(active["model_id"]),
                api_key=api_key,
                reasoning_effort=str(active.get("reasoning_effort") or "high"),
                provider="openai",
            )
        return Backend(
            alias="local",
            base_url=str(active["base_url"]).rstrip("/"),
            model_id=str(active["model_id"]),
        )
    if model == "planner":
        state = load_state()
        planner_cfg = state["planner"]
        if planner_cfg.get("kind") == "local":
            # Planner has its own role now (its own model/port/sampling),
            # rather than borrowing another phase's via "local_phase".
            role = state["roles"]["planner"]
            reasoning = str(role.get("reasoning") or "auto")
            return Backend(
                alias="planner",
                base_url=f"http://127.0.0.1:{int(role['port'])}/v1",
                model_id="planner",
                reasoning=reasoning,
                enable_thinking=(reasoning != "off"),
                temperature=role.get("temperature"),
                top_p=role.get("top_p"),
                top_k=role.get("top_k"),
                min_p=role.get("min_p"),
                presence_penalty=role.get("presence_penalty"),
                repetition_penalty=role.get("repetition_penalty"),
            )
        api_key = get_openai_api_key()
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail="No OpenAI API key is stored in Model Deck for the planner.",
            )
        return Backend(
            alias="planner",
            base_url=str(planner_cfg["base_url"]).rstrip("/"),
            model_id=str(planner_cfg["model"]),
            api_key=api_key,
            reasoning_effort=str(planner_cfg.get("reasoning_effort") or "high"),
            provider="openai",
        )
    if model in {"scout", "builder", "auditor", "renovator", "chat", "prompt_dev"}:
        role = load_state()["roles"][model]
        reasoning = str(role.get("reasoning") or "auto")
        return Backend(
            alias=model,
            base_url=f"http://127.0.0.1:{int(role['port'])}/v1",
            model_id=model,
            reasoning=reasoning,
            enable_thinking=(reasoning != "off"),
            temperature=role.get("temperature"),
            top_p=role.get("top_p"),
            top_k=role.get("top_k"),
            min_p=role.get("min_p"),
            presence_penalty=role.get("presence_penalty"),
            repetition_penalty=role.get("repetition_penalty"),
        )
    raise HTTPException(status_code=404, detail=f"Unknown model alias: {model}")


def auth_headers(backend: Backend) -> dict[str, str]:
    if not backend.api_key:
        return {}
    return {"Authorization": f"Bearer {backend.api_key}"}


def prepare_payload(payload: dict[str, Any], backend: Backend) -> dict[str, Any]:
    upstream_payload = dict(payload)
    upstream_payload["model"] = backend.model_id
    if backend.reasoning is not None:
        upstream_payload["reasoning"] = backend.reasoning
    if backend.enable_thinking is not None:
        upstream_payload["enable_thinking"] = backend.enable_thinking
    if backend.temperature is not None:
        upstream_payload["temperature"] = backend.temperature
    if backend.top_p is not None:
        upstream_payload["top_p"] = backend.top_p
    if backend.top_k is not None:
        upstream_payload["top_k"] = backend.top_k
    if backend.min_p is not None:
        upstream_payload["min_p"] = backend.min_p
    if backend.presence_penalty is not None:
        upstream_payload["presence_penalty"] = backend.presence_penalty
    if backend.repetition_penalty is not None:
        upstream_payload["repetition_penalty"] = backend.repetition_penalty
    if backend.provider == "openai":
        upstream_payload.pop("enable_thinking", None)
        upstream_payload.pop("reasoning", None)
        upstream_payload.pop("top_k", None)
        upstream_payload.pop("min_p", None)
        upstream_payload.pop("repetition_penalty", None)
        if "max_tokens" in upstream_payload:
            upstream_payload.setdefault(
                "max_completion_tokens", upstream_payload.pop("max_tokens")
            )
        if backend.reasoning_effort is not None:
            upstream_payload["reasoning_effort"] = backend.reasoning_effort
    return upstream_payload



def _preview_content(content: Any) -> tuple[Any, int | None]:
    if not isinstance(content, str):
        return content, None
    total = len(content)
    if total <= _CAPTURE_CONTENT_PREVIEW_CHARS:
        return content, total
    return content[:_CAPTURE_CONTENT_PREVIEW_CHARS] + f"...[truncated, {total} chars total]", total


def capture_incoming_request(payload: dict[str, Any], phase: str) -> None:
    try:
        messages = payload.get("messages") or []
        captured_messages = []
        for message in messages:
            content, chars = _preview_content(message.get("content"))
            captured_messages.append({"role": message.get("role"), "chars": chars, "content": content})

        tools = payload.get("tools") or []
        tools_json = json.dumps(tools)
        captured_tools = [
            {"name": (t.get("function") or {}).get("name") or t.get("name")}
            for t in tools
            if isinstance(t, dict)
        ]

        capture = {
            "phase": phase,
            "captured_at": time.time(),
            "model": payload.get("model"),
            "message_count": len(messages),
            "messages": captured_messages,
            "tool_count": len(tools),
            "tools_serialized_chars": len(tools_json),
            "tools": captured_tools,
            "tool_choice": payload.get("tool_choice"),
        }
        (RUN_DIR / "last-request.json").write_text(json.dumps(capture, indent=2))
    except Exception:
        pass


def wants_injection_skipped(request: Request) -> bool:
    return request.headers.get("x-modeldeck-skip-injection", "").lower() in ("1", "true")


def inject_phase_instructions(payload: dict[str, Any], phase: str) -> dict[str, Any]:
    text = effective_prompt(phase, load_state())
    if text is None:
        return payload
    instructions = f"Model Deck {phase} phase requirements:\n{text}"
    messages = list(payload.get("messages") or [])
    if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
        merged = dict(messages[0])
        merged["content"] = f"{merged['content']}\n\n---\n\n{instructions}"
        messages[0] = merged
    else:
        messages.insert(0, {"role": "system", "content": instructions})
    payload = dict(payload)
    payload["messages"] = messages
    return payload


def _feed_guard_from_sse_line(guard: Any, line: str) -> None:
    line = line.strip("\r")
    if not line.startswith("data: "):
        return
    data = line[len("data: "):]
    if data == "[DONE]":
        return
    try:
        event = json.loads(data)
    except ValueError:
        return
    choices = event.get("choices") or []
    if not choices:
        return
    content = (choices[0].get("delta") or {}).get("content")
    if content:
        loop_guard.feed(guard, content)


def upstream_error(backend: Backend, exc: httpx.RequestError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"{backend.alias} backend unavailable at {backend.base_url}: {exc}",
    )



@app.get("/health")
async def health() -> dict[str, Any]:
    workflow = load_state()
    return {
        "status": "ok",
        "active": workflow["active"],
        "models": ["local", settings.scout.alias, settings.builder.alias, "planner", "auditor", "renovator", "chat", "prompt_dev"],
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "local", "object": "model", "owned_by": "model-deck"},
            {"id": settings.scout.alias, "object": "model", "owned_by": "local"},
            {"id": settings.builder.alias, "object": "model", "owned_by": "local"},
            {"id": "planner", "object": "model", "owned_by": "model-deck"},
            {"id": "auditor", "object": "model", "owned_by": "local"},
            {"id": "renovator", "object": "model", "owned_by": "local"},
            {"id": "chat", "object": "model", "owned_by": "local"},
            {"id": "prompt_dev", "object": "model", "owned_by": "local"},
        ],
    }


@app.get("/loop-alert")
async def loop_alert() -> dict[str, Any]:
    return loop_guard.status()


@app.post("/loop-alert/stop")
async def loop_alert_stop(request: Request) -> dict[str, Any]:
    payload = await request.json()
    ok = loop_guard.request_stop(int(payload["id"]))
    return {"ok": ok}


@app.post("/loop-alert/ack")
async def loop_alert_ack(request: Request) -> dict[str, Any]:
    payload = await request.json()
    ok = loop_guard.ack(int(payload["id"]), int(payload["seq"]))
    return {"ok": ok}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    requested_model = payload.get("model")
    if not requested_model:
        raise HTTPException(status_code=400, detail="Request must include model")

    backend = backend_for_model(requested_model)
    phase = load_state()["active"]["phase"] if requested_model == "local" else str(requested_model)
    capture_incoming_request(payload, phase)
    if not wants_injection_skipped(request):
        payload = inject_phase_instructions(payload, phase)
    upstream_payload = prepare_payload(payload, backend)

    stream = bool(upstream_payload.get("stream", False))
    url = f"{backend.base_url}/chat/completions"

    if stream:
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            "POST", url, json=upstream_payload, headers=auth_headers(backend)
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            raise upstream_error(backend, exc) from exc

        if upstream.status_code >= 400:
            body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(
                status_code=upstream.status_code,
                content={"error": body.decode(errors="replace")},
            )

        guard = loop_guard.start_stream(phase)

        async def iterator():
            line_buffer = ""
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    line_buffer += chunk.decode("utf-8", errors="replace")
                    *lines, line_buffer = line_buffer.split("\n")
                    for line in lines:
                        _feed_guard_from_sse_line(guard, line)
                    if guard.stop_requested:
                        break
            finally:
                await upstream.aclose()
                await client.aclose()
            if guard.stop_requested:
                stop_chunk = {
                    "id": "loop-guard-stop",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"

        return StreamingResponse(iterator(), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                url,
                json=upstream_payload,
                headers=auth_headers(backend),
            )
    except httpx.RequestError as exc:
        raise upstream_error(backend, exc) from exc

    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text}

    return JSONResponse(status_code=response.status_code, content=body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("router.main:app", host=settings.host, port=settings.port, reload=False)

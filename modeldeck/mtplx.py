from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUN_DIR = PROJECT_DIR / ".run"
DEFAULT_MTPLX_BIN = Path.home() / ".mtplx" / "bin" / "mtplx"


def fetch_json(url: str, timeout: float = 0.6) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def fetch_sse_snapshot(url: str, timeout: float = 0.6) -> dict[str, Any]:
    """One-shot read of the first event from an SSE endpoint, then closes
    the connection -- for /v1/mtplx/metrics/stream, whose events are each a
    full self-contained snapshot (not a delta), so there's no need to hold
    the connection open like a real subscriber would. This is the only
    channel that carries live per-chunk prefill progress (in_flight[].
    prefill_state: tokens_done/tokens_total/elapsed_s) -- confirmed
    empirically against mtplx's own app, which reads this same data for
    its live "prefill tps / ETA" gauge. /v1/mtplx/flight (used everywhere
    else in this module) never carries it at all, at any prompt size."""
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                try:
                    value = json.loads(line[len("data:"):].strip())
                except ValueError:
                    return {}
                return value if isinstance(value, dict) else {}
    return {}


def post_json(url: str, body: dict[str, Any], timeout: float = 0.6) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def healthy(port: int) -> bool:
    try:
        return bool(fetch_json(f"http://127.0.0.1:{port}/health").get("ok"))
    except (OSError, urllib.error.URLError, ValueError):
        return False


def router_healthy(port: int = 8100) -> bool:
    try:
        return fetch_json(f"http://127.0.0.1:{port}/health").get("status") == "ok"
    except (OSError, urllib.error.URLError, ValueError):
        return False


def listener_pid(port: int) -> int | None:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def installed_models(mtplx_bin: Path = DEFAULT_MTPLX_BIN) -> list[dict[str, Any]]:
    result = subprocess.run(
        [str(mtplx_bin), "models", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    return [
        item
        for item in payload.get("models", [])
        if item.get("has_runtime_contract") and item.get("validation", {}).get("ok")
    ]


def model_command(
    phase: str,
    role: dict[str, Any],
    mtplx_bin: Path = DEFAULT_MTPLX_BIN,
) -> tuple[list[str], dict[str, str]]:
    command = [
        str(mtplx_bin),
        "serve",
        "--model",
        str(role["model"]),
        "--model-id",
        phase,
        "--host",
        "127.0.0.1",
        "--port",
        str(role["port"]),
        "--profile",
        str(role["profile"]),
        "--depth",
        str(role["depth"]),
        "--context-window",
        str(role["context_window"]),
        "--paged-kv-quantization",
        str(role["kv_quantization"]),
        "--ssd-session-cache",
        str(role.get("ssd_session_cache", "off")),
        "--ssd-session-cache-max-size",
        str(role["ssd_cache"]),
        "--ssd-session-cache-min-prefix-tokens",
        "512",
        "--reasoning",
        str(role["reasoning"]),
        "--reasoning-effort",
        str(role["reasoning_effort"]),
        "--preserve-thinking",
        str(role["preserve_thinking"]),
        "--scheduler-mode",
        "serial",
        "--batching-preset",
        "latency",
        "--prefill-chunk-tokens",
        str(role["prefill_chunk_tokens"]),
        "--fan-mode",
        str(role["fan_mode"]),
        "--no-auth",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "MTPLX_SESSION_BANK_MAX_BYTES": str(role["ram_cache_total"]),
            "MTPLX_SESSION_BANK_PER_SESSION_BYTES": str(
                role["ram_cache_per_session"]
            ),
            "MTPLX_SESSION_BANK_MAX_ENTRIES": str(role["ram_cache_entries"]),
        }
    )
    return command, environment


class ProcessManager:
    def __init__(self, mtplx_bin: Path = DEFAULT_MTPLX_BIN):
        self.mtplx_bin = mtplx_bin
        RUN_DIR.mkdir(parents=True, exist_ok=True)

    def stop_local_models(self, ports: list[int]) -> None:
        for port in ports:
            pid = listener_pid(port)
            if pid is None:
                continue
            owned = self._owned_pid_for_port(port)
            if owned != pid:
                raise RuntimeError(
                    f"Port {port} is owned by PID {pid}, not by Model Deck; "
                    "stop it manually before switching."
                )
            os.kill(pid, signal.SIGTERM)
            self._wait_stopped(port)
            pid_path = RUN_DIR / f"{self._phase_for_port(port)}.pid"
            if pid_path.exists():
                pid_path.unlink()

    def launch(self, phase: str, role: dict[str, Any]) -> int:
        port = int(role["port"])
        if healthy(port):
            pid = listener_pid(port)
            if pid is None:
                raise RuntimeError(f"{phase} is healthy but its PID is unavailable")
            return pid
        command, environment = model_command(phase, role, self.mtplx_bin)
        log_path = RUN_DIR / f"{phase}.log"
        log_handle = log_path.open("a")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        for _ in range(180):
            if healthy(port):
                pid = listener_pid(port)
                if pid is None:
                    break
                (RUN_DIR / f"{phase}.pid").write_text(f"{pid}\n")
                return pid
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"{phase} exited with status {return_code}; see {log_path}"
                )
            time.sleep(0.5)
        raise RuntimeError(f"{phase} did not become ready; see {log_path}")

    def ensure_router(self) -> int:
        if router_healthy(8100):
            return listener_pid(8100) or 0
        python = PROJECT_DIR / ".venv" / "bin" / "python"
        log_handle = (RUN_DIR / "router.log").open("a")
        subprocess.Popen(
            [str(python), "-m", "router.main"],
            cwd=PROJECT_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        for _ in range(40):
            try:
                fetch_json("http://127.0.0.1:8100/health")
                pid = listener_pid(8100) or 0
                if pid:
                    (RUN_DIR / "router.pid").write_text(f"{pid}\n")
                return pid
            except (OSError, urllib.error.URLError, ValueError):
                time.sleep(0.25)
        raise RuntimeError("Router did not become ready")

    def _owned_pid_for_port(self, port: int) -> int | None:
        phase = self._phase_for_port(port)
        path = RUN_DIR / f"{phase}.pid"
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _phase_for_port(port: int) -> str:
        return {
            8000: "scout", 8002: "builder", 8004: "auditor",
            8006: "renovator", 8008: "planner", 8010: "chat",
        }.get(
            port, f"model-{port}"
        )

    @staticmethod
    def _wait_stopped(port: int) -> None:
        for _ in range(60):
            if listener_pid(port) is None:
                return
            time.sleep(0.25)
        raise RuntimeError(f"Model on port {port} did not stop")

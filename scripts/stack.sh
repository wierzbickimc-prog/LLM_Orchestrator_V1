#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$PROJECT_DIR/.run"
MTPLX_BIN="${MTPLX_BIN:-$HOME/.mtplx/bin/mtplx}"
SCOUT_PORT="${SCOUT_PORT:-8000}"
BUILDER_PORT="${BUILDER_PORT:-8002}"
ROUTER_PORT="${ROUTER_PORT:-8100}"
SCOUT_MODEL="${SCOUT_MODEL:-Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Speed-FP16}"
BUILDER_MODEL="${BUILDER_MODEL:-Youssofal/Qwen3.8-27B-MTPLX-Optimized-Speed-FP16}"
SCOUT_CONTEXT_WINDOW="${SCOUT_CONTEXT_WINDOW:-100000}"
BUILDER_CONTEXT_WINDOW="${BUILDER_CONTEXT_WINDOW:-100000}"
SCOUT_KV_QUANTIZATION="${SCOUT_KV_QUANTIZATION:-q8}"
BUILDER_KV_QUANTIZATION="${BUILDER_KV_QUANTIZATION:-q8}"
# 4G per-session held ~61k tokens of KV at the default 65536 B/token density
# (4e9 / 65536), just short of the 100k context window -- long scout/builder
# runs would blow past it, evict, and lose the cache. 12G covers the full
# window with headroom (see modeldeck/state.py, same fix applied there).
SCOUT_SESSION_BANK_MAX="${SCOUT_SESSION_BANK_MAX:-16G}"
BUILDER_SESSION_BANK_MAX="${BUILDER_SESSION_BANK_MAX:-16G}"
SCOUT_SESSION_BANK_PER_SESSION_MAX="${SCOUT_SESSION_BANK_PER_SESSION_MAX:-12G}"
BUILDER_SESSION_BANK_PER_SESSION_MAX="${BUILDER_SESSION_BANK_PER_SESSION_MAX:-12G}"
SCOUT_SSD_SESSION_CACHE_MAX="${SCOUT_SSD_SESSION_CACHE_MAX:-10G}"
BUILDER_SSD_SESSION_CACHE_MAX="${BUILDER_SSD_SESSION_CACHE_MAX:-10G}"
SCOUT_DEPTH="${SCOUT_DEPTH:-3}"
BUILDER_DEPTH="${BUILDER_DEPTH:-3}"
FAN_MODE="${FAN_MODE:-smart}"
PREFILL_CHUNK_TOKENS="${PREFILL_CHUNK_TOKENS:-2048}"
SESSION_BANK_MAX_ENTRIES="${SESSION_BANK_MAX_ENTRIES:-8}"

healthy() {
  curl --max-time 2 --silent --fail "http://127.0.0.1:$1/health" >/dev/null 2>&1
}

wait_for_health() {
  local name="$1" port="$2"
  for _ in {1..90}; do
    if healthy "$port"; then
      echo "$name ready on port $port"
      return 0
    fi
    sleep 1
  done
  echo "$name did not become healthy; see $RUN_DIR/$name.log" >&2
  return 1
}

start_model() {
  local name="$1" model="$2" model_id="$3" port="$4" profile="$5"
  local context_window="$6" kv_quantization="$7" session_bank_max="$8"
  local per_session_max="$9" ssd_cache_max="${10}" depth="${11}"
  local reasoning="${12}" reasoning_effort="${13}"
  if healthy "$port"; then
    echo "$name already running on port $port"
    return
  fi
  nohup env \
    MTPLX_SESSION_BANK_MAX_BYTES="$session_bank_max" \
    MTPLX_SESSION_BANK_PER_SESSION_BYTES="$per_session_max" \
    MTPLX_SESSION_BANK_MAX_ENTRIES="$SESSION_BANK_MAX_ENTRIES" \
    "$MTPLX_BIN" serve \
    --model "$model" \
    --model-id "$model_id" \
    --host 127.0.0.1 \
    --port "$port" \
    --profile "$profile" \
    --depth "$depth" \
    --context-window "$context_window" \
    --paged-kv-quantization "$kv_quantization" \
    --ssd-session-cache on \
    --ssd-session-cache-max-size "$ssd_cache_max" \
    --ssd-session-cache-min-prefix-tokens 512 \
    --reasoning "$reasoning" \
    --reasoning-effort "$reasoning_effort" \
    --preserve-thinking auto \
    --scheduler-mode serial \
    --batching-preset latency \
    --prefill-chunk-tokens "$PREFILL_CHUNK_TOKENS" \
    --fan-mode "$FAN_MODE" \
    --no-auth \
    >"$RUN_DIR/$name.log" 2>&1 &
  local launcher_pid="$!"
  wait_for_health "$name" "$port"
  local listener_pid
  listener_pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t | head -n 1)"
  echo "${listener_pid:-$launcher_pid}" >"$RUN_DIR/$name.pid"
}

stop_owned() {
  local name="$1" pid_file="$RUN_DIR/$1.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name was not started by this script"
    return
  fi
  local pid
  pid="$(<"$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "stopped $name (pid $pid)"
  fi
  unlink "$pid_file"
}

wait_for_stop() {
  local name="$1" port="$2"
  for _ in {1..40}; do
    if ! healthy "$port"; then
      return 0
    fi
    sleep 0.25
  done
  echo "$name did not stop on port $port" >&2
  return 1
}

status() {
  local name port
  for name in scout builder router; do
    case "$name" in
      scout) port="$SCOUT_PORT" ;;
      builder) port="$BUILDER_PORT" ;;
      router) port="$ROUTER_PORT" ;;
    esac
    if healthy "$port"; then
      echo "$name: ready (http://127.0.0.1:$port)"
    else
      echo "$name: stopped (port $port)"
    fi
  done
}

start_router() {
  mkdir -p "$RUN_DIR"
  if healthy "$ROUTER_PORT"; then
    echo "router already running on port $ROUTER_PORT"
  else
    if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
      echo "Missing .venv; run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
      exit 1
    fi
    (
      cd "$PROJECT_DIR"
      nohup .venv/bin/python -m router.main >"$RUN_DIR/router.log" 2>&1 &
      echo "$!" >"$RUN_DIR/router.pid"
    )
    wait_for_health router "$ROUTER_PORT"
  fi
}

start_role() {
  local role="$1" other other_port
  mkdir -p "$RUN_DIR"
  if [[ ! -x "$MTPLX_BIN" ]]; then
    echo "MTPLX CLI not found at $MTPLX_BIN" >&2
    exit 1
  fi

  if [[ "$role" == "scout" ]]; then
    other="builder"
    other_port="$BUILDER_PORT"
  else
    other="scout"
    other_port="$SCOUT_PORT"
  fi
  if healthy "$other_port"; then
    echo "$other is still running on port $other_port; stop it before launching $role" >&2
    exit 1
  fi

  if [[ "$role" == "scout" ]]; then
    start_model scout "$SCOUT_MODEL" scout \
      "$SCOUT_PORT" turbo "$SCOUT_CONTEXT_WINDOW" "$SCOUT_KV_QUANTIZATION" \
      "$SCOUT_SESSION_BANK_MAX" "$SCOUT_SESSION_BANK_PER_SESSION_MAX" \
      "$SCOUT_SSD_SESSION_CACHE_MAX" "$SCOUT_DEPTH" auto medium
  else
    start_model builder "$BUILDER_MODEL" builder \
      "$BUILDER_PORT" turbo "$BUILDER_CONTEXT_WINDOW" "$BUILDER_KV_QUANTIZATION" \
      "$BUILDER_SESSION_BANK_MAX" "$BUILDER_SESSION_BANK_PER_SESSION_MAX" \
      "$BUILDER_SSD_SESSION_CACHE_MAX" "$BUILDER_DEPTH" auto medium
  fi

  start_router
  status
}

switch_role() {
  local role="$1" other other_port
  if [[ "$role" == "scout" ]]; then
    other="builder"
    other_port="$BUILDER_PORT"
  else
    other="scout"
    other_port="$SCOUT_PORT"
  fi

  if healthy "$other_port"; then
    if [[ ! -f "$RUN_DIR/$other.pid" ]]; then
      echo "$other is running but is not owned by this launcher; stop it manually" >&2
      exit 1
    fi
    stop_owned "$other"
    wait_for_stop "$other" "$other_port"
  fi
  start_role "$role"
}

case "${1:-status}" in
  start)
    case "${2:-}" in
      scout|builder) start_role "$2" ;;
      router) start_router; status ;;
      *) echo "Usage: $0 start {scout|builder|router}" >&2; exit 2 ;;
    esac
    ;;
  switch)
    case "${2:-}" in
      scout|builder) switch_role "$2" ;;
      *) echo "Usage: $0 switch {scout|builder}" >&2; exit 2 ;;
    esac
    ;;
  stop)
    case "${2:-all}" in
      scout|builder|router) stop_owned "$2" ;;
      all)
        stop_owned router
        stop_owned builder
        stop_owned scout
        ;;
      *) echo "Usage: $0 stop [scout|builder|router|all]" >&2; exit 2 ;;
    esac
    ;;
  status) status ;;
  logs)
    tail -n 100 "$RUN_DIR"/*.log
    ;;
  *) echo "Usage: $0 {start {scout|builder|router}|switch {scout|builder}|stop [role]|status|logs}" >&2; exit 2 ;;
esac

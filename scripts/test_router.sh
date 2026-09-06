#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8100/v1}"
MODEL="${MODEL:-scout}"
MAX_TOKENS="${MAX_TOKENS:-128}"

curl -sS "$BASE_URL/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: router works\"}],\"temperature\":0,\"max_tokens\":$MAX_TOKENS}" \
  | python3 -c 'import json, sys
data = json.load(sys.stdin)
choice = data["choices"][0]
print(json.dumps({
    "model": data.get("model"),
    "message": choice.get("message"),
    "finish_reason": choice.get("finish_reason"),
    "usage": data.get("usage"),
}, indent=2))'

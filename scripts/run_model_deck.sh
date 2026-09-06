#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Model Deck environment is missing. Run scripts/setup_model_deck.sh first." >&2
  exit 1
fi

cd "$PROJECT_DIR"
exec "$PYTHON" -m modeldeck.gui

#!/bin/zsh
PROJECT_DIR="/Users/michaelwierzbicki/Downloads/local-cline-router"
mkdir -p "$PROJECT_DIR/.run"
cd "$PROJECT_DIR" || exit 1
nohup "$PROJECT_DIR/.venv/bin/python" -m modeldeck.gui >>"$PROJECT_DIR/.run/model-deck-gui.log" 2>&1 </dev/null &
exit

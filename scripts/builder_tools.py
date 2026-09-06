"""The three actions builder_agent.py can take, and the parsing/safety
around them.

Deliberately not the OpenAI `tools`/function-calling field -- see
report_common.stream_chat's docstring for why. Instead the model is told
(in the system prompt) to emit exactly one fenced ```tool block containing
a JSON object per turn to act, or plain text with no such block to finish.
We parse that block ourselves here, with our own error recovery when it's
malformed, rather than depending on mtplx's tool-prompt bridge."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL)

# Long command output eats context fast (e.g. a verbose test run) without
# adding much signal past a point -- keep enough to see the failure, drop
# the rest with a clear note instead of silently truncating.
MAX_COMMAND_OUTPUT_CHARS = 8_000

VALID_TOOLS = {"read_file", "write_file", "run_command", "append_file"}


class ToolCallParseError(Exception):
    """The model's response didn't contain a valid tool call. Callers feed
    this back to the model as an error to correct, rather than crashing --
    the whole point of parsing this ourselves is to recover from exactly
    this instead of stalling the way mtplx's bridge did on malformed XML."""


def extract_tool_call(response_text: str) -> dict | None:
    """Returns the parsed {"name": ..., "arguments": {...}} dict, or None if
    the response has no ```tool block at all (meaning the model is done).
    Raises ToolCallParseError if a block exists but is malformed -- including
    the case where a ```tool block was opened but never closed, which means
    the model's own completion-token limit cut it off mid-write rather than
    the model actually finishing. Without this distinction, a truncated large
    write_file call (no tools block, no closing fence) looks identical to
    "no tool call at all" and gets mistaken for the model signaling it's
    done -- the write never runs, and the garbled cutoff gets saved as if it
    were the final report."""
    match = TOOL_BLOCK_RE.search(response_text)
    if match is None:
        if "```tool" in response_text:
            raise ToolCallParseError(
                "Your last response started a ```tool block but never closed it -- "
                "it looks like the content was too large and got cut off before "
                "finishing, not that you chose to stop. Split large file writes "
                "across a write_file call for the first part plus one or more "
                "append_file calls for the rest, each small enough to fit in a "
                "single response."
            )
        return None
    raw = match.group(1).strip()
    try:
        call = json.loads(raw)
    except ValueError as exc:
        raise ToolCallParseError(f"```tool block is not valid JSON: {exc}") from exc
    if not isinstance(call, dict) or "name" not in call or "arguments" not in call:
        raise ToolCallParseError('```tool block must be {"name": ..., "arguments": {...}}')
    if call["name"] not in VALID_TOOLS:
        raise ToolCallParseError(f"Unknown tool {call['name']!r}. Valid tools: {sorted(VALID_TOOLS)}")
    if not isinstance(call["arguments"], dict):
        raise ToolCallParseError('"arguments" must be a JSON object')
    return call


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"Path {relative!r} escapes the project root ({root})")
    return candidate


def run_tool(call: dict, root: Path, command_timeout: float, dry_run: bool) -> str:
    """Executes one parsed tool call and returns the text to feed back to
    the model as the result. Never raises for expected failures (bad path,
    missing file, failed command) -- those come back as descriptive result
    text instead, so the model can react to them like a normal tool result."""
    name = call["name"]
    args = call["arguments"]

    if name == "read_file":
        try:
            path = _safe_path(root, str(args.get("path", "")))
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            return path.read_text()
        except OSError as exc:
            return f"Error reading {args.get('path')}: {exc}"

    if name == "write_file":
        try:
            path = _safe_path(root, str(args.get("path", "")))
        except ValueError as exc:
            return f"Error: {exc}"
        content = args.get("content")
        if not isinstance(content, str):
            return 'Error: "content" must be a string (the full new file content)'
        if dry_run:
            return f"[dry-run] Would write {len(content)} chars to {args.get('path')}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Wrote {len(content)} chars to {args.get('path')}"

    if name == "append_file":
        try:
            path = _safe_path(root, str(args.get("path", "")))
        except ValueError as exc:
            return f"Error: {exc}"
        content = args.get("content")
        if not isinstance(content, str):
            return 'Error: "content" must be a string'
        if dry_run:
            return f"[dry-run] Would append {len(content)} chars to {args.get('path')}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(content)
        return f"Appended {len(content)} chars to {args.get('path')} (now {path.stat().st_size} bytes)"

    if name == "run_command":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return 'Error: "command" must be a non-empty string'
        if dry_run:
            return f"[dry-run] Would run: {command}"
        try:
            result = subprocess.run(
                command, shell=True, cwd=root, capture_output=True, text=True,
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {command_timeout}s: {command}"
        output = (result.stdout or "") + (result.stderr or "")
        if len(output) > MAX_COMMAND_OUTPUT_CHARS:
            output = output[:MAX_COMMAND_OUTPUT_CHARS] + f"\n...[truncated, {len(output)} chars total]"
        return f"Exit code {result.returncode}\n{output}"

    raise AssertionError(f"unreachable: unvalidated tool name {name!r}")

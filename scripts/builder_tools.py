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

# Recovery for a tool-call-shaped JSON object that appears with its ```tool
# fence stripped entirely (not truncated -- just never fenced), observed in
# a live run: the response was exactly `tool\n{"name": "read_file", ...}`,
# no backticks anywhere, and the model's own next turn said "I see my tool
# blocks aren't being registered." Without this, extract_tool_call finds no
# fence, sees no "```tool" substring either, and returns None -- read as
# "the model is done" -- ending the session after zero real work with this
# stray text saved as the final report.
_BARE_TOOL_JSON_RE = re.compile(r'\{\s*"name"\s*:')
_OPEN_FENCE_RE = re.compile(r"```tool\s*\n")

# Long command output eats context fast (e.g. a verbose test run) without
# adding much signal past a point -- keep enough to see the failure, drop
# the rest with a clear note instead of silently truncating.
MAX_COMMAND_OUTPUT_CHARS = 8_000

VALID_TOOLS = {"read_file", "write_file", "run_command", "append_file", "ask_question"}
# ask_question is intercepted in builder_agent.run_agent before it ever
# reaches run_tool() below -- answering it means blocking on real input,
# which run_tool has no way to do (it's a pure request/response function
# with no access to the agent loop's on_chunk or stdin handling).

# Same five actions as the ```tool convention above, expressed as OpenAI
# function-calling schema -- for the native-tool-calling path (see
# report_common.stream_chat_native), used only for roles configured with
# native_tool_calling=True. Kept as one hand-written source next to
# VALID_TOOLS/run_tool rather than generated, since there are only five and
# generation would just move the same information one layer further from
# where it's actually consumed.
OPENAI_TOOL_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file at a path relative to the project root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative file path"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace the entire contents of a file at a path relative to the project root, creating it if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "The full new file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append content to the end of a file at a path relative to the project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Content to add to the end"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the project root and return its combined stdout/stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command, e.g. pytest"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_question",
            "description": (
                "Pause and ask the person running this session a question. Use sparingly -- only "
                "when the plan is genuinely silent on a decision AND guessing wrong would be costly "
                "or hard to undo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The decision you need"},
                    "options": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Optional fixed choices; omit for a free-text answer",
                    },
                },
                "required": ["question"],
            },
        },
    },
]


class ToolCallParseError(Exception):
    """The model's response didn't contain a valid tool call. Callers feed
    this back to the model as an error to correct, rather than crashing --
    the whole point of parsing this ourselves is to recover from exactly
    this instead of stalling the way mtplx's bridge did on malformed XML."""


def _try_bare_json_tool_call(response_text: str) -> dict | None:
    """Looks for a tool-call-shaped JSON object with no ```tool fence around
    it at all -- see _BARE_TOOL_JSON_RE's comment for why this exists. Uses
    a real JSON decoder positioned at the first plausible start rather than
    a brace-matching regex, so it isn't confused by braces inside string
    values (e.g. write_file's own "content"). Returns None (not an error)
    for anything that doesn't cleanly decode into a valid call -- this is a
    best-effort recovery, not a replacement for the real fence."""
    match = _BARE_TOOL_JSON_RE.search(response_text)
    if match is None:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(response_text, match.start())
    except ValueError:
        return None
    if not isinstance(obj, dict) or "name" not in obj or "arguments" not in obj:
        return None
    if obj["name"] not in VALID_TOOLS or not isinstance(obj["arguments"], dict):
        return None
    return obj


def _try_complete_json_after_open_fence(response_text: str) -> dict | None:
    """A ```tool fence was opened but TOOL_BLOCK_RE found no closing fence --
    before assuming the content was truncated (cut off by the completion-
    token limit), check whether the JSON object right after the opening
    marker is actually already complete. Observed in a live run: five
    consecutive short, well-formed tool calls (e.g. a single read_file,
    maybe 60 characters of JSON) each missing only their closing ``` despite
    being nowhere near large enough to be genuinely truncated -- a chat-
    template/bridge quirk that strips the closing fence, not a length
    problem. JSON is self-delimiting, so the closing fence was never
    actually needed to know where the object ends; a real JSONDecodeError
    here (content that doesn't parse even without a trailing fence) still
    means true truncation, and falls through to that error as before."""
    open_match = _OPEN_FENCE_RE.search(response_text)
    if open_match is None:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(response_text, open_match.end())
    except ValueError:
        return None
    if not isinstance(obj, dict) or "name" not in obj or "arguments" not in obj:
        return None
    if obj["name"] not in VALID_TOOLS or not isinstance(obj["arguments"], dict):
        return None
    return obj


def extract_tool_call(response_text: str) -> dict | None:
    """Returns the parsed {"name": ..., "arguments": {...}} dict, or None if
    the response has no ```tool block at all (meaning the model is done).

    An opened-but-unclosed fence is recovered directly when the JSON right
    after the opening marker is already complete (see
    _try_complete_json_after_open_fence -- observed live: short, well-formed
    calls missing only their closing ``` from what looks like a chat-
    template/bridge quirk, not actual truncation). Only when that JSON is
    itself unparseable does this raise ToolCallParseError for a genuine
    truncation (the model's own completion-token limit cutting off a large
    write_file mid-write) -- without that distinction, a truncated call
    looks identical to "no tool call at all" and gets mistaken for the model
    signaling it's done, so the write never runs and the garbled cutoff gets
    saved as if it were the final report."""
    match = TOOL_BLOCK_RE.search(response_text)
    if match is None:
        if "```tool" in response_text:
            recovered = _try_complete_json_after_open_fence(response_text)
            if recovered is not None:
                return recovered
            raise ToolCallParseError(
                "Your last response started a ```tool block but never closed it -- "
                "it looks like the content was too large and got cut off before "
                "finishing, not that you chose to stop. Split large file writes "
                "across a write_file call for the first part plus one or more "
                "append_file calls for the rest, each small enough to fit in a "
                "single response."
            )
        bare = _try_bare_json_tool_call(response_text)
        if bare is not None:
            return bare
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


# Xcode's project.pbxproj is a serialized property list tracking every file
# reference, target, and build phase -- fragile even between two human
# engineers, and write_file's only primitive is "replace the whole file", so
# one bad regeneration can silently break the project's buildability with no
# diff an Auditor pass could meaningfully review. ".build"/".swiftpm" are
# generated Swift Package Manager state, never something to hand-edit either.
# Xcode's bundles are always named "<Project>.xcodeproj"/"<Project>.
# xcworkspace" -- a suffix, not a literal directory name like the other two.
_PROTECTED_BUNDLE_NAMES = (".build", ".swiftpm")
_PROTECTED_BUNDLE_SUFFIXES = (".xcodeproj", ".xcworkspace")


def _reject_protected_bundle_write(path: Path) -> str | None:
    """Returns an error message if path falls inside a protected bundle/build
    directory, else None. Checked only for write_file/append_file -- reading
    is harmless and sometimes legitimately useful (e.g. inspecting
    Package.resolved)."""
    for part in path.parts:
        if part in _PROTECTED_BUNDLE_NAMES or part.endswith(_PROTECTED_BUNDLE_SUFFIXES):
            return (
                f"Error: refusing to write inside {part} -- this is generated/managed "
                "project state, not source to hand-edit. If a new file needs to be part "
                "of the build, add it under Sources/<Target>/ (or the project's existing "
                "source layout) instead; Xcode/SPM pick up new files there automatically "
                "without any project-file edit."
            )
    return None


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
        blocked = _reject_protected_bundle_write(path)
        if blocked is not None:
            return blocked
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
        blocked = _reject_protected_bundle_write(path)
        if blocked is not None:
            return blocked
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

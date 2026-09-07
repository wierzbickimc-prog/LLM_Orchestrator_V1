"""Shared engine behind scripts/{scout,planner,auditor}_report.py.

Each of those is a thin, phase-specific wrapper: they all do the same three
things (collect file contents, ask the model for one written report, save
it) with no tool-calling and no agentic loop -- see scout_report.py's module
docstring for why that shape was chosen.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_ROUTER_URL = "http://127.0.0.1:8100/v1"

# Enough headroom under the 100k context window for the model's own report
# generation, after the task + file contents + system prompt. ~260k chars is
# roughly 65-70k tokens for code-heavy text, leaving 30k+ for the response.
DEFAULT_CHAR_BUDGET = 260_000

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "dist", "build", ".ai", ".run", "site-packages", ".idea",
    ".vscode",
    # Swift Package Manager: ".build" holds compiled artifacts and checked-
    # out dependency sources (can be large), ".swiftpm" holds local package
    # resolution state -- neither is source a phase should read or write.
    # Xcode's own project bundles (always named "<Project>.xcodeproj" /
    # "<Project>.xcworkspace", so a suffix, not a literal dir name like the
    # two above -- handled separately in collect_files) are deliberately
    # not source either; see docs/IMPROVEMENTS_TODO.md for why an agent
    # should never write_file into project.pbxproj directly.
    ".build", ".swiftpm",
}

XCODE_BUNDLE_SUFFIXES = (".xcodeproj", ".xcworkspace")

DEFAULT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md",
    ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
    # Swift Package Manager source -- Package.swift is itself a ".swift" file,
    # so no separate manifest extension is needed.
    ".swift", ".h", ".m", ".mm", ".plist", ".entitlements", ".xcconfig",
}

# Some chat templates (seen on the "tokenizer" profile, e.g. Builder) default
# toward attempting a tool call even when no `tools` field is sent at all --
# not just when one's declared. mtplx intercepts the malformed attempt and
# injects a bracketed [MTPLX: ...] notice into the assistant's own output
# instead of real content, so include this bluntly in every script's system
# prompt, and looks_like_failed_tool_call below still catches it if a
# particular chat template ignores the warning anyway.
NO_TOOLS_NOTICE = (
    "You have no tools in this conversation -- not read_file, not list_files, "
    "not run_command, nothing. Every file you need is already given to you in "
    "full below. If you find yourself about to emit anything that looks like "
    "a tool call, stop: it will not execute, and attempting it wastes the "
    "whole response. Just write your answer as plain text using only what is "
    "given below."
)

_TOOL_CALL_FAILURE_MARKERS = ("[MTPLX:", "<tool_call>", "</tool_call>")


def looks_like_failed_tool_call(text: str) -> bool:
    return any(marker in text for marker in _TOOL_CALL_FAILURE_MARKERS)


class ReportError(Exception):
    """Raised for expected, user-facing failures (missing artifact, no
    matching files, unreachable router) -- callers print .args[0] and exit 1
    rather than showing a traceback."""


def resolve_ai_path(target: Path, filename: str) -> Path:
    """Anchor a .ai/<filename> artifact to the project being worked on (the
    target path passed on the command line), not to wherever this tool
    happens to be installed or invoked from."""
    base = target if target.is_dir() else target.parent
    return base / ".ai" / filename


def collect_files(root: Path, extensions: set[str]) -> list[Path]:
    if root.is_file():
        return [root]
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if any(part.endswith(XCODE_BUNDLE_SUFFIXES) for part in path.parts):
            continue
        if path.suffix not in extensions:
            continue
        files.append(path)
    return files


_PATH_LIKE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z]{1,10}")


def referenced_files(report_text: str, root: Path) -> list[Path]:
    """Pull out the specific files a prior phase's report actually names
    (e.g. "`web/app.js`" or "serve.py:42"), resolved against root and
    filtered to files that really exist. This is how a downstream phase
    (Planner reading Scout's report, Auditor reading the plan) verifies
    exactly what was cited instead of blanket re-ingesting the whole tree --
    doing that defeats the point of the earlier phase being a cheaper
    summarizer, since the next one just reprocesses everything from scratch
    at the same cost anyway, usually on a slower model."""
    base = root if root.is_dir() else root.parent
    seen: set[Path] = set()
    found: list[Path] = []
    for match in _PATH_LIKE_RE.findall(report_text):
        candidate = match.strip("`'\",.:;()[]{}").split(":")[0]
        if not candidate:
            continue
        direct = base / candidate
        actual: Path | None = None
        if direct.is_file():
            actual = direct
        else:
            name = Path(candidate).name
            matches = [p for p in base.rglob(name) if p.is_file()] if name else []
            if len(matches) == 1:
                actual = matches[0]
        if actual is None:
            continue
        # Dedup on the resolved (absolute, symlink-free) identity, but keep
        # the original path spelling for display/reading -- resolving would
        # otherwise turn a relative root into ugly, inconsistent absolute
        # paths in the model context and printed output.
        identity = actual.resolve()
        if identity not in seen:
            seen.add(identity)
            found.append(actual)
    return sorted(found)


def _file_cache_path(cache_target: Path) -> Path:
    return resolve_ai_path(cache_target, ".file-cache.json")


def _load_file_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_file_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    except OSError:
        pass  # caching is a pure speed optimization; never fail a run over it


def build_context(
    files: list[Path], char_budget: int, *, cache_target: Path | None = None,
) -> tuple[str, list[Path], list[Path]]:
    """Reads each file's content through an on-disk cache keyed by (path,
    mtime, size) when cache_target is given -- pass the same target path
    used for resolve_ai_path so the cache lands next to that run's other
    .ai/ artifacts. Scout, Planner (on its full-scan fallback), and Auditor
    each build their own context independently, often over overlapping file
    sets, within one pipeline run; without this they re-read and re-buffer
    every file from scratch each time even when nothing changed. A file
    Builder actually edited gets a new mtime and is read fresh; everything
    else is served from cache."""
    cache_file = _file_cache_path(cache_target) if cache_target is not None else None
    cache = _load_file_cache(cache_file) if cache_file is not None else {}
    cache_dirty = False

    included: list[Path] = []
    skipped: list[Path] = []
    chunks: list[str] = []
    remaining = char_budget
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            skipped.append(path)
            continue
        key = str(path.resolve())
        entry = cache.get(key)
        if entry and entry.get("mtime_ns") == stat.st_mtime_ns and entry.get("size") == stat.st_size:
            text = entry["content"]
        else:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                skipped.append(path)
                continue
            if cache_file is not None:
                cache[key] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "content": text}
                cache_dirty = True
        block = f"=== {path} ===\n{text}\n"
        if len(block) > remaining:
            skipped.append(path)
            continue
        chunks.append(block)
        included.append(path)
        remaining -= len(block)

    if cache_dirty and cache_file is not None:
        _save_file_cache(cache_file, cache)

    return "\n".join(chunks), included, skipped


def describe_skipped(skipped: list[Path]) -> str:
    """A note to append to the model's own prompt when build_context had to
    drop some files for being over budget (or unreadable). Printing the skip
    list to the operator's terminal isn't enough -- the model itself never
    sees stderr, so from its side those files simply don't exist. Without
    this, it reasons correctly from an incomplete picture and confidently
    reports a file as missing/absent when it's actually just missing from
    *this* context, not from the repository. Returns "" when nothing was
    skipped, so callers can always append the result unconditionally."""
    if not skipped:
        return ""
    listing = "\n".join(f"- {p}" for p in skipped)
    return (
        "\n\nNOTE: the following files exist in the repository but were left "
        "out of the content below (over the size budget, or unreadable) -- "
        "do not report them as missing or absent. If a finding would depend "
        "on one of them, say it exists but could not be reviewed here due to "
        "the context budget, not that it's missing:\n" + listing + "\n"
    )


def parse_verdict(report_text: str) -> str:
    """Reads the auditor's required leading "VERDICT: PASS" / "VERDICT: REJECT"
    line. Returns "PASS", "REJECT", or "UNKNOWN" if the report didn't follow
    that format. Callers should treat UNKNOWN like a REJECT for the purpose of
    deciding whether to trust the result unattended -- an auditor that ignored
    its own required format is not evidence of a clean pass, just evidence the
    instruction wasn't followed."""
    for line in report_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("VERDICT:"):
            value = stripped.split(":", 1)[1].strip().upper()
            if value.startswith("PASS"):
                return "PASS"
            if value.startswith("REJECT"):
                return "REJECT"
        break  # only the first non-empty line counts
    return "UNKNOWN"


def read_required_artifact(path: Path, produced_by: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise ReportError(
            f"Could not read {path} ({exc}). Run {produced_by} first -- this phase reads its output."
        ) from exc


def stream_chat(
    model_alias: str,
    messages: list[dict[str, str]],
    on_chunk: Callable[[str], None] | None = None,
    router_url: str = DEFAULT_ROUTER_URL,
    timeout: float = 600.0,
) -> str:
    """Call the model over the router's streaming endpoint with an arbitrary
    message history, returning the full response text. If on_chunk is
    given, it's called with each text delta as it arrives -- e.g. to print
    progress live instead of going silent until the whole (possibly
    multi-minute) response is done.

    Deliberately never sends an OpenAI `tools` field: that's what routes a
    request through mtplx's --tool-prompt-mode hybrid bridge, which is what
    produced the "unclosed_tool_call" parse failure seen going through a
    chat-client extension. Callers that need actions taken (see builder_agent.py) define
    their own plain-text calling convention in the system prompt and parse
    it themselves instead, trading mtplx's bridge for one we fully control.

    Deliberately does NOT raise when the response looks like a failed
    native tool-call attempt (see looks_like_failed_tool_call) -- that
    check belongs to the caller, not this primitive. call_model (the
    one-shot scout/planner/auditor path, with no retry loop of its own)
    raises on it. builder_agent.py's loop calls stream_chat directly
    because it retries this exact failure itself; raising here would
    short-circuit that retry before the loop ever saw the response."""
    url = router_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model_alias,
        "stream": True,
        "messages": messages,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # No chat-client extension in the picture -- skip the
            # chat-client-oriented phase reinforcement the router otherwise
            # injects for this alias.
            "X-Model-Deck-Skip-Injection": "1",
        },
        method="POST",
    )
    full_text: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            buffer = ""
            for raw_line in response:
                buffer += raw_line.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    event, buffer = buffer.split("\n\n", 1)
                    for line in event.splitlines():
                        line = line.strip("\r")
                        if not line.startswith("data: "):
                            continue
                        data = line[len("data: "):]
                        if data == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(data)
                        except ValueError:
                            continue
                        choices = parsed.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            full_text.append(delta)
                            if on_chunk is not None:
                                on_chunk(delta)
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError, so this must come first -- catching
        # URLError first would swallow every HTTPError here too and silently
        # discard the response body (e.g. OpenAI's actual reason for a 429:
        # quota vs. rate limit vs. TPM cap), leaving only a generic message.
        detail = exc.read().decode(errors="replace")
        raise ReportError(f"Router returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ReportError(f"Could not reach router at {router_url}: {exc}") from exc
    return "".join(full_text)


def call_model(
    model_alias: str,
    system_prompt: str,
    user_content: str,
    on_chunk: Callable[[str], None] | None = None,
    router_url: str = DEFAULT_ROUTER_URL,
    timeout: float = 600.0,
) -> str:
    """One-shot system+user convenience wrapper around stream_chat, for the
    single-exchange scripts (scout/planner/auditor). Unlike stream_chat
    itself, this raises when the response looks like a failed native
    tool-call attempt -- there's no retry loop here to hand it to, so the
    only options are surface it as a failure or silently save the garbled
    text as the report. builder_agent.py doesn't go through this wrapper;
    it calls stream_chat directly so it can retry the same failure itself."""
    result = stream_chat(
        model_alias,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        on_chunk=on_chunk,
        router_url=router_url,
        timeout=timeout,
    )
    if looks_like_failed_tool_call(result):
        raise ReportError(
            "The model attempted a tool call that doesn't exist in this request "
            "(no `tools` field was sent), and the server injected an error into "
            "the response instead of writing real content:\n\n"
            f"{result}\n\n"
            "This is a chat-template/model quirk, not a transient failure -- "
            "retrying with the same model may reproduce it. Seen so far on "
            "Builder's \"tokenizer\" chat-template profile."
        )
    return result

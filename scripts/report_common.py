"""Shared engine behind scripts/{scout,planner,auditor}_report.py.

Each of those is a thin, phase-specific wrapper: they all do the same three
things (collect file contents, ask the model for one written report, save
it) with no tool-calling and no agentic loop -- see scout_report.py's module
docstring for why that shape was chosen.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

DEFAULT_ROUTER_URL = "http://127.0.0.1:8100/v1"

# Fallback only -- char_budget_for_role() below derives the real number from
# whichever role is actually running. This constant is what's used when that
# derivation can't be done (an unresolvable role, a cloud-backed phase whose
# context window this app doesn't track). Sized against the 131,072-token
# window the local roles run at the time this was last hand-tuned, keeping
# the same proportional headroom the original 260k-char number reserved
# under the 100k window it was written for.
#
# This constant is exactly the failure mode char_budget_for_role exists to
# prevent: it was written once for a 100k window and silently went stale
# when the roles moved to 131,072, quietly dropping files over a budget that
# no longer matched reality until that drift was noticed and fixed by hand.
# A fallback is still needed -- keep it, but don't expect it to stay
# accurate; that's the derived function's job now.
DEFAULT_CHAR_BUDGET = 360_000

# Conservative estimate for code-heavy text -- used only to convert the
# token headroom below into a character count, since build_context reads
# files directly and has no tokenizer to count with. Erring low (an
# overestimate of chars-per-token would let more file content in than
# actually fits) is what makes this a fine approximation rather than a real
# risk of overflowing the context window.
CHARS_PER_TOKEN = 3.7

# Reserved for the system prompt and the model's own response, on top of
# whatever the file content consumes. Not tuned per-phase: Scout, Planner,
# and Auditor's system prompts are all a few thousand tokens, and their
# reports run a few thousand more -- 34k is comfortable headroom for any of
# them without giving back so much of the window that raising context_window
# stops mattering.
RESPONSE_HEADROOM_TOKENS = 34_000


def char_budget_for_role(phase: str) -> int:
    """The file-content character budget derived from the *actual*
    context_window of the local role backing `phase`, so raising the window
    in the Deck tab raises the budget along with it -- this is the fix for
    the exact drift DEFAULT_CHAR_BUDGET above describes: a hand-typed number
    sized for one window value, silently wrong after the window changed and
    nothing forced it to be revisited.

    Falls back to DEFAULT_CHAR_BUDGET when the role can't be resolved: an
    unknown phase, modeldeck not importable (these scripts are meant to run
    standalone), or -- for "planner" specifically -- the planner currently
    backed by a cloud model rather than roles["planner"]. A cloud model's
    real context window isn't tracked anywhere in this app, so deriving a
    number from the *local* planner role while it sits unused would be a
    guess dressed up as a measurement; DEFAULT_CHAR_BUDGET is the honest
    answer there, not a bug to fix later.

    Getting this number right is an optimization, not a requirement for the
    phase to run -- any exception here is swallowed and treated as "use the
    fallback," never as a reason to fail the run."""
    try:
        project_dir = Path(__file__).resolve().parents[1]
        if str(project_dir) not in sys.path:
            sys.path.insert(0, str(project_dir))
        from modeldeck.state import load_state

        state = load_state()
        if phase == "planner" and (state.get("planner") or {}).get("kind") != "local":
            return DEFAULT_CHAR_BUDGET
        role = (state.get("roles") or {}).get(phase) or {}
        context_window = int(role.get("context_window") or 0)
        if context_window <= 0:
            return DEFAULT_CHAR_BUDGET
        available_tokens = context_window - RESPONSE_HEADROOM_TOKENS
        if available_tokens <= 0:
            return DEFAULT_CHAR_BUDGET
        return int(available_tokens * CHARS_PER_TOKEN)
    except Exception:
        return DEFAULT_CHAR_BUDGET

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
    # This project's own scratch area for test projects (an unrelated
    # thermocycler app, etc.). Scanning the orchestrator repo used to pull
    # ~250k chars of it into every context, which at the old budget meant
    # everything sorting after "sandbox/" -- the whole of scripts/ and
    # tests/ -- was dropped. Note this is matched RELATIVE TO THE SCAN ROOT
    # (see collect_files), so pointing a phase directly at
    # sandbox/SomeProject still scans it normally; it is only ignored when
    # it sits *inside* the tree being scanned.
    "sandbox",
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


def write_report(path: Path, content: str) -> None:
    """Writes content to path, archiving whatever was there first into
    path.parent/history/.

    Every phase writes to the same fixed filename every run
    (.ai/audit-report.md, etc.) because everything downstream -- the next
    phase's REQUIRED_ARTIFACT_FOR_START check, resolve_ai_path callers,
    a human rereading a report -- expects that exact path to hold the
    current run's result. That fixed-path contract is also what makes a
    re-run destructive: an audit that REJECTs and triggers a Renovator
    repair pass gets overwritten by its own re-audit, so by the time
    anyone goes looking, the fix list the Renovator actually worked from
    is gone -- confirmed live: a re-audit at 18:09 was the only surviving
    copy, and the original REJECT that produced the Renovator's fix list
    had already been overwritten by the time it was read.

    Archiving on write keeps the fixed path working for every existing
    consumer while leaving a real, diffable run history behind. Uses the
    *previous* file's own mtime for the archive's timestamp, not "now" --
    so the archived name reflects when that version was actually produced,
    which is the number worth comparing runs by."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        history_dir = path.parent / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
        archived = history_dir / f"{path.stem}_{stamp}{path.suffix}"
        suffix = 2
        while archived.exists():
            archived = history_dir / f"{path.stem}_{stamp}-{suffix}{path.suffix}"
            suffix += 1
        shutil.copy2(path, archived)
    path.write_text(content)


def resolve_ai_path(target: Path, filename: str) -> Path:
    """Anchor a .ai/<filename> artifact to the project being worked on (the
    target path passed on the command line), not to wherever this tool
    happens to be installed or invoked from."""
    base = target if target.is_dir() else target.parent
    return base / ".ai" / filename


def collect_files(root: Path, extensions: set[str]) -> list[Path]:
    """Source files under root, ignoring IGNORED_DIRS, ordered so that a
    context-budget shortfall degrades evenly (see _interleave_by_area).

    Ignore matching is relative to root, not against the whole path: a
    target's own location must not disqualify it. Scanning
    sandbox/Thermocycler_Program_REV1 directly has to work even though
    "sandbox" is an ignored directory name, and the same applies to anyone
    whose checkout happens to live under a directory called "build" or
    "dist"."""
    if root.is_file():
        return [root]
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        if any(part.endswith(XCODE_BUNDLE_SUFFIXES) for part in relative_parts):
            continue
        if path.suffix not in extensions:
            continue
        files.append(path)
    return _interleave_by_area(files, root)


def _interleave_by_area(files: list[Path], root: Path) -> list[Path]:
    """Round-robin the files across their top-level directories, keeping
    each area's own files in alphabetical order.

    build_context fills its budget in list order and drops the remainder, so
    a plain alphabetical sort turns a budget shortfall into an amputation:
    on this repo it cut off inside "scripts/", and every file in scripts/
    and tests/ vanished from every Scout context -- meaning Scout had never
    read the code that actually runs the pipeline. Interleaving doesn't
    create room, but it changes *how* a shortfall lands: instead of one
    area disappearing completely, every area loses its tail. A report built
    from a thin slice of everything is recoverable; one built with no
    knowledge that scripts/ exists is not, because nothing downstream can
    tell the difference."""
    areas: dict[str, list[Path]] = {}
    for path in files:
        relative = path.relative_to(root).parts
        area = relative[0] if len(relative) > 1 else ""
        areas.setdefault(area, []).append(path)
    ordered: list[Path] = []
    queues = list(areas.values())
    while queues:
        for queue in list(queues):
            ordered.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    return ordered


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


def parse_builder_accuracy(report_text: str) -> int | None:
    """Reads the auditor's optional "BUILDER_ACCURACY: <0-100>" line -- its
    estimate of what fraction of the plan the builder actually implemented
    correctly, judged against the code and the real test results. Returns
    None when absent or unparseable.

    This is a model's judgement, not a measurement, so it is only meaningful
    compared against itself: the point is to make different builder settings
    (model, reasoning mode, turn budget) comparable across benchmark runs,
    not to assert an objective score. Treat a single number as noise; treat
    a consistent gap between two configurations as signal."""
    for line in report_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("BUILDER_ACCURACY:"):
            digits = "".join(ch for ch in stripped.split(":", 1)[1] if ch.isdigit())
            if not digits:
                return None
            value = int(digits)
            return value if 0 <= value <= 100 else None
        if stripped.upper().startswith("VERDICT:"):
            continue
        break
    return None


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

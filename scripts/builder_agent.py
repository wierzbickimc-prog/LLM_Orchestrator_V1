#!/usr/bin/env python3
"""Builder: a small, purpose-built agent loop -- not a chat-client extension,
not an OpenAI `tools` field (see builder_tools.py and report_common.stream_chat for why).
Three actions (read_file, write_file, run_command), our own system prompt,
our own parsing and error recovery.

This is the one script in this set that actually changes files and runs
commands with your permissions, scoped to the target directory but not
sandboxed beyond that -- read .ai/implementation-plan.md before running it,
and consider trying it on a low-stakes target first.

Usage:
    scripts/builder_agent.py sandbox/Thermocycler_Program_REV1
    scripts/builder_agent.py sandbox/Thermocycler_Program_REV1 --dry-run
    scripts/builder_agent.py sandbox/Thermocycler_Program_REV1 --max-steps 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from builder_tools import ToolCallParseError, extract_tool_call, run_tool
from report_common import (
    DEFAULT_ROUTER_URL,
    ReportError,
    looks_like_failed_tool_call,
    read_required_artifact,
    resolve_ai_path,
    stream_chat,
)

DEFAULT_MAX_STEPS = 40
DEFAULT_COMMAND_TIMEOUT = 120.0

SYSTEM_PROMPT_TEMPLATE = """You are a careful software engineer implementing an \
already-approved plan. Implement only what the plan below calls for -- do not \
expand scope, and do not edit anything the plan doesn't mention.

You have no special tool-calling API. To take an action, respond with \
EXACTLY ONE fenced code block labeled tool containing a single JSON object, \
and nothing meaningful outside it. Available actions:

```tool
{{"name": "read_file", "arguments": {{"path": "relative/path"}}}}
```
```tool
{{"name": "write_file", "arguments": {{"path": "relative/path", "content": "full new file content"}}}}
```
```tool
{{"name": "append_file", "arguments": {{"path": "relative/path", "content": "more content to add to the end"}}}}
```
```tool
{{"name": "run_command", "arguments": {{"command": "shell command, e.g. pytest"}}}}
```

All paths are relative to the project root and must stay within it. \
write_file replaces the entire file -- always read a file before changing \
part of it, and reproduce every part you are not changing. If a file you're \
writing is large, do not try to send it all in one write_file call: use \
write_file for the first part and one or more append_file calls for the \
rest, each small enough to comfortably fit in a single response. A response \
that gets cut off before its ```tool block closes is treated as an error and \
you will be asked to retry -- avoid that by splitting large writes up front \
rather than after it happens.

Send only one tool call per response, then wait for its result before the \
next one. When you have implemented the plan and verified it (tests pass), \
reply with plain text and no ```tool block: a summary of what you did, files \
changed, verification commands run and their results, and any discrepancies \
from the plan. That plain-text reply ends the session, so do not send it \
until you are actually done.

The complete implementation plan is already included below in full -- do \
not spend a turn reading it again from .ai/implementation-plan.md, you \
already have everything it contains.

Implementation plan:
{plan}"""


def run_agent(
    root: Path,
    plan: str,
    task: str,
    max_steps: int,
    command_timeout: float,
    router_url: str,
    timeout: float,
    dry_run: bool,
    on_chunk,
) -> tuple[str, int, list[str]]:
    """Returns (final_report_text, steps_taken, touched_files) -- touched_files
    is every relative path passed to write_file/append_file this run (deduped,
    sorted), for the Auditor to prioritize instead of treating the whole tree
    as equally likely to matter."""
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(plan=plan)
    user_intro = "Begin." if not task else f"Begin. Additional note from the requester:\n{task}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_intro},
    ]
    touched: set[str] = set()

    for step in range(1, max_steps + 1):
        on_chunk(f"\n--- step {step} ---\n")
        response = stream_chat(
            "builder", messages, on_chunk=on_chunk, router_url=router_url, timeout=timeout
        )
        messages.append({"role": "assistant", "content": response})

        if looks_like_failed_tool_call(response):
            # The model attempted a native tool call this request never
            # offered (no `tools` field is ever sent -- see stream_chat's
            # docstring), and mtplx injected an error into the output
            # instead of real content. Without this check, a response like
            # this has no ```tool block, so extract_tool_call would return
            # None and the caller would mistake this garbled response for
            # legitimate completion, saving it as the final report.
            on_chunk("\n[model attempted an unavailable tool; asking it to retry as plain ```tool]\n")
            messages.append({
                "role": "user",
                "content": "That didn't work -- there is no tool-calling API available here. "
                "To act, respond with exactly one fenced ```tool block as instructed. Try again.",
            })
            continue

        try:
            call = extract_tool_call(response)
        except ToolCallParseError as exc:
            on_chunk(f"\n[invalid tool call: {exc}]\n")
            messages.append({"role": "user", "content": f"Your last response was invalid: {exc}. Try again."})
            continue

        if call is None:
            return response, step, sorted(touched)

        result = run_tool(call, root, command_timeout, dry_run)
        on_chunk(f"\n[{call['name']} -> {len(result)} chars]\n")
        messages.append({"role": "user", "content": f"Tool result for {call['name']}:\n{result}"})
        if call["name"] in ("write_file", "append_file") and not dry_run:
            path = call["arguments"].get("path")
            if isinstance(path, str):
                touched.add(path)

    return (
        f"Stopped after {max_steps} steps without the model signaling completion. "
        "This is a safety cap, not a crash -- review the transcript above; the plan "
        "may need to be smaller, or max-steps raised.",
        max_steps,
        sorted(touched),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="project directory to work in")
    parser.add_argument("--task", default="", help="optional extra note/constraint for the builder")
    parser.add_argument("--plan", type=Path, default=None, help="defaults to <path>/.ai/implementation-plan.md")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/builder-report.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--command-timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT)
    parser.add_argument("--timeout", type=float, default=600.0, help="per-generation timeout")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run the loop but log intended writes/commands instead of executing them",
    )
    args = parser.parse_args()

    if not args.path.is_dir():
        print(f"{args.path} is not a directory", file=sys.stderr)
        return 1
    if args.plan is None:
        args.plan = resolve_ai_path(args.path, "implementation-plan.md")
    if args.out is None:
        args.out = resolve_ai_path(args.path, "builder-report.md")

    try:
        plan = read_required_artifact(args.plan, produced_by="scripts/planner_report.py")
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Implementing against {args.plan} in {args.path} (max {args.max_steps} steps)"
          + (" [dry-run]" if args.dry_run else "") + "...")
    try:
        report, steps, touched = run_agent(
            args.path, plan, args.task, args.max_steps, args.command_timeout,
            args.router_url, args.timeout, args.dry_run,
            on_chunk=lambda piece: print(piece, end="", flush=True),
        )
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"\n\nFinished after {steps} step(s).")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(f"Wrote {args.out}")

    if touched:
        touched_path = resolve_ai_path(args.path, "builder-touched-files.json")
        touched_path.write_text(json.dumps(touched))
        print(f"Wrote {touched_path} ({len(touched)} file(s) touched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

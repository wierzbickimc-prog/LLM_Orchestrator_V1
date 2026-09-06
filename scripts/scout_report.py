#!/usr/bin/env python3
"""Scout, without an agent in the loop.

Walks a target path itself, reads the matching files directly, and asks the
scout model to summarize them in one shot -- no tool-calling, no autonomous
exploration, no chat-extension system prompt riding along. That sidesteps
the whole category of failure this project hit going through a chat-client
extension: malformed tool-call markup, repeated/stale reads, ask_question
detours, tool-schema token overhead. The model only ever has to produce text.

Usage:
    scripts/scout_report.py "add a read-only status page" sandbox/Thermocycler_Program_REV1
    scripts/scout_report.py "add a read-only status page" sandbox/Thermocycler_Program_REV1 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from report_common import (
    DEFAULT_CHAR_BUDGET,
    DEFAULT_EXTENSIONS,
    DEFAULT_ROUTER_URL,
    NO_TOOLS_NOTICE,
    ReportError,
    build_context,
    call_model,
    collect_files,
    describe_skipped,
    resolve_ai_path,
)

SYSTEM_PROMPT = f"""You are a focused code-investigation assistant. {NO_TOOLS_NOTICE}

Produce a single Markdown report containing:
- relevant files, symbols, and execution paths, with exact file references \
drawn only from the content given below;
- existing tests and commands relevant to this task;
- observed behavior, clearly separated from any hypotheses;
- risks, unknowns, and specific open questions for the Planner.

Verify every factual claim against the file contents given below -- do not \
guess about code you were not shown. If the provided files are insufficient \
to answer part of the task, say so explicitly as an open question rather \
than speculating.

Respond with only the report's Markdown content: no preamble, no code fence \
wrapping the whole response, no tool calls (you have none)."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="what the change/investigation is for")
    parser.add_argument("path", type=Path, help="file or directory to scan")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/scout-report.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument("--char-budget", type=int, default=DEFAULT_CHAR_BUDGET)
    parser.add_argument(
        "--ext", action="append", default=None,
        help="restrict to this extension (repeatable); default is a built-in source/text list",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--dry-run", action="store_true", help="collect and report file stats without calling the model")
    args = parser.parse_args()
    if args.out is None:
        args.out = resolve_ai_path(args.path, "scout-report.md")

    extensions = set(args.ext) if args.ext else DEFAULT_EXTENSIONS
    files = collect_files(args.path, extensions)
    if not files:
        print(f"No matching files under {args.path}", file=sys.stderr)
        return 1

    context, included, skipped = build_context(files, args.char_budget, cache_target=args.path)
    print(f"Included {len(included)} file(s), {len(context):,} chars.")
    if skipped:
        print(f"Skipped {len(skipped)} file(s) (over budget or unreadable):", file=sys.stderr)
        for path in skipped:
            print(f"  - {path}", file=sys.stderr)

    if args.dry_run:
        return 0

    print("Calling scout model (this can take a while for a large context)...")
    try:
        report = call_model(
            "scout", SYSTEM_PROMPT,
            f"Task:\n{args.task}\n\nRepository files:\n{context}{describe_skipped(skipped)}",
            on_chunk=lambda piece: print(piece, end="", flush=True),
            router_url=args.router_url, timeout=args.timeout,
        )
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

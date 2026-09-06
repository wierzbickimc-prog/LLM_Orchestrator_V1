#!/usr/bin/env python3
"""Planner, without an agent in the loop.

Reads .ai/scout-report.md, then reads only the specific files it cites --
not the whole tree. Re-ingesting everything Scout already read would defeat
the point of Scout being a cheaper summarizer: Planner would pay the same
full-tree prefill cost a second time, on top of Scout's report, usually on a
slower model. Falls back to a full scan only if the scout report doesn't
cite any resolvable files (or --full-context is passed).

Usage:
    scripts/planner_report.py sandbox/Thermocycler_Program_REV1
    scripts/planner_report.py sandbox/Thermocycler_Program_REV1 --task "keep it read-only"
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
    read_required_artifact,
    referenced_files,
    resolve_ai_path,
)

SYSTEM_PROMPT_TEMPLATE = """You are a focused implementation-planning assistant. {no_tools_notice} \
Do not implement the change yourself; you are writing the plan the Builder \
will implement from.

{scope_note}

Produce a single Markdown implementation plan containing:
- objective and verified current behavior;
- exact files and behavioral contracts to change;
- ordered implementation steps;
- explicit scope boundaries and safety constraints;
- tests and acceptance criteria;
- discrepancies or unsupported claims found in the scout report.

If the provided files are insufficient to plan part of the task, say so \
explicitly as an open question rather than speculating.

Respond with only the plan's Markdown content: no preamble, no code fence \
wrapping the whole response, no tool calls (you have none)."""

SCOPE_NOTE_CITED = (
    "The files below are exactly the ones the scout report cites, so there is "
    "no separate file-selection step to redo here -- ground the plan directly "
    "in their actual contents rather than re-deriving what's relevant from "
    "scratch. Where a specific scout claim doesn't match what a file actually "
    "contains, note that discrepancy in the plan rather than silently working "
    "around it."
)
SCOPE_NOTE_FULL_SCAN = (
    "The scout report didn't cite any resolvable files, so the files below "
    "are a full scan of the target instead of a scout-scoped subset -- you do "
    "need to judge relevance yourself here. Where a specific scout claim "
    "doesn't match what a file actually contains, note that discrepancy in "
    "the plan rather than silently working around it."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="file or directory the scout report refers to")
    parser.add_argument("--task", default="", help="optional extra note/constraint for the planner")
    parser.add_argument("--scout-report", type=Path, default=None, help="defaults to <path>/.ai/scout-report.md")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/implementation-plan.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument("--char-budget", type=int, default=DEFAULT_CHAR_BUDGET)
    parser.add_argument(
        "--ext", action="append", default=None,
        help="restrict to this extension (repeatable); default is a built-in source/text list",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--full-context", action="store_true",
        help="scan the whole tree instead of only the files the scout report cites",
    )
    parser.add_argument("--dry-run", action="store_true", help="collect and report file stats without calling the model")
    args = parser.parse_args()
    if args.scout_report is None:
        args.scout_report = resolve_ai_path(args.path, "scout-report.md")
    if args.out is None:
        args.out = resolve_ai_path(args.path, "implementation-plan.md")

    try:
        scout_report = read_required_artifact(args.scout_report, produced_by="scripts/scout_report.py")
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    extensions = set(args.ext) if args.ext else DEFAULT_EXTENSIONS
    files: list[Path] = [] if args.full_context else referenced_files(scout_report, args.path)
    if files:
        print(f"Scout report cites {len(files)} resolvable file(s); reading only those.")
        scope_note = SCOPE_NOTE_CITED
    else:
        if not args.full_context:
            print(
                "Scout report cited no resolvable files -- falling back to a full scan.",
                file=sys.stderr,
            )
        files = collect_files(args.path, extensions)
        if not files:
            print(f"No matching files under {args.path}", file=sys.stderr)
            return 1
        scope_note = SCOPE_NOTE_FULL_SCAN

    context, included, skipped = build_context(files, args.char_budget, cache_target=args.path)
    print(f"Included {len(included)} file(s), {len(context):,} chars.")
    if skipped:
        print(f"Skipped {len(skipped)} file(s) (over budget or unreadable):", file=sys.stderr)
        for path in skipped:
            print(f"  - {path}", file=sys.stderr)

    if args.dry_run:
        return 0

    user_content = (
        f"Scout report ({args.scout_report}):\n{scout_report}\n\n"
        + (f"Additional note from the requester:\n{args.task}\n\n" if args.task else "")
        + f"Repository files:\n{context}"
        + describe_skipped(skipped)
    )

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(no_tools_notice=NO_TOOLS_NOTICE, scope_note=scope_note)

    print("Calling planner model (this can take a while for a large context)...")
    try:
        report = call_model(
            "planner", system_prompt, user_content,
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

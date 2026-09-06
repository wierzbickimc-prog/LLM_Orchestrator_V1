#!/usr/bin/env python3
"""Auditor, without an agent in the loop.

Reads .ai/implementation-plan.md plus the resulting source files directly,
and asks the auditor model to write .ai/audit-report.md in one shot -- same
no-tool-calling shape as scout_report.py, for the same reasons (see that
script's docstring).

This reads the *current* file contents at the given path, so run it after
the Builder has made its changes -- it audits what's actually on disk
against the plan, not a diff.

Usage:
    scripts/auditor_report.py sandbox/Thermocycler_Program_REV1
"""

from __future__ import annotations

import argparse
import json
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
    resolve_ai_path,
)

SYSTEM_PROMPT = f"""You are a focused implementation-audit assistant. {NO_TOOLS_NOTICE} \
Do not edit anything; you are producing an audit report, not a fix.

Your response's very first line must be exactly one of:
VERDICT: PASS
VERDICT: REJECT
A downstream process parses that exact line to decide whether to hand this \
off for repair, so it must be the first non-empty line, with no other text \
before it. Use REJECT for anything a reasonable reviewer would block on \
before merging (a plan-compliance gap, a missing safety constraint, an \
untested and materially risky change) -- not for every stylistic nit. Minor \
residual gaps that don't rise to that bar belong in the findings below, not \
in the verdict.

After the verdict line, produce a single Markdown audit report with findings \
ordered by severity. For each finding include a file reference, impact, and \
evidence drawn only from the content given below. Confirm:
- plan compliance (does the code actually do what the plan says?);
- test coverage for the change (is there a test, and does it look like it \
would actually catch a regression?);
- error handling for the new/changed behavior;
- unintended scope (anything changed that the plan didn't call for);
- network exposure: if this change alters what a server binds to by default \
(e.g. a new flag or mode that changes the default host from a loopback \
address to something LAN- or internet-reachable), call this out explicitly \
as its own finding even when a guard (auth check, write-block, etc.) is \
present and working correctly. A working guard is a reason the finding \
isn't a REJECT-level defect, not a reason to omit the finding -- exposing \
previously-local-only data or control to the network by default is a \
deliberate tradeoff a human should see stated plainly, not something to \
describe approvingly as "correctly implemented" alongside the rest of the \
plan-compliance checklist.

If the verdict is REJECT, include a "## Fix List" section: a numbered list \
of the specific defects to repair, each naming the exact file(s) and the \
concrete change needed. This list becomes another agent's entire scope for \
the repair pass, so keep it to what actually needs fixing -- don't fold in \
unrelated suggestions or restate the whole plan.

If no defects are found, state that explicitly and list any residual test \
gaps. If the provided files are insufficient to confirm part of the plan, \
say so explicitly as an open question rather than speculating.

Respond with only the verdict line followed by the report's Markdown \
content: no preamble before the verdict, no code fence wrapping the whole \
response, no tool calls (you have none)."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="file or directory that was changed")
    parser.add_argument("--task", default="", help="optional extra note/constraint for the auditor")
    parser.add_argument("--plan", type=Path, default=None, help="defaults to <path>/.ai/implementation-plan.md")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/audit-report.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument("--char-budget", type=int, default=DEFAULT_CHAR_BUDGET)
    parser.add_argument(
        "--ext", action="append", default=None,
        help="restrict to this extension (repeatable); default is a built-in source/text list",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--dry-run", action="store_true", help="collect and report file stats without calling the model")
    args = parser.parse_args()
    if args.plan is None:
        args.plan = resolve_ai_path(args.path, "implementation-plan.md")
    if args.out is None:
        args.out = resolve_ai_path(args.path, "audit-report.md")

    try:
        plan = read_required_artifact(args.plan, produced_by="scripts/planner_report.py")
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    touched: list[str] = []
    touched_path = resolve_ai_path(args.path, "builder-touched-files.json")
    if touched_path.exists():
        try:
            touched = json.loads(touched_path.read_text())
        except (OSError, ValueError):
            touched = []

    extensions = set(args.ext) if args.ext else DEFAULT_EXTENSIONS
    files = collect_files(args.path, extensions)
    if not files:
        print(f"No matching files under {args.path}", file=sys.stderr)
        return 1

    if touched:
        # Builder's own tool log, not a guess -- these are the exact files it
        # wrote or appended to this run. Sort them first so build_context's
        # over-budget cutoff drops the least-likely-relevant files first,
        # not whatever happened to sort alphabetically after them.
        touched_resolved = {str((args.path / t).resolve()) for t in touched}
        files.sort(key=lambda p: str(p.resolve()) not in touched_resolved)
        print(f"Builder touched {len(touched)} file(s) this run; reviewing those first.")

    context, included, skipped = build_context(files, args.char_budget, cache_target=args.path)
    print(f"Included {len(included)} file(s), {len(context):,} chars.")
    if skipped:
        print(f"Skipped {len(skipped)} file(s) (over budget or unreadable):", file=sys.stderr)
        for path in skipped:
            print(f"  - {path}", file=sys.stderr)

    if args.dry_run:
        return 0

    user_content = (
        f"Implementation plan ({args.plan}):\n{plan}\n\n"
        + (f"Additional note from the requester:\n{args.task}\n\n" if args.task else "")
        + (
            f"Builder's own tool log reports touching these files this run -- "
            f"start your review there, but still scan everything below for "
            f"unintended scope: {', '.join(touched)}\n\n"
            if touched else ""
        )
        + f"Current repository files:\n{context}"
        + describe_skipped(skipped)
    )

    print("Calling auditor model (this can take a while for a large context)...")
    try:
        report = call_model(
            "auditor", SYSTEM_PROMPT, user_content,
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

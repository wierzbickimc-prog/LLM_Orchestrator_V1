#!/usr/bin/env python3
"""Renovator: Builder's own agent loop (see builder_agent.py), narrowly
rescoped to fix exactly what an Auditor REJECT verdict flagged -- not to
reimplement the plan from scratch. Reuses builder_agent.run_agent() as-is;
the only difference is what "the plan" contains (the audit's Fix List,
framed as the entire scope) and where the report lands.

Meant to run after auditor_report.py produced a "VERDICT: REJECT" report,
then hand back to auditor_report.py for re-review -- see report_common.
parse_verdict and the pipeline orchestration in modeldeck/gui.py.

Usage:
    scripts/renovator_agent.py sandbox/Thermocycler_Program_REV1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from builder_agent import DEFAULT_COMMAND_TIMEOUT, run_agent
from report_common import DEFAULT_ROUTER_URL, ReportError, read_required_artifact, resolve_ai_path, write_report

DEFAULT_MAX_STEPS = 40
# Was 20 ("a repair pass is scoped to a handful of defects, not a whole
# plan") -- too tight in a live run where Auditor's Fix List had 5
# substantial items across 3 files plus 2 test files, close to a full
# implementation's worth of work. Ran out and hit the generic step-cap
# message rather than a genuine self-reported stop. Kept below Builder's
# 80 to preserve the intended scope difference (a fix list, not a full
# plan), but 20 had no real margin for read+write+verify per item.

SCOPE_PREAMBLE = """The implementation below was already built and already audited. The audit \
REJECTED it for the specific defects listed under "Audit findings (your scope)" below \
-- that Fix List is your entire scope. Do not re-implement anything the audit didn't \
flag, do not revisit design decisions the audit didn't object to, and do not touch \
files the fix list doesn't name. Read whatever files you need to confirm the current \
state before changing anything -- some defects may already be partially addressed.

Original implementation plan, for context only (not additional scope):
{plan}

Audit findings (your scope):
{audit}"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="project directory to work in")
    parser.add_argument("--plan", type=Path, default=None, help="defaults to <path>/.ai/implementation-plan.md")
    parser.add_argument("--audit", type=Path, default=None, help="defaults to <path>/.ai/audit-report.md")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/renovator-report.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--command-timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT)
    parser.add_argument("--timeout", type=float, default=600.0, help="per-generation timeout")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run the loop but log intended writes/commands instead of executing them",
    )
    parser.add_argument(
        "--native-tool-calling", action="store_true",
        help="use OpenAI-style tool_calls instead of the ```tool convention -- only works if "
        "the renovator role's model server was launched with --tool-prompt-mode native",
    )
    args = parser.parse_args()

    if not args.path.is_dir():
        print(f"{args.path} is not a directory", file=sys.stderr)
        return 1
    if args.plan is None:
        args.plan = resolve_ai_path(args.path, "implementation-plan.md")
    if args.audit is None:
        args.audit = resolve_ai_path(args.path, "audit-report.md")
    if args.out is None:
        args.out = resolve_ai_path(args.path, "renovator-report.md")

    try:
        plan = read_required_artifact(args.plan, produced_by="scripts/planner_report.py")
        audit = read_required_artifact(args.audit, produced_by="scripts/auditor_report.py")
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    scoped_plan = SCOPE_PREAMBLE.format(plan=plan, audit=audit)

    print(f"Repairing against {args.audit}'s fix list in {args.path} (max {args.max_steps} steps)"
          + (" [dry-run]" if args.dry_run else "") + "...")
    try:
        report, steps, touched = run_agent(
            args.path, scoped_plan, "", args.max_steps, args.command_timeout,
            args.router_url, args.timeout, args.dry_run,
            on_chunk=lambda piece: print(piece, end="", flush=True),
            model_alias="renovator",
            native_tool_calling=args.native_tool_calling,
        )
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"\n\nFinished after {steps} step(s).")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.out, report)
    print(f"Wrote {args.out}")

    if touched:
        # Same artifact name builder_agent.py writes -- auditor_report.py
        # already knows to prioritize whatever's listed there, so a repair
        # pass gets the same "review this first" treatment as the original
        # build did, with no changes needed on the auditor side.
        touched_path = resolve_ai_path(args.path, "builder-touched-files.json")
        touched_path.write_text(json.dumps(touched))
        print(f"Wrote {touched_path} ({len(touched)} file(s) touched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

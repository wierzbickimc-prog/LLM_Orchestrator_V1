#!/usr/bin/env python3
"""Diagnose, without an agent in the loop.

The troubleshoot flow's answer to Scout. Scout does an open-ended
whole-codebase survey, which is the right first move for "add a feature"
and the wrong one for "X worked, then a change landed, now X is broken" --
there the regression is almost always in the most recent diff, and a broad
survey buries that signal (seen live: Scout chased backend serialization and
an env var while the actual cause was a one-line strict-mode slip in a JS
module).

So this phase is diff-anchored instead: it feeds the model the symptom, the
target repo's recent git history and uncommitted diff, the current content
of the changed files (sorted first), and -- when a test command can be
detected -- real output from actually running the suite. It asks for a root
cause with an exact file/line and a minimal fix, written to
.ai/diagnosis-report.md, which the Planner then consumes in place of a
scout report.

Same no-tool-calling shape as scout_report.py, for the same reasons (see
that script's docstring).

Usage:
    scripts/diagnose_report.py "the thermocyclers stopped appearing after the last change" sandbox/Thermocycler_Program_REV1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from report_common import (
    DEFAULT_TEST_TIMEOUT,
    DEFAULT_EXTENSIONS,
    DEFAULT_ROUTER_URL,
    NO_TOOLS_NOTICE,
    ReportError,
    build_context,
    call_model,
    collect_files,
    describe_skipped,
    detect_test_command,
    ensure_nonempty_report,
    git_changed_files,
    git_repro_context,
    importers_of,
    resolve_ai_path,
    run_test_suite,
    write_report,
)

# Diagnose reuses the Auditor role (same model, same defect-finding sampling
# profile) rather than owning a separate one -- diagnosis is a review task,
# not a distinct configuration worth its own Deck-tab knobs. The pipeline
# launches roles["auditor"] for this phase and serves it under that alias
# (see PHASE_ROLE_OVERRIDE in modeldeck/pipeline.py).
MODEL_ALIAS = "auditor"

# A regression diagnosis is small: a root cause, a mechanism, a minimal fix.
# It does not need the whole tree -- and paying full-tree prefill on a large
# repo is exactly what sank the first version (80k prompt tokens in a 108k
# window left a thinking model no room to answer).
#
# The changed files are the bug site by definition, so they are ALWAYS
# included in full -- never budget-dropped (the first cut of this dropped
# app.js, the actual bug, as "over budget" behind the feature's larger
# files). Importers get a capped budget on top. If the changed set itself is
# enormous, warn rather than silently truncate -- a diff that large is a
# real "too big to one-shot" signal.
IMPORTERS_CHAR_BUDGET = 45_000
CHANGED_FILES_SANITY_CEILING = 240_000  # ~65k tokens; warn past this
# Full-scan fallback (not a git repo / no changes): smaller than the role's
# derived budget on purpose, same reason as above.
FULL_SCAN_CHAR_BUDGET = 120_000
# Backstop for a reasoning runaway: a bounded stop instead of eating the
# window. Generous because the prompt is now small; the real fix is the
# prompt size.
RESPONSE_MAX_TOKENS = 20_000

SYSTEM_PROMPT = f"""You are a focused regression-diagnosis assistant. {NO_TOOLS_NOTICE} \
Do not fix anything; you are producing a diagnosis the Planner will turn \
into a fix plan.

A behavior that used to work is now broken. The single most recent change \
is the prime suspect -- the git history and uncommitted diff below are your \
highest-signal evidence, so start there before considering anything else.

Produce a single Markdown diagnosis report containing:
- the symptom, restated precisely (what the user actually observes);
- ROOT CAUSE: the exact file and line(s), quoted from the content below, \
and which change introduced it (name the commit, or say "uncommitted diff");
- MECHANISM: the specific causal chain from that code to the symptom -- not \
a general area of suspicion but the actual sequence of what happens;
- MINIMAL FIX: the smallest change that restores the behavior, stated as a \
concrete before/after. Do not expand scope to refactors, cleanup, or \
unrelated hardening;
- VERIFICATION: an existing test, or a command / manual step, that would \
confirm the fix;
- CONFIDENCE, and any alternative hypotheses you could not rule out from \
what you were shown.

If a "Test suite execution" block appears below, it is REAL output from \
running the tests just now -- treat it as ground truth over your own \
reading of the test source.

Verify every claim against the content given below -- do not guess about \
code you were not shown. If the evidence is insufficient to isolate a \
single cause, say so explicitly and give the narrowest set of suspects \
rather than forcing one answer.

Respond with only the report's Markdown content: no preamble, no code fence \
wrapping the whole response, no tool calls (you have none)."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="the symptom -- what broke, as the user reported it")
    parser.add_argument("path", type=Path, help="file or directory to investigate")
    parser.add_argument("--out", type=Path, default=None, help="defaults to <path>/.ai/diagnosis-report.md")
    parser.add_argument("--router-url", default=DEFAULT_ROUTER_URL)
    parser.add_argument(
        "--full-context", action="store_true",
        help="scan the whole tree instead of just changed files + their importers",
    )
    parser.add_argument(
        "--ext", action="append", default=None,
        help="restrict to this extension (repeatable); default is a built-in source/text list",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--test-command", default=None,
        help="override the auto-detected test command (e.g. 'swift test')",
    )
    parser.add_argument(
        "--skip-test-run", action="store_true",
        help="don't run the test suite -- reason from source and the diff only",
    )
    parser.add_argument("--test-timeout", type=float, default=DEFAULT_TEST_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true", help="collect and report file stats without calling the model")
    args = parser.parse_args()
    if args.out is None:
        args.out = resolve_ai_path(args.path, "diagnosis-report.md")

    extensions = set(args.ext) if args.ext else DEFAULT_EXTENSIONS
    base = args.path if args.path.is_dir() else args.path.parent

    repro_context = git_repro_context(args.path)
    changed_ids = git_changed_files(args.path)
    all_files = collect_files(args.path, extensions)
    changed_paths = sorted(p for p in all_files if str(p.resolve()) in changed_ids)

    skipped: list = []
    if changed_paths and not args.full_context:
        # Diff-anchored: the changed files ALWAYS in full (they are the bug
        # site), then the modules that import them (where the regression
        # surfaces), capped. This is the whole point of Diagnose over
        # Scout -- not a whole-tree survey.
        importers = [p for p in importers_of(changed_paths, base, extensions) if p not in changed_paths]
        changed_ctx, changed_in, changed_skip = build_context(
            changed_paths, 10 * CHANGED_FILES_SANITY_CEILING, cache_target=args.path,
        )
        if len(changed_ctx) > CHANGED_FILES_SANITY_CEILING:
            print(
                f"WARNING: {len(changed_ctx):,} chars of changed files -- this diff may be "
                "too large to diagnose reliably in one shot.",
                file=sys.stderr,
            )
        imp_ctx, imp_in, imp_skip = build_context(
            importers, IMPORTERS_CHAR_BUDGET, cache_target=args.path,
        )
        context = (
            f"--- Changed files ({len(changed_in)}) ---\n{changed_ctx}\n"
            f"--- Files that import them ({len(imp_in)}) ---\n{imp_ctx}"
        )
        included = changed_in + imp_in
        skipped = changed_skip + imp_skip
        print(f"Diff-anchored: {len(changed_in)} changed file(s) + {len(imp_in)} importer(s).")
    else:
        if not repro_context:
            print("Target is not a git repository -- diagnosing from a full scan.", file=sys.stderr)
        elif not changed_paths:
            print("Git shows no changed files -- diagnosing from a full scan.", file=sys.stderr)
        else:
            print("--full-context: scanning the whole tree.")
        if not all_files:
            print(f"No matching files under {args.path}", file=sys.stderr)
            return 1
        context, included, skipped = build_context(
            all_files, FULL_SCAN_CHAR_BUDGET, cache_target=args.path,
        )

    print(f"Included {len(included)} file(s), {len(context):,} chars.")
    if skipped:
        print(f"Skipped {len(skipped)} file(s) (over budget or unreadable):", file=sys.stderr)
        for path in skipped:
            print(f"  - {path}", file=sys.stderr)

    if args.dry_run:
        return 0

    test_block = ""
    if not args.skip_test_run:
        command = args.test_command or detect_test_command(args.path)
        if command:
            print(f"Running test suite: {command}")
            test_output = run_test_suite(args.path, command, args.test_timeout)
            print(test_output)
            test_block = f"Test suite execution (ran just now, real output):\n{test_output}\n\n"
        else:
            print("No test command detected -- skipping.")

    user_content = (
        f"Reported symptom:\n{args.task}\n\n"
        + (f"Git history and uncommitted changes:\n{repro_context}\n\n" if repro_context else "")
        + test_block
        + f"Current content of the changed files and their importers:\n{context}"
        + describe_skipped(skipped)
    )

    print("Calling diagnosis model...")
    try:
        report = call_model(
            MODEL_ALIAS, SYSTEM_PROMPT, user_content,
            on_chunk=lambda piece: print(piece, end="", flush=True),
            router_url=args.router_url, timeout=args.timeout,
            max_tokens=RESPONSE_MAX_TOKENS,
        )
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print()

    try:
        ensure_nonempty_report(report, phase="diagnose")
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.out, report)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

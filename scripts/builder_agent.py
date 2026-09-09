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
    write_report,
    stream_chat,
)

DEFAULT_MAX_STEPS = 80

# Bounds a single turn's completion length -- see stream_chat's docstring
# for the incident this exists to shorten, not prevent. Raised from the
# original 12,000 after a second incident: a model that cannot disable its
# own reasoning (no instruct/off mode, reasoning is always on) burned the
# entire 12,000-token budget thinking at a modest 31K-token prompt and was
# truncated before producing anything -- an empty response that run_agent
# then mistook for legitimate completion (see the empty-response check
# below, which is the other half of that fix). 24,000 still firmly bounds
# the original runaway case (the 798-second, 17,329-token turn this cap
# exists to shorten would still get cut well before reaching double that
# length) while giving an always-reasoning model enough room to actually
# finish a thought before acting.
MAX_COMPLETION_TOKENS = 24_000

# After this many consecutive tool-call parse failures, the retry message
# stops being the generic "try again" and starts naming the fix directly.
# The system prompt already tells the model to split large writes up front;
# this is for when it doesn't take that advice and just keeps re-attempting
# the same oversized call -- observed live: three straight JSON-escaping
# failures on one write_file, each answered with the same generic retry
# text, before the fourth attempt spiraled into the runaway generation
# above instead of ever trying something different.
CONSECUTIVE_FAILURES_BEFORE_ESCALATION = 2
# 40 wasn't enough in a live run: Builder's own final report used an
# explicit [x]/[ ] checklist showing it knew devices.py/qc.py/serve.py/tests
# were unfinished, but signaled done anyway instead of continuing. Whether
# that was step-budget pressure or something else isn't confirmed (no
# transcript was kept), but raising the ceiling is the cheap, low-risk
# thing to try before touching reasoning="off", which is deliberate (see
# the role-default comments in modeldeck/state.py) and shouldn't change
# without isolated testing.
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
```tool
{{"name": "ask_question", "arguments": {{"question": "the decision you need", "options": ["A", "B"]}}}}
```

ask_question pauses this session and puts your question in front of the \
person who launched this run -- "options" is optional (omit it for a \
free-text answer). Use it sparingly, only when the plan is genuinely silent \
on a decision AND guessing wrong would be costly or hard to undo (e.g. an \
irreversible destructive command, a choice between two incompatible designs \
the plan didn't resolve). For anything else -- naming, minor structure, \
small ambiguities a competent engineer would just resolve -- use your best \
judgment and note the decision in your final report instead of asking. \
Every ask_question call costs the person's attention; don't spend it on \
things you can reasonably decide yourself.

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
next one.

Before you finish, you must actually RUN the tests with run_command and see \
them pass. Not read them, not reason about whether they would pass -- run \
them and look at the output. If the project has no obvious test command, \
run whatever does exercise the change (an import, a script, a build) and \
say in your report what you ran and what it proved. Tests you wrote but \
never executed are the single most common way a run like this fails: they \
are written against the code you *intended*, and the mismatches are exactly \
the ones you cannot see by re-reading your own work. When a test fails, fix \
it and run again -- a failing suite is not something to hand off with an \
explanation attached.

When the plan is implemented and you have seen the tests pass, reply with \
plain text and no ```tool block: a summary of what you did, files changed, \
the verification commands you ran with their actual output, and any \
discrepancies from the plan. If you are ending without a green test run, \
say so in the first line of that report and state exactly what is failing \
-- do not describe the work as verified, complete, or passing when you have \
not watched it pass. That plain-text reply ends the session, so do not send \
it until you are actually done.

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
    model_alias: str = "builder",
) -> tuple[str, int, list[str]]:
    """Returns (final_report_text, steps_taken, touched_files) -- touched_files
    is every relative path passed to write_file/append_file this run (deduped,
    sorted), for the Auditor to prioritize instead of treating the whole tree
    as equally likely to matter.

    model_alias exists so renovator_agent.py can reuse this exact loop on a
    different backend: Builder and Renovator are deliberately on different
    models (fast MoE for the bulk pass, dense for expert cleanup -- see the
    role comments in modeldeck/state.py), which they can't be while sharing
    one alias."""
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(plan=plan)
    user_intro = "Begin." if not task else f"Begin. Additional note from the requester:\n{task}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_intro},
    ]
    touched: set[str] = set()
    consecutive_parse_failures = 0

    for step in range(1, max_steps + 1):
        on_chunk(f"\n--- step {step} ---\n")
        response = stream_chat(
            model_alias, messages, on_chunk=on_chunk, router_url=router_url, timeout=timeout,
            max_tokens=MAX_COMPLETION_TOKENS,
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
            consecutive_parse_failures += 1
            on_chunk(f"\n[invalid tool call ({consecutive_parse_failures} in a row): {exc}]\n")
            if consecutive_parse_failures >= CONSECUTIVE_FAILURES_BEFORE_ESCALATION:
                # Repeating the same generic nudge did not work last time,
                # so don't repeat it a third time -- name the fix directly.
                # This is also very likely truncation from MAX_COMPLETION_TOKENS
                # on a call that was too large to begin with, which is the
                # same fix as the JSON-escaping case: make it smaller.
                feedback = (
                    f"That's {consecutive_parse_failures} failed attempts in a row on this same "
                    f"call: {exc}. Stop retrying it as-is. Whatever you're writing is too large "
                    "for one response -- split it now: a write_file call for roughly the first "
                    "half of the content, then one or more append_file calls for the rest, each "
                    "small enough to comfortably fit in a single response on its own."
                )
            else:
                feedback = f"Your last response was invalid: {exc}. Try again."
            messages.append({"role": "user", "content": feedback})
            continue

        consecutive_parse_failures = 0

        if call is None:
            if not response.strip():
                # A response with no ```tool block AND no actual text is
                # never a legitimate "I'm done" signal -- the system prompt
                # requires the final reply to be a real summary. An empty
                # response means the completion-token cap cut the turn off
                # before the model produced anything at all (seen live: a
                # model that cannot disable its own reasoning burned the
                # entire MAX_COMPLETION_TOKENS budget thinking, at a modest
                # 31K-token prompt, and never got to emit a tool call or a
                # report). Treating that as completion silently wrote a
                # blank report and ended the run having touched no files.
                on_chunk(
                    "\n[empty response -- likely cut off mid-thought by the "
                    "completion-token cap before producing anything; retrying]\n"
                )
                messages.append({
                    "role": "user",
                    "content": "Your last response was empty -- it looks like you were cut off "
                    "before producing any output, likely mid-reasoning. Get to the point sooner: "
                    "take the next concrete action (a ```tool call) or, if you are actually done, "
                    "give your plain-text summary directly without a long lead-in.",
                })
                continue
            return response, step, sorted(touched)

        if call["name"] == "ask_question":
            # Handled here, not in builder_tools.run_tool, because answering
            # requires blocking on real input -- print a marker line the
            # caller can recognize (a human at a terminal, or the GUI
            # watching this subprocess's stdout to pop a dialog and write
            # the answer to stdin) and read one line back.
            question = str(call["arguments"].get("question") or "")
            options = call["arguments"].get("options")
            marker = json.dumps({
                "question": question,
                "options": options if isinstance(options, list) else None,
            })
            on_chunk(f"\n[ASK_QUESTION] {marker}\n")
            answer = input().strip()
            on_chunk(f"[answered: {answer}]\n")
            messages.append({
                "role": "user",
                "content": f"The person running this session answered: {answer}",
            })
            continue

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
    write_report(args.out, report)
    print(f"Wrote {args.out}")

    if touched:
        touched_path = resolve_ai_path(args.path, "builder-touched-files.json")
        touched_path.write_text(json.dumps(touched))
        print(f"Wrote {touched_path} ({len(touched)} file(s) touched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

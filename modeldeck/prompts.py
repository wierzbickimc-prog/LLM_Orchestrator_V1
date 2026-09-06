from __future__ import annotations

# Shared by the GUI (displayed/copyable per phase) and the router (spliced
# into every request's system message for the active phase, so the
# requirements hold for the whole task instead of only the pasted-in first
# message). Keep this free of GUI imports so the router doesn't have to
# pull in PySide6.

PROMPTS: dict[str, tuple[str, str]] = {
    "scout": (
        ".ai/scout-report.md",
        "Inspect this repository for the requested change. Do not edit application code.\n\n"
        "Start `.ai/scout-report.md` early — after your first few reads, as soon as you have "
        "anything concrete to record — and append to it as you keep investigating. Do not hold "
        "the full investigation in context and write it all at once at the end; treat the file "
        "as your running notes, not a final deliverable assembled only once.\n\n"
        "Write `.ai/scout-report.md` containing:\n"
        "- relevant files, symbols, and execution paths with exact references;\n"
        "- existing tests and commands;\n"
        "- observed behavior separated from hypotheses;\n"
        "- risks, unknowns, and questions for the Planner.\n\n"
        "Verify every factual claim against the repository. Include as much detail as the "
        "Planner needs, while omitting unrelated inventory and repeated observations.\n\n"
        "Budget your exploration: if you have made roughly 15 tool calls since the last time "
        "you appended to `.ai/scout-report.md`, stop investigating and append what you have "
        "now. List anything still unresolved as open questions for the Planner rather than "
        "continuing to search for them — an incomplete report that exists beats a thorough one "
        "that never gets written.\n\n"
        "Write the report using your editor's native file-editing tool only. Never use terminal "
        "commands, heredocs, shell redirection, or Python to write it — including as a "
        "fallback after the native tool appears to fail. If a section is long, write it across "
        "multiple sequential native-editor writes/appends rather than switching mechanisms.\n\n"
        "If native file editing is unavailable, fails, or a reread shows the file is missing, "
        "stale, or garbled, do not retry with a different writing mechanism. Print whatever the "
        "report holds so far in your final response instead and stop there.",
    ),
    "planner": (
        ".ai/implementation-plan.md",
        "Read `.ai/scout-report.md` and independently inspect the referenced source. Do not "
        "implement the change.\n\n"
        "Start `.ai/implementation-plan.md` early and append to it as you inspect each "
        "referenced file, rather than holding the whole plan in context until one final write.\n\n"
        "Write `.ai/implementation-plan.md` as the authoritative Builder handoff. Include:\n"
        "- objective and verified current behavior;\n"
        "- exact files and behavioral contracts to change;\n"
        "- ordered implementation steps;\n"
        "- explicit scope boundaries and safety constraints;\n"
        "- tests and acceptance criteria;\n"
        "- discrepancies or unsupported Scout claims.\n\n"
        "Budget your inspection: if you have made roughly 10 tool calls since the last time you "
        "appended to `.ai/implementation-plan.md`, stop and append what you have now, noting "
        "anything unverified as an open question rather than continuing to chase it.\n\n"
        "Write the plan using your editor's native file-editing tool only. Never use terminal "
        "commands, heredocs, shell redirection, or Python to write it — including as a fallback "
        "after the native tool appears to fail. If a section is long, write it across multiple "
        "sequential native-editor writes/appends rather than switching mechanisms.\n\n"
        "If native file editing is unavailable, fails, or a reread shows the file is missing, "
        "stale, or garbled, do not retry with a different writing mechanism. Print whatever the "
        "plan holds so far in your final response instead and stop there.",
    ),
    "builder": (
        ".ai/builder-report.md",
        "Read `.ai/implementation-plan.md`, verify its assumptions against the source, and "
        "implement only that plan.\n\n"
        "Start `.ai/builder-report.md` early and append to it as each part of the plan lands — "
        "a completed file change, a test run — rather than holding the summary until everything "
        "is done. If a step turns out to conflict with the plan, append that discrepancy when "
        "you find it, not from memory afterward.\n\n"
        "Run the specified tests. Write `.ai/builder-report.md` containing:\n"
        "- files changed and behavioral result;\n"
        "- exact verification commands and results;\n"
        "- discrepancies from the plan;\n"
        "- remaining concerns without implementing out-of-scope changes.\n\n"
        "Budget your work: if you have made roughly 15 tool calls since the last time you "
        "appended to `.ai/builder-report.md`, stop and append your status now, noting what's "
        "still incomplete, rather than continuing silently.\n\n"
        "Write the report using your editor's native file-editing tool only. Never use terminal "
        "commands, heredocs, shell redirection, or Python to write it — including as a fallback "
        "after the native tool appears to fail. (This is about `.ai/builder-report.md` itself; "
        "use whatever tools the plan calls for to edit application code.) If a section is long, "
        "write it across multiple sequential native-editor writes/appends rather than switching "
        "mechanisms.\n\n"
        "If native file editing is unavailable, fails, or a reread shows the file is missing, "
        "stale, or garbled, do not retry with a different writing mechanism. Print whatever the "
        "report holds so far in your final response instead and stop there.",
    ),
    "auditor": (
        ".ai/audit-report.md",
        "Audit the implementation against `.ai/implementation-plan.md`. Inspect the complete "
        "diff and run relevant tests. Do not edit application code.\n\n"
        "Start `.ai/audit-report.md` early and append findings as you confirm each one, rather "
        "than holding the whole audit in context until one final write.\n\n"
        "Write `.ai/audit-report.md` with findings ordered by severity. For each finding include "
        "a file reference, impact, and evidence. Confirm plan compliance, test coverage, error "
        "handling, and unintended scope. If no defects are found, state that explicitly and list "
        "any residual test gaps.\n\n"
        "Budget your inspection: if you have made roughly 15 tool calls since the last time you "
        "appended to `.ai/audit-report.md`, stop and append what you have now. List anything "
        "still unverified as a residual gap rather than continuing to chase it — an incomplete "
        "audit that exists beats a thorough one that never gets written.\n\n"
        "Write the report using your editor's native file-editing tool only. Never use terminal "
        "commands, heredocs, shell redirection, or Python to write it — including as a fallback "
        "after the native tool appears to fail. If a section is long, write it across multiple "
        "sequential native-editor writes/appends rather than switching mechanisms.\n\n"
        "If native file editing is unavailable, fails, or a reread shows the file is missing, "
        "stale, or garbled, do not retry with a different writing mechanism. Print whatever the "
        "report holds so far in your final response instead and stop there.",
    ),
}


def effective_prompt(phase: str, state: dict) -> str | None:
    """The instructions text actually spliced into a request for this phase
    (see router.main.inject_phase_instructions): an operator-edited override
    from state.json's "prompt_overrides" if one has been saved, else the
    built-in default above. Returns None for an unknown phase.

    This only affects a chat client hitting the router's phase-
    injected `local` alias -- scripts/*_report.py and renovator_agent.py
    carry their own complete system prompts and send
    X-Model-Deck-Skip-Injection, so they never reach this path at all."""
    override = (state.get("prompt_overrides") or {}).get(phase)
    if isinstance(override, str) and override.strip():
        return override
    entry = PROMPTS.get(phase)
    return entry[1] if entry else None

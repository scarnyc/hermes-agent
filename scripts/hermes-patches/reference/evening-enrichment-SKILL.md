---
name: evening-enrichment
description: Evening peer of daily-task-list. Reports the ENRICH summary emitted by ~/.hermes/scripts/evening_enrichment.py — pass-through formatting only; the pre-job script is the source of truth.
version: 1.0.0
author: Hermes Agent
license: MIT
prerequisites:
  commands: []
metadata:
  hermes:
    tags: [TaskList, Productivity, Enrichment, Peer-MOL246]
---

# Evening Enrichment — Pass-Through Reporter

The pre-job script `~/.hermes/scripts/evening_enrichment.py` (MOL-246) pulls Calendar / Gmail / Granola signals and either previews (dry-run) or applies enrichment bullets into today's task-list file at `# NOW` / `# This Week` anchors. **All real work is in the script** — your role is a thin reporter.

This skill runs in the cron environment at 21:00 ET. Morning's `daily-task-list` skill (06:30 ET) then carries `[ ]` bullets forward via the existing preserve-then-mutate drop pass — zero coordination required between the two jobs.

## Persona

Read:
- `~/.hermes/SOUL.md` — identity
- `~/.hermes/memories/USER.md` — who you're helping

Do NOT read `~/.hermes/memories/TASKS.md` — it is archive-frozen. Canonical task source is today's file under `~/.hermes/notes/obsidian/task-list/`.

## Pipeline

### Step 1: Parse the Script Output block

The scheduler injects the pre-job script's stdout above as `## Script Output`. It will contain exactly one of:

- **Success line** — `ENRICH: N items (cal=C gmail=G granola=R; filtered=F deduped)` followed optionally by a unified-diff block. This is the dry-run or apply summary.
- **Abort line** — `ABORT: <reason>` (exit 2). Means the bridge check, today-file existence guard, or equivalent pre-flight failed.
- **DEGRADED banners** — zero or more lines like `DEGRADED_CALENDAR: timeout` / `DEGRADED_GMAIL: unparseable-line` / `DEGRADED_GRANOLA: auth` preceding the ENRICH line. Each indicates one extractor failed softly and returned zero items.

### Step 2: Final output

Emit a **single self-contained report** built from the Script Output:

1. Begin with the literal `ENRICH:` line from the Script Output, verbatim.
2. If any `DEGRADED_<SRC>:` banners appeared, list them after the ENRICH line on their own lines, verbatim.
3. If the Script Output starts with `ABORT:`, emit that line verbatim and stop. Do NOT fabricate a success line.
4. If the Script Output contains a unified-diff block (lines starting with `---`, `+++`, `@@`, `+-`, `- `), append the literal text `PREVIEW DIFF:` followed by the diff exactly as emitted — do not paraphrase, trim, or rewrite bullet text.
5. If `N items == 0` and no DEGRADED banners fired, append one plain-text sentence: `No new signals to enrich tonight.` — no recommendations, no speculation.
6. If `N items > 0` and the orchestrator is in dry-run mode (diff present), append: `Review the diff above; if sane, a follow-up ticket flips enrichment.auto_apply.`

**Do not**:
- Fabricate counts (e.g., "Dropped 1 completed items" is a morning-skill artifact; evening enrichment never drops items).
- Invoke `memory_observe`, `send_message`, or any tool — this skill is read-only + report-only.
- Add commentary about what items "should" exist — the script is authoritative about what ran.
- Re-execute extractors or re-parse Gmail/Calendar yourself — that duplicates the pre-job script's work.

The user has provided the following instruction alongside the skill invocation: [SYSTEM: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically delivered to the user — do NOT use send_message or try to deliver the output yourself. Just produce your report/output as your final response and the system handles the rest. SILENT: If the Script Output contains `ENRICH: 0 items` AND zero `DEGRADED_` banners AND zero diff content, respond with exactly `[SILENT]` (nothing else) to suppress delivery. Otherwise, report normally.]

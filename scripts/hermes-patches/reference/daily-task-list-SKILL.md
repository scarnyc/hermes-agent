---
name: daily-task-list
description: Writes today's canonical task list to the Obsidian vault. Preserves yesterday's unfinished items byte-faithfully via a deterministic pre-job script; drops items completed yesterday. Peer evening job (MOL-246) at 21:00 ET injects Calendar/Gmail/Granola action items into today's file; the morning pass then carries them into tomorrow's file.
version: 3.0.0
author: Hermes Agent
license: MIT
prerequisites:
  commands: [diff]
metadata:
  hermes:
    tags: [TaskList, Productivity, DailyOversight]
---

# Daily Task List — Vault-Backed Oversight + Feedback Loop

The morning briefing carries yesterday's task list forward and drops items the user completed (`[x]`) yesterday. Body composition is deterministic and handled by a pre-job script before this prompt runs; your only jobs are the correction-signal feedback loop and a single-line status.

This skill runs in the cron environment. The preserve-then-mutate script handles body composition before this prompt runs. Your role is to read the `## Script Output` block, record the day's diff-vs-snapshot correction signal to memory, and emit a single-line status.

## Persona

Read your persona:
- `~/.hermes/SOUL.md` — identity and behavioral guidelines
- `~/.hermes/memories/USER.md` — who you're helping

Do NOT read `~/.hermes/memories/TASKS.md` — it is archive-frozen and no longer the canonical task source. The canonical source is today's file under `~/.hermes/notes/obsidian/task-list/`.

Adopt the persona from SOUL.md. Address the user as specified in USER.md.

## Pipeline

The historical Steps 0, 1, 3, 5 from v2.1.0 are now handled by `~/.hermes/scripts/compose_task_list.py` (date resolution, bridge integrity check, body composition, atomic write). The LLM-facing pipeline reduces to three named steps.

### Diff feedback loop

Compute yesterday's paths from the current date (America/New_York), then diff the snapshot against the user's edited version:

```bash
YESTERDAY=~/.hermes/notes/obsidian/task-list/"$(TZ=America/New_York date -v-1d +'%b %-d %Y').md"
YESTERDAY_SNAP=~/.hermes/cron/output/daily-task-list/"$(TZ=America/New_York date -v-1d +'%b %-d %Y')-original.md"
if [ -f "$YESTERDAY_SNAP" ] && [ -f "$YESTERDAY" ]; then
  diff -u "$YESTERDAY_SNAP" "$YESTERDAY" > /tmp/daily-task-diff.txt || true
fi
```

If the diff file exists and is non-empty, store its content via `memory_observe` (Category: `project`, Title: `Task list corrections <date>`). If length > 9000, truncate middle. If the `memory_observe` tool is unavailable in your environment, do NOT attempt to invoke it via terminal workarounds (e.g., `hermes chat`); immediately append `MEMORY: DEGRADED` to the Step output.

If either `$YESTERDAY_SNAP` or `$YESTERDAY` does not exist (e.g. first-ever run, or a day the cron was paused), skip the diff and record `Corrections observed: skipped` in the final status.

### Confirm composition

Body composition is handled deterministically by `~/.hermes/scripts/compose_task_list.py`. Do not attempt to compose the file in this prompt.

The cron scheduler runs the script BEFORE your prompt loads; its stdout is injected above as `## Script Output`. Verify it contains a `COMPOSED: <path>` line. If it does, proceed to Final output. If it does not (or the block contains only an `ABORT:` stderr line), stop and report the error verbatim in Final output.

### Final output

Return a single line confirming the outcome:
`DAILY BRIEFING -- wrote <filename>. Dropped <N> completed items. Corrections observed: <yes|no|skipped|DEGRADED>.`

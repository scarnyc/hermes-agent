---
name: comprehensive-update
description: Morning briefing pipeline. Gathers Gmail, Calendar, Jira, Granola meetings, Drive activity, and news. Synthesizes cross-source briefing and sends via Telegram.
version: 1.3.0
author: Hermes Agent
license: MIT
prerequisites:
  commands: [gws, jira]
metadata:
  hermes:
    tags: [Briefing, Productivity, Morning Update, Cross-source]
---

# Comprehensive Update — Morning Briefing Pipeline

You are a productivity agent running the daily comprehensive update.
Gather data from all available sources and produce a structured briefing with cross-source analysis.

## Persona

Before responding, read your persona and task files:
- `~/.hermes/SOUL.md` — your identity and behavioral guidelines
- `~/.hermes/memories/USER.md` — who you're helping (address them as specified here)
- Today's canonical task list (see Step 1 for the fallback chain). Do NOT read `~/.hermes/memories/TASKS.md` — it is archive-frozen and no longer the canonical source.

Adopt the persona defined in SOUL.md. Address the user as specified in USER.md.

## Pipeline (11 Steps)

### Step 0: Infrastructure Health Check

Before gathering data, verify the environment is healthy. Check and report:

1. Workspace files mounted:
   - `ls ~/.hermes/SOUL.md ~/.hermes/memories/USER.md`
   - If ANY are missing, note "DEGRADED: persona files missing"
   - Today's task list is verified separately in Step 1 (fallback chain).

2. CLI tools available:
   - `which gws jira mcporter` -- check if these resolve
   - If `mcporter` is missing, Granola tools might be unavailable. Note which tools are missing.

3. Logic/Memory Availability:
   - Check if `memory_observe` or `session_search` are in your `available_tools` list.
   - If `memory_observe` is missing, note "DEGRADED: memory storage unavailable".

4. Task list tripwire check (daily-task-list skill):
   - `ls ~/.hermes/cron/output/daily-task-list/ABORTED-*.marker 2>/dev/null` — check for any abort tripwires from the 06:30 run.
   - If a tripwire exists for today or yesterday, read its contents (`cat` the file) and emit a HIGH-VISIBILITY first line of the briefing (BEFORE INFRA): `>>> TASK LIST STALE -- daily-task-list ABORTED <date> — symlink bridge broken. Re-run migration per ABORTED-*.marker. <<<`
   - Do NOT suppress this. It escalates above every other briefing content.
   - **If NO tripwire marker exists, emit NOTHING for this check. Absence = healthy. Do NOT surface `NO_TRIPWIRE` in the INFRA line — it is not a DEGRADED signal.**

5. Cron Health (MANDATORY — not optional):
   - Run `python3 ~/.hermes/hermes-agent/cron_health.py` via the `terminal` tool and include its stdout verbatim in the briefing under a `## CRON HEALTH` section placed after INFRA and before EMAIL.
   - Under sandbox-exec the `## Launchd Agents` section is elided entirely (P71/MOL-250) — absence of the section is EXPECTED under sandbox, NOT a degradation. The `## Cron Jobs` table above is the load-bearing signal.
   - If you skip this step for any reason, emit `CRON_HEALTH_SKIPPED` (NOT `CRON_HEALTH_FAILED`) in the INFRA line and briefly justify why.

6. News Recency:
   - `web_search` and `blogwatcher-cli` queries MUST strictly prioritize the last 7 days. Ignore archival/historical results (>90 days) unless critically relevant.
   - Partial `blogwatcher-cli scan` feed errors (HTTP 404 / timeouts on individual feeds that have moved or paused upstream) are CONTENT limitations, NOT infra failures. Surface them inside the `FROM YOUR FEEDS` subsection as `N/M feeds OK`. Do NOT emit `BLOGS_FAILED` in the INFRA line unless the `blogwatcher-cli` binary itself is missing OR every tracked feed errors.

Include an INFRA HEALTH line at the top of the final briefing output (below the tripwire line if present):
- `INFRA: ALL GREEN` if everything checks out
- `INFRA: DEGRADED -- [list what failed]` **ONLY for ACTIVE failures**: missing CLI binary, auth error, service unreachable, subprocess crash. Do NOT emit DEGRADED for: absent warning markers (healthy state), partial content fetches (individual feed 404s), or expected sandbox-exec restrictions (launchctl mach-lookup denial).

### Step 1: Read Workspace & Sync Tasks

Read SOUL.md and USER.md. Then resolve today's canonical task list via the fallback chain:

```bash
python3 - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
tz = ZoneInfo("America/New_York")
today = datetime.now(tz).date()
base = os.path.expanduser("~/.hermes/notes/obsidian/task-list")
for delta, label in [(0, "TODAY"), (1, "YESTERDAY"), (2, "DAYBEFORE")]:
    d = today - timedelta(days=delta)
    p = f"{base}/{d.strftime('%b %-d %Y')}.md"
    if os.path.exists(p):
        print(f"TASK_LIST={p}")
        print(f"TASK_LIST_AGE={label}")
        break
else:
    print("TASK_LIST=")
    print("TASK_LIST_AGE=MISSING")
PY
```

- If `TASK_LIST` resolves to today's file (`TASK_LIST_AGE=TODAY`), read that file as the canonical source.
- If `TASK_LIST_AGE` is `YESTERDAY`, emit a HIGH-VISIBILITY line BEFORE the briefing content: `>>> TASK LIST STALE: using YESTERDAY's file (Apr N). Daily cron may have failed at 06:30. Run 'hermes cron run <daily-task-list-id>' manually and check ABORTED-*.marker under cron/output/daily-task-list/. <<<`. Continue with that stale file.
- If `TASK_LIST_AGE` is `DAYBEFORE`, escalate further: same high-visibility line but with `TASK LIST STALE 2+ DAYS -- daily-task-list cron is failing`. Continue with day-before content.
- If `TASK_LIST_AGE` is `MISSING`, emit `>>> NO RECENT TASK LIST -- daily-task-list has not produced a file in 3+ days. Task context is unavailable for today's briefing. <<<` and continue without task context.
- Stale or missing status MUST appear above the standard briefing, not buried in a mid-briefing INFRA line. User reads top-down.
- NEVER read `~/.hermes/memories/TASKS.md`. It is archive-frozen.

Parse the resolved file for active tasks, blocked items, and priorities. This context informs triage and analysis in later steps.

### Step 2: JIRA Highlights

Use the `terminal` tool to query recent project activity:
```bash
jira issue list --updated -1d --order-by updated --reverse --plain
```

Note: jira-cli's `-q` flag does not accept `ORDER BY` clauses — use the dedicated `--order-by` + `--reverse` flags instead. Project is implicit from the configured default.
For important issues (blockers, items assigned to user, status changes), get details:
```bash
jira issue view MOL-123
```

### Step 3: Granola Meetings

List this week's meetings using the Granola tools:
```
list_granola_meetings(time_range="this_week")
```

For each meeting with action items or key decisions, get the full details:
```
get_granola_meeting(id="meeting-uuid", include_transcript=true)
```
*Pitfall: The `id` parameter strictly expects a UUID. Do not pass search strings (e.g., project names) into `get_granola_meeting`. Use `query_granola_meetings` for natural language searches.*

For follow-up topic queries, use natural language search:
```
query_granola_meetings(query="action items and decisions from this week")
```

Extract key decisions, action items, and commitments from each meeting.

If Granola is unavailable (auth error), note it and continue.

### Step 4: Gmail Pipeline

Use the `terminal` tool to fetch unread emails:
```bash
gws gmail +triage
```

Categorize results:
- **Urgent** — from known contacts, action-required, time-sensitive
- **FYI** — informational, status updates, CC'd threads
- **Newsletters** — automated digests, marketing, subscriptions

To read a specific email for more detail:
```bash
gws gmail +read --id MESSAGE_ID --format json
```

### Step 5: Calendar

Use the `terminal` tool to get today's agenda:
```bash
gws calendar +agenda
```

**Filtering rules:**
- **Skip VZ (Verizon) calendar entries entirely.** These display as blank/private (freeBusyReader only — no titles, no attendees). They are noise.
- Only include meetings with actual titles OR external attendees (non-VZ, non-self).
- For each included meeting, note: time, title, attendees (external ones highlighted), and whether prep is needed.

Flag: conflicts, back-to-back stretches (3+ consecutive), and meetings where you're the organizer (you likely need slides/agenda).

### Step 6: News & Research

#### 6a: RSS Blog Feeds

Scan tracked blogs for new articles using blogwatcher-cli:
```bash
blogwatcher-cli scan
```

Then list any new (unread) articles:
```bash
blogwatcher-cli articles
```

Include new articles in the NEWS section under a "FROM YOUR FEEDS" subsection. After the briefing is delivered, mark them as read:
```bash
blogwatcher-cli read-all -y
```

Note: the installed binary is `blogwatcher-cli`, not `blogwatcher`. See Step 0.6 (News Recency) for partial-feed-failure polarity — individual 404s are NOT infra failures.

Reference: `references/wsj-feeds-status.md` — documents which WSJ RSS feeds are frozen and the web-search workaround for subscribers. The expansion to WSJ + NYT + world / business / markets coverage in 6c below was added under MOL-443.

If blogwatcher is not installed or no blogs are tracked, skip this substep and note "RSS: not configured" in the output. This is the ONLY condition that warrants `BLOGS_FAILED` in INFRA.

#### 6b: AI News Scout (Integration)

Search for today's AI news across 5 categories, deduplicate, and score by relevance to an AI Product Manager working on contact center AI and agentic systems.

1. "AI news today"
2. "agentic AI agents news"
3. "new LLM model release"
4. "enterprise AI deployment contact center"
5. "AI product management trends"

Filter results to only include those from the last 7 days. Include the top 5-8 most relevant stories in the NEWS section under a "TOP AI STORIES" subsection.

#### 6c: General News Scout (Top Headlines)

Search broader news beyond AI/ML — WSJ, NYT, world news, business, markets. Four queries:

1. `"top news today" site:wsj.com`
2. `"top headlines today" site:nytimes.com`
3. `"world news today"`
4. `"business markets today"`

The `web_search` tool accepts only `(query, limit)` — no recency parameter is forwarded. Inspect each result's date/freshness in the returned content and discard anything older than 24 hours. Daily brief should be daily news, not weekly.

Cap output at **6 stories total** across all 4 queries combined. Deduplicate by domain + topic — if WSJ and NYT both cover the same Fed decision, keep one. Avoid 4× "same story" results.

Render under a **TOP HEADLINES** subsection of the NEWS block in the output template, between FROM YOUR FEEDS and TOP AI STORIES. One line per story: headline, source, link.

WSJ subscriber note: WSJ RSS feeds at `feeds.a.dj.com` are frozen as of Jan 2025 (see `references/wsj-feeds-status.md`). Web-search results land on wsj.com URLs; subscriber cookies handle paywall click-through. Headlines + URLs only — don't try to extract paywalled body content.

### Step 7: Drive Activity

Use the `terminal` tool to check recently modified files:
```bash
gws drive files list --params '{"q":"modifiedTime > '\''YESTERDAY_ISO'\''","pageSize":10}' --format table
```

Replace `YESTERDAY_ISO` with the actual ISO date for yesterday (e.g., `2026-04-06T00:00:00`).

**Do NOT list every file.** Only surface notable changes:
- New docs/slides/spreadsheets created (not auto-generated or temp files)
- Files modified by collaborators (not self)
- Files whose name contains an active JIRA key (matching `MOL-\d+`) or a substring of a today's-calendar meeting title
- Skip routine syncs, logs, and auto-saves — they're noise

If nothing notable changed, omit the FILES section entirely rather than reporting "no activity."

### Step 8: Triage & Analysis

Synthesize all collected data. Perform cross-source analysis:
- Cross-reference meeting action items with JIRA tickets and today's task list (resolved in Step 1)
- Identify blocked items and items needing follow-up
- Flag upcoming deadlines (today and tomorrow)
- Detect patterns across sources (same topic in email + meeting + JIRA)
- Highlight items that need the user's attention today
- Note discrepancies (e.g., JIRA ticket marked done but meeting notes show open action items)

**Surface task-list items — this is required, not optional.** The file resolved in Step 1 (`~/.hermes/notes/obsidian/task-list/<MMM D YYYY>.md`) is the canonical to-do list. Render the TASKS section in the output template with the structure below, even on light days:
- **NOW:** every unchecked (`[ ]`) item under the `### NOW` heading. Preserve the existing parent → child indentation so context isn't lost.
- **This Week:** top 3-5 unchecked items from the `## This Week` heading, prioritized by overlap with today's calendar / active JIRA / overdue email.
- **Cross-references:** if a NOW or This Week item shares vocabulary with a meeting / email / JIRA ticket already surfaced above, append `(see CALENDAR / EMAIL / JIRA above)`.
- **Don't drop the section.** If the resolved task-list file is empty or all items are checked, emit `TASKS -- no open items in today's list` so the reader knows the source was consulted. NEVER omit the TASKS section silently.

**Proactive day prep — scan the calendar for these triggers:**

- **Interview** (title contains "interview", "panel", "onsite", "loop", or external attendees — anyone with an email not on `verizon.com` or `aipmbydesign.com`): Include a dedicated INTERVIEW PREP section with:
  - Who you're meeting (name, company, role if available)
  - What's known about them (from emails, Granola prep notes, or prior threads)
  - Study guide: key topics to prepare, questions to ask, your talking points
  - A genuine "Good luck!" sign-off

- **Presentation / deck** (title contains "presentation", "deck", "slides", "demo", "review", or you're the organizer): Include a PRESENTATION reminder with:
  - Which deck/slides are needed (search Drive for matching names if unclear)
  - When it was last modified (is it stale?)
  - "Your slide deck is ready" or "Slides may need updating — last touched [date]"

- **External client/vendor call** (non-VZ attendees): Note who's external and any recent email context with them.

If nothing triggers, skip these sections — don't force them.

### Step 9: Follow-Up Scheduling

For items needing follow-up (deadlines within 48h, stale tickets, unresolved meeting action items), note them in the output under the FOLLOW-UPS section. Include:
- What needs follow-up and why
- Suggested timing
- Source (which step identified the item)

### Step 10: Output

Attempt to store the briefing via memory tool for long-term recall. As of April 2026, memory_observe works in the cron environment. If it fails, skip gracefully and deliver the briefing without storing.

```
memory_observe(title="Daily Briefing YYYY-MM-DD", content="[briefing summary]", category="briefing")
```

Then deliver the structured briefing as your final response.

**Output format -- emit each line LEFT-JUSTIFIED with NO leading indentation, NO surrounding code fences, NO markdown bold/asterisks. The literal `[brackets]` are placeholder slots for the agent to fill in with content. The horizontal-rule lines below are visual delimiters showing where the template begins and ends — do NOT include the rules in your output:**

----- BEGIN BRIEFING TEMPLATE -----
GOOD MORNING CHIEF

INFRA: [ALL GREEN or DEGRADED -- details]

EMAIL -- [count] unread
- [urgent items first, then FYI -- one line each]

CALENDAR -- [today's events with times, conflicts flagged. Skip blank/untitled VZ entries.]

MEETINGS -- [meeting summaries, key decisions, action items]

INTERVIEW PREP [if applicable]
- [Who you're meeting + what's known about them, key topics, questions to ask, your talking points, "Good luck!"]

PRESENTATION [if applicable]
- [Which deck, last modified, readiness check]

JIRA -- [recent updates, blockers, status changes]

NEWS
  FROM YOUR FEEDS -- [new blog posts from tracked RSS feeds, one line each]
  TOP HEADLINES -- [WSJ / NYT / world / politics / business / markets — last 24h, max 6 dedup'd]
  TOP AI STORIES -- [top AI/tech stories with one-line summaries]

FILES [if applicable]
- [notable Drive changes only; omit the entire FILES heading + bullets when nothing notable]

TASKS
  NOW -- [every unchecked item from the `### NOW` heading of today's task-list file, preserving indentation; cross-reference inline where relevant]
  THIS WEEK -- [3-5 highest-priority unchecked items from `## This Week`, ranked by overlap with today's calendar / JIRA / email]
  NEW -- [items created today, if any]

FOLLOW-UPS -- [items needing follow-up, timing, source]

ANALYSIS -- [cross-source insights, patterns, recommendations for today. This is the most important section — synthesize, don't just list. What should Chief actually do today?]
----- END BRIEFING TEMPLATE -----

Keep the briefing concise but actionable. Lead with what matters most today.

## Important Notes

- Use `terminal` tool for all CLI commands (`gws`, `jira`, `blogwatcher-cli`)
- Use `list_granola_meetings`, `get_granola_meeting`, `query_granola_meetings` for meeting notes. Do NOT fallback to `mcporter` if these are missing in cron, as `mcporter` does not have Granola configured in that environment.
- Use `web_search` for news (not brave_search)
- If `memory_observe` is available, use it to store the briefing for long-term recall. If not, don't attempt it.
- Output discipline (HARD CONSTRAINTS -- Telegram delivery is automatic, the system handles the rest):
  - Emit the briefing as your sole final response. Do NOT preface it. Do NOT begin with "I need to...", "I will...", "Here is...", "Let me...", "I'll...", or any meta-narration about what you are about to output.
  - No code fences (no triple backticks anywhere), no markdown, no bold, no asterisks. ALL CAPS for section headers, plain dashes for bullets.
  - Emit the briefing EXACTLY ONCE. Never draft a copy and then repeat it. Never write "I will output exactly this" followed by another copy.
  - If you catch yourself about to write meta-narration, stop and just write the briefing.
- Your final response IS the briefing -- nothing before, nothing after.
- All Granola and gws read operations are auto-approved by Rampart
- Jira read operations are auto-approved; write operations require human approval
- If any source fails (auth error, timeout, tool missing), note it in the output and continue with remaining sources -- never let one failure block the entire briefing
- `gws` Format Pitfall: `gws` subcommands are not unified on formats. Defaulting to `text` format for `+read` causes a fallback warning string. Explicitly use `--format json` for reading emails, while `list` commands support `table`.
- **Rampart Security Pitfall**: Never use `python -c "..."` or `bash -c "..."` in terminal commands, as Rampart's policy engine explicitly blocks the `-c` script execution flags. Write temporary files using heredocs instead (e.g., `cat > temp.py <<'EOF'` followed by `python3 temp.py`).
- Cron jobs run autonomously -- do not ask questions or request clarification. Make reasonable defaults and note assumptions.

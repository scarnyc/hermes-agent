# Hermes Runtime Patches

Patches applied via cherry-pick from upstream (NousResearch/hermes-agent). 
Each entry records the upstream commit, our local commit, and what it does.

DO NOT run `hermes update` — it wipes these patches via git pull/reset.
Use `git cherry-pick <sha>` for future upstream integration.

## Active Patches

### P177 — Upstream absorption: extract message sanitization to agent/message_sanitization.py
- **Cherry-picked:** 2026-05-19 (MOL-597 modular refactor batch — 1/12)
- **Upstream:** `885d1242a` refactor(run_agent): extract message sanitization to agent/message_sanitization.py
- **Local:** `3c95c8810`
- **Files:** run_agent.py, agent/message_sanitization.py (NEW, 444 LOC)
- **Why:** Begins porting upstream's modular refactor of run_agent.py (was 15,195 lines). Extracts `_sanitize_surrogates` + sibling helpers into a dedicated module so future cherry-picks no longer fight against the monolith.
- **Conflict:** Resolved by accepting upstream forwarder; no local-patch overlap on this range.

### P178 — Upstream absorption: extract tool-dispatch helpers to agent/tool_dispatch_helpers.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 2/12)
- **Upstream:** `59f1c0f0b` refactor(run_agent): extract tool-dispatch helpers to agent/tool_dispatch_helpers.py
- **Local:** `b7883e425`
- **Files:** run_agent.py, agent/tool_dispatch_helpers.py (NEW, 336 LOC)
- **Why:** Extracts `_is_multimodal_tool_result`, `_should_parallelize_tool_batch`, `_extract_file_mutation_targets`, `_trajectory_normalize_msg`, etc. Re-exported from run_agent for compatibility with tests + cli imports.
- **Conflict:** None.

### P179 — Upstream absorption: extract background memory/skill review to agent/background_review.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 3/12)
- **Upstream:** `1f6eb1738` refactor(run_agent): extract background memory/skill review to agent/background_review.py
- **Local:** `326a0feb3`
- **Files:** run_agent.py, agent/background_review.py (NEW, 582 LOC)
- **Why:** Moves the background-review subprocess driver — preserves P154 review_whitelist fingerprint (moved from run_agent.py to agent/background_review.py:231).
- **Conflict:** None — fingerprint pre/post snap confirmed clean move.

### P180 — Upstream absorption: extract context compression to agent/conversation_compression.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 4/12)
- **Upstream:** `5311d9959` refactor(run_agent): extract context compression to agent/conversation_compression.py
- **Local:** `aa1ded289`
- **Files:** run_agent.py, agent/conversation_compression.py (NEW, 592 LOC)
- **Why:** Extracts trajectory-compressor + sliding-window context management.
- **Conflict:** None.

### P181 — Upstream absorption: extract system-prompt builder to agent/system_prompt.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 5/12)
- **Upstream:** `2d2cd5e90` refactor(run_agent): extract system-prompt builder to agent/system_prompt.py
- **Local:** `3211a5dad`
- **Files:** run_agent.py, agent/system_prompt.py (NEW, 346 LOC)
- **Why:** Extracts system-prompt construction (SOUL/persona/skills assembly).
- **Conflict:** None.

### P182 — Upstream absorption: extract tool execution to agent/tool_executor.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 6/12)
- **Upstream:** `79559214a` refactor(run_agent): extract tool execution to agent/tool_executor.py
- **Local:** `a9c45ed4c`
- **Files:** run_agent.py, agent/tool_executor.py (NEW, 924 LOC)
- **Why:** Extracts concurrent + sequential tool-dispatch loop bodies.
- **Conflict:** None.

### P183 — Upstream absorption: extract stream diagnostics to agent/stream_diag.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 7/12)
- **Upstream:** `57f6762ca` refactor(run_agent): extract stream diagnostics to agent/stream_diag.py
- **Local:** `106718205`
- **Files:** run_agent.py, agent/stream_diag.py (NEW, 280 LOC)
- **Why:** Extracts stream-event diagnostics + token-usage normalization helpers.
- **Conflict:** None.

### P184 — Upstream absorption: extract chat-completion helpers to agent/chat_completion_helpers.py (part 1)
- **Cherry-picked:** 2026-05-19 (MOL-597 — 8/12)
- **Upstream:** `4b25619bc` refactor(run_agent): extract chat-completion helpers to agent/chat_completion_helpers.py
- **Local:** `6341c6782`
- **Files:** run_agent.py, agent/chat_completion_helpers.py (NEW)
- **Why:** Initial extraction of chat-completion helper functions (provider-specific request shaping, response normalization). Preserves P157 fingerprint `_is_deepseek = base_url_host_matches(agent.base_url, "api.deepseek.com")` at agent/chat_completion_helpers.py:322.
- **Conflict:** None — fingerprint snap confirmed clean move.

### P185 — Upstream absorption: extract streaming API caller to agent/chat_completion_helpers.py (part 2)
- **Cherry-picked:** 2026-05-19 (MOL-597 — 9/12)
- **Upstream:** `0430e71ec` refactor(run_agent): extract streaming API caller (893 LOC) to agent/chat_completion_helpers.py
- **Local:** `230e32697`
- **Files:** run_agent.py, agent/chat_completion_helpers.py (+893 LOC)
- **Why:** Adds the streaming dispatcher (`call_chat_completion_streaming`) to the helpers module. Total module now 2,078 LOC across the two parts.
- **Conflict:** None.

### P186 — Upstream absorption: extract run_conversation to agent/conversation_loop.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 10/12)
- **Upstream:** `053025238` refactor(run_agent): extract run_conversation to agent/conversation_loop.py
- **Local:** `be1c96e35` (amended for P161 carry-forward)
- **Files:** run_agent.py, agent/conversation_loop.py (NEW, 4,099 LOC), agent/auxiliary_client.py (set_runtime_main backport), agent/iteration_budget.py (NEW, copied from upstream), agent/process_bootstrap.py (NEW, copied from upstream)
- **Why:** Largest extraction — the 3,877-line `run_conversation` body becomes its own module. AIAgent.run_conversation is now a thin forwarder.
- **Conflict:** P161 (perf: `List[str].append` + `"".join` for truncated_response_parts) silently dropped by cherry-pick (upstream used older `str` form). Re-attached at 4 sites in agent/conversation_loop.py: declaration (line 467), accumulate calls (line 1360), join-and-strip (line 1381), final-response prepend (lines 3585-3587). Cascading imports satisfied by backporting `set_runtime_main`/`clear_runtime_main` to agent/auxiliary_client.py from upstream `3800972dd`, plus verbatim copy of agent/iteration_budget.py and agent/process_bootstrap.py from upstream `5f309ae68`.

### P187 — Upstream absorption: extract __init__ to agent/agent_init.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 11/12)
- **Upstream:** `9f408989c` refactor(run_agent): extract __init__ (1,381 LOC) to agent/agent_init.py
- **Local:** `a9cbbae21`
- **Files:** run_agent.py, agent/agent_init.py (NEW, 1,504 LOC)
- **Why:** Extracts the 1,381-line AIAgent.__init__ body. `__init__` is now a thin forwarder calling `init_agent(self, ...)`.
- **Conflict:** Standard delete-old-body / accept-upstream-forwarder. All five fingerprints intact post-snap.

### P188 — Upstream absorption: extract 10 remaining helpers to agent/agent_runtime_helpers.py
- **Cherry-picked:** 2026-05-19 (MOL-597 — 12/12)
- **Upstream:** `94c3e0ab8` refactor(run_agent): extract 10 more helpers to agent/agent_runtime_helpers.py
- **Local:** `3b3be6ad5`
- **Files:** run_agent.py, agent/agent_runtime_helpers.py (NEW, 2,159 LOC)
- **Why:** Final batch — extracts `_invoke_tool`, `_extract_api_error_context`, `switch_model`, and 7 sibling helpers. Each becomes a thin forwarder. End state: run_agent.py 5,297 lines (was 15,195 — 65% reduction).
- **Conflict:** Three delete-vs-modified blocks resolved bottom-up; standard accept-upstream-forwarder pattern. All five fingerprints intact post-snap.

### P176 — Kimi K2.6 fallback for transient composer failures
- **Cherry-picked:** N/A (local patch — MOL-660)
- **Upstream:** N/A
- **Local:** `782a46508`
- **Files:** plugins/memory/tiered/llm.py
- **Why:** Composer was the only cron-invoked production LLM site missing fallback symmetry with primary turn-loop (config.yaml:463 fallback_model). On transient errors (rate limit, 5xx, network) retry once via Kimi K2.6 before returning None. Permanent paths (ComposerKeyMissing, ComposerAuthFailure) skip fallback.
- **Conflict:** None — new function `_try_kimi_fallback`, surgical change to `except Exception` block.

### P152 — Session state survives gateway restarts
- **Cherry-picked:** 2026-05-16
- **Upstream:** `e0e7397c` fix(session): persist auto-reset state across gateway restarts
- **Local:** `0a67a1a21`
- **Files:** gateway/session.py, gateway/run.py
- **Why:** Session auto-reset state persists across gateway restarts (MOL-576).

### P153 — Skip OpenViking upload symlinks in memory
- **Cherry-picked:** 2026-05-16
- **Upstream:** `63991bbd` fix(memory): skip OpenViking upload symlinks
- **Local:** `ebf5a3a88`
- **Files:** plugins/memory/tiered store + upload
- **Why:** Prevents memory provider from following symlinks in upload dirs.

### P154 — Silence memory provider teardown output
- **Cherry-picked:** 2026-05-16
- **Upstream:** `55ba02be` fix(background-review): silence memory provider teardown output leak
- **Local:** `90964fc77`
- **Files:** run_agent.py
- **Why:** Suppresses noisy memory provider shutdown output during background review.
- **Conflict:** run_agent.py — resolved by accepting incoming (tool whitelist + provider shutdown).

### P155 — Show context compaction status
- **Cherry-picked:** 2026-05-16
- **Upstream:** `00ad3d3c` fix: show context compaction status
- **Local:** (auto-merged into 90964fc77 sequence)
- **Files:** run_agent.py
- **Why:** Visibility into when context compaction fires — we can now see it happening.

### P156 — Compression model context-length detection with custom providers
- **Cherry-picked:** 2026-05-16
- **Upstream:** `7becb19e` fix(auxiliary): forward custom_providers to compression model context-length detection
- **Local:** `2b646ed20`
- **Files:** agent/auxiliary_client.py
- **Why:** Compression model works correctly with custom providers (our DeepSeek setup).

### P157 — Keep image results from poisoning text-only sessions
- **Cherry-picked:** 2026-05-16
- **Upstream:** `a28add19` fix(agent): keep image tool results from poisoning text-only sessions
- **Local:** `f2cf44134`
- **Files:** run_agent.py
- **Why:** Prevents image tool results from silently consuming context in text-only model sessions.
- **Conflict:** run_agent.py — resolved by accepting incoming (new image-rejection error patterns).

### P158 — Docs: media impact on session context
- **Cherry-picked:** 2026-05-16
- **Upstream:** `1dd33988` docs: clarify media impact on session context
- **Local:** `707d40b59`
- **Files:** website/docs/user-guide/sessions.md
- **Why:** Documents how media attachments affect context budget.

## Unreachable (need fetch)

- `4e89c530` fix(async): close unscheduled coroutines in threadsafe bridges (May 15) — 6 conflicts, cross-cutting, skipped

### P167 — DeepSeek thinking.type + reasoning_effort mapping
- **Cherry-picked:** 2026-05-16
- **Upstream:** `068c24f8` feat(deepseek): add thinking.type + reasoning_effort mapping for DeepSeek API
- **Local:** `8c8eeddf5`
- **Files:** agent/transports/chat_completions.py, run_agent.py
- **Why:** We use DeepSeek V4 — better reasoning control via thinking.type parameter.

### P168 — Gateway: keep running when platforms fail + circuit breaker
- **Cherry-picked:** 2026-05-16
- **Upstream:** `518f3955` fix(gateway): keep running when platforms fail; add per-platform circuit breaker + /platform
- **Local:** `3f8886c4d`
- **Files:** gateway/run.py (major), +8 other files
- **Why:** Gateway survives platform failures instead of dying. Critical for our gateway restart (MOL-576).
- **Conflict:** gateway/run.py (2 conflicts — startup failure handling + reconnect retry logic, accepted incoming)

### P169 — Delegate: prevent orphan heartbeat thread
- **Cherry-picked:** 2026-05-16
- **Upstream:** `2d7182f7` fix(delegate): move heartbeat thread start inside try block to prevent orphan
- **Local:** `278afdc74`
- **Files:** tools/delegate_tool.py
- **Why:** delegate_task heartbeat threads don't leak when delegation fails.

### P170 — Delegate: guard heartbeat join against unstarted thread
- **Cherry-picked:** 2026-05-16
- **Upstream:** `60683633` fix(delegate): guard heartbeat join against unstarted thread
- **Local:** `3b2af6ac3`
- **Files:** tools/delegate_tool.py
- **Why:** Companion to P169 — prevents crash when joining a heartbeat that never started.

### P171 — Terminal: tighten dangerous-command detection
- **Cherry-picked:** 2026-05-16
- **Upstream:** `6ba35ec3` Inspired by Claude Code: tighten dangerous-command detection
- **Local:** `745eaf985`
- **Files:** tools/approval.py, tests/tools/test_approval.py
- **Why:** Claude Code-inspired security hardening for terminal command approval.
- **Conflict:** test_approval.py (test additions — accepted incoming)

### P172 — Plugins: tool override flag
- **Cherry-picked:** 2026-05-16
- **Upstream:** `016c772e` feat(plugins): tool override flag for replacing built-in tools
- **Local:** `58044c3ef`
- **Files:** tools/registry.py, hermes_cli/plugins.py, +2 others
- **Why:** Clean way to layer our patches — override built-in tools via plugins instead of modifying source.
- **Conflict:** tools/registry.py (new register() params — accepted incoming)

### P173 — MCP: supports_parallel_tool_calls
- **Cherry-picked:** 2026-05-16
- **Upstream:** `395e9dd9` feat: add supports_parallel_tool_calls for MCP servers
- **Local:** `e38111236`
- **Files:** tools/mcp_tool.py, run_agent.py, +4 others
- **Why:** MCP servers can declare parallel tool call support — faster multi-tool operations.

### P174 — Security: sanitize tool error strings
- **Cherry-picked:** 2026-05-16
- **Upstream:** `627f8a5f` security: sanitize tool error strings before injecting into model context
- **Local:** `fa0813f7e`
- **Files:** model_tools.py, tools/registry.py
- **Why:** Prevents error stack traces and sensitive data from entering context. Directly addresses memory budget concern.

### P175 — Gateway: merge rapid TEXT follow-ups
- **Cherry-picked:** 2026-05-16
- **Upstream:** `585d6b64` fix(gateway): merge rapid TEXT follow-ups during active sessions
- **Local:** `cdf42b532`
- **Files:** gateway/platforms/base.py
- **Why:** Rapid-fire messages get merged into one turn instead of separate turns — reduces context bloat and API calls.

### P159 — Skip providers without credentials
- **Cherry-picked:** 2026-05-16
- **Upstream:** `057f5a31` fix(auxiliary): skip providers without credentials immediately
- **Local:** `f15858e90`
- **Files:** agent/auxiliary_client.py
- **Why:** Faster session startup — don't attempt providers that have no credentials.

### P160 — Stop retrying initial MCP auth failures
- **Cherry-picked:** 2026-05-16
- **Upstream:** `1247ff2d` fix: stop retrying initial MCP auth failures
- **Local:** `db6a090fd`
- **Files:** 2 files in agent/
- **Why:** MCP tools fail fast instead of retry-looping on auth errors.

### P161 — Perf: list+join in agent loop
- **Cherry-picked:** 2026-05-16
- **Upstream:** `4f8aaf10` perf(run_agent): accumulate length-continuation prefix via list+join
- **Local:** `d5f60d81a`
- **Files:** run_agent.py
- **Why:** Lower latency, less memory pressure — replaces repeated string concatenation with list append/join.

### P162 — Terminal safety filter false positives
- **Cherry-picked:** 2026-05-16
- **Upstream:** `364ddd45` fix(terminal): prevent safety filter false positives on keywords inside quoted strings
- **Local:** `72d243b5e`
- **Files:** tools/terminal_tool.py
- **Why:** Our terminal commands get blocked less often when keywords appear inside quoted strings.

### P163 — Gateway forward images to background tasks
- **Cherry-picked:** 2026-05-16
- **Upstream:** `3adde245` fix(gateway): forward image attachments to background agent tasks
- **Local:** `a9e7eb773`
- **Files:** gateway/run.py
- **Why:** Image attachments in cron/scheduled tasks actually get forwarded to the agent.

### P164 — Telegram model-switch fix
- **Cherry-picked:** 2026-05-16
- **Upstream:** `26deeea8` fix(telegram): restore model-switch success path + author map
- **Local:** `5fce0dfd9`
- **Files:** gateway/platforms/telegram.py
- **Why:** Model switching in Telegram works again. Uses format_message() wrapper for safe markdown.
- **Conflict:** telegram.py (MARKDOWN_V2 + format_message wrapper — accepted incoming), test file deleted (kept our deletion).

### P165 — Gateway 429 rate-limit guard
- **Cherry-picked:** 2026-05-16
- **Upstream:** `23ac522d` fix(gateway): isinstance-guard string-form 429 error body
- **Local:** `09b31ea09`
- **Files:** gateway/run.py
- **Why:** Properly handles rate-limit responses when OpenRouter throttles us.

### P166 — Cron name-based lookup
- **Cherry-picked:** 2026-05-16
- **Upstream:** `6682f91b` feat(cron): support name-based lookup for job operations
- **Local:** `ccbef02ed`
- **Files:** cron/jobs.py, hermes_cli/cron.py, tools/cronjob_tools.py
- **Why:** Use `cronjob action="list" --name="..."` instead of hunting job IDs. Directly useful for our 14+ cron jobs.

## Skipped (not applicable)

- `12f755c9` fix(codex-runtime): retire wedged sessions — Codex-only, we use DeepSeek. Files deleted in our tree.

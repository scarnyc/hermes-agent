# MOL-376 Phase 2 — Restore Procedure

How to restore the gateway-wide autonomous skill patching changes if
`~/.hermes/hermes-agent/` is deleted.

## Prerequisites

- Fresh clone of `https://github.com/NousResearch/hermes-agent.git`
- All P03-P29 patches already applied per `PATCHES.md`
- Python venv at `~/.hermes/hermes-agent/venv/`

## Files

| File | Type | How to restore |
|------|------|---------------|
| `tools/skill_patcher.py` | New | Copy `reference/skill_patcher.py.new` → `tools/skill_patcher.py` |
| `run_agent.py` | Modified | Three insertions (see below) |
| `tools/skill_manager_tool.py` | Modified | One line change (see below) |
| `hermes_cli/config.py` | Modified | One block addition (see below) |

## Step 1: skill_patcher.py (new file)

```bash
cp reference/skill_patcher.py.new ~/.hermes/hermes-agent/tools/skill_patcher.py
```

## Step 2: run_agent.py (three insertions)

### 2a. _detect_active_skill() — insert BEFORE _spawn_background_review()

Find:
```python
    def _spawn_background_review(
```

Insert BEFORE that line:
```python
    @staticmethod
    def _detect_active_skill(user_message: str, messages_snapshot: List[Dict]) -> Optional[str]:
        """Identify which skill is active for the current turn.

        Three detection strategies, first match wins:
        1. Slash command in user message: /skill-name → skill-name
        2. Tool results with skill_view/skill_manage calls → extract name field
        3. Injected [Skill loaded: name] content block in user message

        Returns skill name string or None if no skill identified.
        """
        import re

        # Strategy 1: Slash command in user message
        slash_match = re.search(r'(?:^|\s)/([a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*)', user_message or "")
        if slash_match:
            skill_name = slash_match.group(1).strip()
            if not skill_name.startswith("/") and "/" not in skill_name.split(".."):
                return skill_name

        # Strategy 2: Tool results with skill_view/skill_manage calls
        for msg in reversed(messages_snapshot or []):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "tool" and isinstance(content, str):
                try:
                    data = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    continue
                name = data.get("name", "")
                action = data.get("action", "")
                if name and action in ("skill_view", "skill_manage", "skill_load"):
                    return name

        # Strategy 3: Injected [Skill loaded: name] block
        load_match = re.search(r'\[Skill loaded:\s*([^\]]+)\]', user_message or "")
        if load_match:
            return load_match.group(1).strip()

        return None

```

### 2b. _spawn_background_quality_review() — insert BETWEEN _detect_active_skill and _spawn_background_review

Insert BETWEEN the end of `_detect_active_skill` and the start of `_spawn_background_review`:
```python
    def _spawn_background_quality_review(
        self,
        final_response: str,
        user_message: str,
        messages_snapshot: List[Dict],
    ) -> None:
        """Background thread: evaluate response quality, patch active skill if needed.

        Fires in a daemon thread after run_conversation() returns. Calls
        reflection_agent.analyze_output(), detects the active skill, and calls
        diagnose_and_patch_for_session(). Fail-open — any error is caught and
        logged; user response is never blocked.
        """
        import threading

        def _run_quality_review():
            try:
                from tools.reflection_agent import analyze_output
                from tools.skill_patcher import diagnose_and_patch_for_session
                from hermes_cli.config import load_config

                concerns, status = analyze_output(
                    final_response, caller_id=self.session_id,
                )
                if not concerns or status == "unavailable":
                    return

                skill_name = self._detect_active_skill(user_message, messages_snapshot)
                config = load_config()
                cfg = config.get("auto_improve", {})
                diagnose_and_patch_for_session(
                    session_id=self.session_id,
                    skill_name=skill_name or "",
                    concerns=concerns,
                    final_response=final_response,
                    auto_improve_cfg=cfg,
                )
            except Exception as e:
                logger.debug("Background quality review failed: %s", e)

        t = threading.Thread(target=_run_quality_review, daemon=True, name="bg-quality-review")
        t.start()

```

### 2c. Call site in run_conversation()

Find:
```python
        except Exception as exc:
            logger.warning("on_session_end hook failed: %s", exc)

        return result
```

Insert BETWEEN `except Exception as exc: ...` and `return result`:
```python
        # MOL-376 Phase 2: Gateway-wide autonomous skill improvement.
        if final_response and not interrupted:
            try:
                self._spawn_background_quality_review(
                    final_response=final_response,
                    user_message=original_user_message or "",
                    messages_snapshot=list(messages),
                )
            except Exception:
                pass  # best-effort
```

## Step 3: tools/skill_manager_tool.py (gating language)

Find:
```
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n"
```

Replace with:
```
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n"
        "Autonomous PATCHING (not creation) of existing skills is permitted without "
        "confirmation when guardrails pass (severity gate, dedup, fuzzy-match, "
        "security scan, LLM diagnosis).\n"
```

## Step 4: hermes_cli/config.py (auto_improve block)

Find the `"enrichment"` block closing `},` in DEFAULT_CONFIG. After that block, insert:

```python
    # MOL-376 Phase 2 — gateway-wide autonomous skill improvement.
    # Fires after every run_conversation() return (Telegram, CLI, slash commands).
    # Evaluates response quality via reflection agent; if HIGH-severity concerns
    # trace to a fixable instruction flaw in the active skill, applies surgical
    # patch via the same pipeline as cron.reflection.auto_patch.
    # Separate toggle from cron path — different risk profile (interactive
    # sessions, variable skill usage, no retry-loop context).
    "auto_improve": {
        "enabled": False,
        "dry_run": True,
    },
```

## Step 5: Verify

```bash
# Verify all patches (including MOL-376)
bash scripts/hermes-patches/verify_patches.sh

# Verify skill_patcher.py compiles
~/.hermes/hermes-agent/venv/bin/python3 -m py_compile ~/.hermes/hermes-agent/tools/skill_patcher.py

# Verify imports work
cd ~/.hermes/hermes-agent && ./venv/bin/python3 -c "
from tools.skill_patcher import diagnose_and_patch, diagnose_and_patch_for_session
print('skill_patcher imports OK')
"
```

## Config activation (after restore)

Add to `~/.hermes/config.yaml`:
```yaml
auto_improve:
  enabled: false
  dry_run: true
```

This is separate from the code changes — the live config gates behavior.

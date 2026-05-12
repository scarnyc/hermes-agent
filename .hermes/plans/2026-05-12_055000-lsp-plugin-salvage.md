# LSP Plugin Salvage — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Salvage PR #24168's LSP implementation into a proper Hermes plugin that uses the existing hook system, ships as opt-in, and manages process lifecycle correctly.

**Architecture:** The existing `agent/lsp/` module (client, protocol, workspace, servers, install, reporter, eventlog) moves wholesale into `plugins/lsp/`. A thin `__init__.py` wires hooks (`pre_tool_call`, `post_tool_call`, `transform_tool_result`, `on_session_start`, `on_session_end`) to the service. Zero core code changes.

**Source Material:** Cherry-pick commits `d331c4952` and `7fa5657d6` from PR #24168 branch `pr-24168-v2`.

---

## Decisions Summary

| Decision | Choice |
|---|---|
| Location | `plugins/lsp/` (bundled plugin, auto-loads when in `plugins.enabled`) |
| Default state | Disabled — user must add `lsp` to `plugins.enabled` list |
| Binary install | Detect-only by default; `hermes lsp install <server>` for explicit installs |
| Hook for baseline capture | `pre_tool_call` — fires before the write, gets `args` with file path |
| Hook for diagnostic injection | `transform_tool_result` — appends diagnostics to write_file/patch output |
| Lifecycle | `on_session_start` / `on_session_end` for init/shutdown |
| Config location | `plugins.lsp.*` section in config.yaml (plugin reads its own keys) |
| Patch double-call bug fix | Plugin captures baseline in `pre_tool_call` and fetches in `post_tool_call` — bypasses the core `_check_lint_delta` entirely |
| Gateway/one-shot mode | Plugin checks `platform` kwarg on `on_session_start` — skips init for non-interactive |

---

## Phase 1: Plugin Skeleton + Hook Wiring

### Task 1.1: Create plugin directory and manifest

**Objective:** Establish the plugin structure with a valid `plugin.yaml`.

**Files:**
- Create: `plugins/lsp/plugin.yaml`
- Create: `plugins/lsp/__init__.py`

**plugin.yaml:**
```yaml
name: lsp
version: "1.0.0"
description: "Semantic diagnostics from real language servers (pyright, gopls, rust-analyzer, etc.) on write_file/patch. Opt-in: add 'lsp' to plugins.enabled in config.yaml."
author: NousResearch
hooks:
  - pre_tool_call
  - post_tool_call
  - transform_tool_result
  - on_session_start
  - on_session_end
```

**`__init__.py` (skeleton):**
```python
"""LSP Plugin — semantic diagnostics from real language servers."""
from __future__ import annotations
import logging

logger = logging.getLogger("plugins.lsp")

def register(ctx):
    """Plugin entry point — wire hooks."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    # CLI commands
    from plugins.lsp.cli import register_cli
    ctx.register_cli_command(
        name="lsp",
        help="Language Server Protocol management",
        setup_fn=register_cli,
    )

def _on_session_start(**kwargs):
    pass  # Phase 2

def _on_session_end(**kwargs):
    pass  # Phase 2

def _pre_tool_call(**kwargs):
    pass  # Phase 3

def _post_tool_call(**kwargs):
    pass  # Phase 3

def _transform_tool_result(**kwargs):
    pass  # Phase 3
```

**Verify:** `python -c "from plugins.lsp import register; print('OK')"` from repo root.

---

### Task 1.2: Move LSP modules from agent/lsp/ to plugins/lsp/

**Objective:** Relocate the implementation files without modification.

**Files:**
- Move: `agent/lsp/client.py` → `plugins/lsp/client.py`
- Move: `agent/lsp/protocol.py` → `plugins/lsp/protocol.py`
- Move: `agent/lsp/manager.py` → `plugins/lsp/manager.py`
- Move: `agent/lsp/servers.py` → `plugins/lsp/servers.py`
- Move: `agent/lsp/workspace.py` → `plugins/lsp/workspace.py`
- Move: `agent/lsp/install.py` → `plugins/lsp/install.py`
- Move: `agent/lsp/reporter.py` → `plugins/lsp/reporter.py`
- Move: `agent/lsp/eventlog.py` → `plugins/lsp/eventlog.py`

**Fix imports:** All internal imports change from `from agent.lsp.X` to `from plugins.lsp.X`. The manager imports client, client imports protocol, etc.

**Verify:** `python -c "from plugins.lsp.manager import LSPService; print('OK')"`

---

### Task 1.3: Move and adapt tests

**Objective:** Relocate tests, fix imports, ensure 68/73 still pass.

**Files:**
- Move: `tests/agent/lsp/` → `tests/plugins/lsp/`
- Fix all `from agent.lsp` → `from plugins.lsp` in test files

**Verify:** `python -m pytest tests/plugins/lsp/ -o "addopts=" --tb=short -q` — 68 pass, 5 fail (same live-system-guard issue, fix in Phase 4).

---

## Phase 2: Lifecycle Hooks

### Task 2.1: Implement `on_session_start` — service initialization

**Objective:** Spin up the LSP service when a session starts, only if conditions are met.

**Logic:**
```python
def _on_session_start(**kwargs):
    """Initialize LSP service if we're in a local interactive session inside a git workspace."""
    platform = kwargs.get("platform", "cli")
    # Skip for non-interactive one-shot modes
    if platform in ("batch", "cron"):
        return
    from plugins.lsp.workspace import find_git_worktree
    import os
    cwd = os.getcwd()
    if not find_git_worktree(cwd):
        return  # Not in a git workspace — stay dormant
    from plugins.lsp.manager import LSPService
    global _service
    _service = LSPService.create_from_config()
```

**Config reading:** Plugin reads `lsp.*` from config.yaml via `hermes_cli.config.load_config()` — same pattern as current `create_from_config()`.

---

### Task 2.2: Implement `on_session_end` — clean shutdown

**Objective:** Kill all language server child processes gracefully.

```python
def _on_session_end(**kwargs):
    """Shutdown all LSP servers."""
    global _service
    if _service is not None:
        _service.shutdown()
        _service = None
```

**Verify:** Start a session with LSP enabled, write a .py file in a git repo, verify pyright spawns (`ps aux | grep pyright`), end session, verify pyright is gone.

---

## Phase 3: Diagnostic Hook Wiring

### Task 3.1: Implement `pre_tool_call` — baseline capture

**Objective:** Before `write_file` or `patch` executes, snapshot current LSP diagnostics.

```python
# Module-level state for the in-flight baseline
_pending_baseline_path: str | None = None

def _pre_tool_call(**kwargs):
    """Capture LSP baseline before a file write."""
    global _pending_baseline_path
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        return
    if _service is None or not _service.is_active():
        return
    args = kwargs.get("args", {})
    if isinstance(args, str):
        import json
        try:
            args = json.loads(args)
        except Exception:
            return
    path = args.get("path", "")
    if not path:
        return
    # Check backend is local
    # (plugin hooks don't have access to the FileOperations instance,
    #  but we can check if the path is accessible from host)
    import os
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    _pending_baseline_path = expanded
    _service.snapshot_baseline(expanded)
```

---

### Task 3.2: Implement `transform_tool_result` — inject diagnostics

**Objective:** After write_file/patch completes successfully, append LSP diagnostics to the tool result.

```python
def _transform_tool_result(**kwargs) -> str | None:
    """Append LSP diagnostics to write_file/patch results."""
    global _pending_baseline_path
    tool_name = kwargs.get("tool_name", "")
    if tool_name not in ("write_file", "patch"):
        _pending_baseline_path = None
        return None
    if _service is None or _pending_baseline_path is None:
        _pending_baseline_path = None
        return None
    path = _pending_baseline_path
    _pending_baseline_path = None
    if not _service.enabled_for(path):
        return None
    try:
        diagnostics = _service.get_diagnostics_sync(path, delta=True)
    except Exception:
        return None
    if not diagnostics:
        return None
    from plugins.lsp.reporter import report_for_file, truncate
    block = report_for_file(path, diagnostics)
    if not block:
        return None
    lsp_output = truncate("LSP diagnostics introduced by this edit:\n" + block)
    # Append to existing result
    result = kwargs.get("result", "")
    return result + "\n\n" + lsp_output
```

**Key insight:** This completely bypasses the `_check_lint_delta` → `_maybe_lsp_diagnostics` path in `file_operations.py`. The baseline is captured in `pre_tool_call` (before the write) and diagnostics are fetched in `transform_tool_result` (after the write). No double-call bug possible because `patch_replace` → `write_file` is a single tool call from the hook's perspective.

---

### Task 3.3: Handle the patch_replace double-call correctly

**Objective:** Verify that when `patch` internally calls `write_file`, the hooks fire correctly.

The `patch` tool is registered as a single tool. `handle_function_call("patch", ...)` calls `patch_replace()` which internally calls `write_file()`. But from the plugin hook perspective, there's only ONE `pre_tool_call(tool_name="patch")` and ONE `post_tool_call(tool_name="patch")`. The internal `write_file()` call is a method call, not a tool dispatch — hooks don't fire for it.

**This means:** The baseline is captured once (before patch), diagnostics are fetched once (after patch). The double-call bug from PR #24168 is impossible in the plugin model.

**Test:** Write a test that patches a file introducing a type error, verify diagnostics appear in the transformed result.

---

## Phase 4: Cleanup and Polish

### Task 4.1: Remove core integration from file_operations.py

**Objective:** Strip the LSP hooks from `tools/file_operations.py` — they're now handled by the plugin.

**Files:**
- Modify: `tools/file_operations.py` — remove `_snapshot_lsp_baseline()`, `_maybe_lsp_diagnostics()`, `_lsp_local_only()`, and the call to `_snapshot_lsp_baseline` in `write_file()`
- Modify: `hermes_cli/config.py` — remove the `"lsp": {...}` section from `DEFAULT_CONFIG`
- Modify: `hermes_cli/main.py` — remove the `from agent.lsp.cli import register_subparser` block
- Delete: `agent/lsp/` directory entirely

**Verify:** `python -m pytest tests/tools/test_file_operations*.py -o "addopts=" -q` — existing file ops tests all pass (LSP was best-effort, removing it doesn't break anything).

---

### Task 4.2: Plugin config section

**Objective:** Plugin reads its config from a dedicated section.

The plugin reads `lsp.*` from config.yaml the same way the current `create_from_config()` does — via `load_config()`. But it lives under the plugin's own namespace. Users configure:

```yaml
plugins:
  enabled:
    - lsp

lsp:
  wait_mode: document
  wait_timeout: 5.0
  install_strategy: manual  # "manual" = detect only; "auto" = install on first use
  servers:
    pyright:
      disabled: false
    gopls:
      command: ["/usr/local/bin/gopls", "serve"]
```

No `DEFAULT_CONFIG` change needed — `_deep_merge` handles missing keys, and the plugin uses `.get()` with defaults internally.

---

### Task 4.3: Fix test_client_e2e.py live-system-guard failures

**Objective:** Make the 5 failing E2E tests work with conftest's subprocess guard.

**Fix:** Add `@pytest.mark.live_system_guard_bypass` to the test class/functions that spawn the mock LSP server subprocess. These tests legitimately need to spawn and terminate child processes.

```python
@pytest.mark.live_system_guard_bypass
async def test_client_lifecycle_clean():
    ...
```

**Verify:** All 73 tests pass.

---

### Task 4.4: CLI commands via plugin context

**Objective:** Wire `hermes lsp` subcommands through `register_cli_command`.

The existing `agent/lsp/cli.py` already has `register_subparser()`. Adapt it to work with the plugin's `ctx.register_cli_command()`:

```python
# In __init__.py
ctx.register_cli_command(
    name="lsp",
    help="Language Server Protocol management",
    setup_fn=register_subparser_adapted,
    handler_fn=run_lsp_command,
)
```

**Verify:** `hermes lsp status` works when plugin is enabled, shows "plugin not enabled" hint when disabled.

---

## Phase 5: Documentation and PR

### Task 5.1: Update docs page

**Objective:** Update `website/docs/user-guide/features/lsp.md` to reflect opt-in plugin model.

Key changes:
- "Enable with `plugins: { enabled: [lsp] }` in config.yaml"
- Remove "enabled by default" language
- Add "Install servers" section explaining `hermes lsp install` vs manual PATH
- Note that `install_strategy: manual` is default (detect-only)

### Task 5.2: Close PR #24168 and #24155 with credit

- Open new PR from the worktree branch with the plugin-based implementation
- Reference both PRs in the body
- Close #24168 with comment crediting @teknium1's implementation
- Close #24155 with comment crediting @OutThisLife's simpler design influence

---

## File Inventory

| File | Action | LOC (approx) |
|------|--------|---:|
| `plugins/lsp/plugin.yaml` | Create | 10 |
| `plugins/lsp/__init__.py` | Create | 120 |
| `plugins/lsp/client.py` | Move from agent/lsp/ | 930 |
| `plugins/lsp/protocol.py` | Move | 187 |
| `plugins/lsp/manager.py` | Move + minor edits | 510 |
| `plugins/lsp/servers.py` | Move | 1025 |
| `plugins/lsp/workspace.py` | Move | 205 |
| `plugins/lsp/install.py` | Move + change default | 347 |
| `plugins/lsp/reporter.py` | Move | 78 |
| `plugins/lsp/eventlog.py` | Move | 213 |
| `plugins/lsp/cli.py` | Move + adapt | 270 |
| `tests/plugins/lsp/*` | Move + fix imports | 870 |
| `tools/file_operations.py` | Remove 107 lines | -107 |
| `hermes_cli/config.py` | Remove 47 lines | -47 |
| `hermes_cli/main.py` | Remove 11 lines | -11 |
| `agent/lsp/` | Delete entirely | -3611 |
| `website/docs/user-guide/features/lsp.md` | Update | ~223 |

**Net:** ~4000 LOC in plugin, -3776 LOC from core. Core gets smaller.

---

## Implementation Order

```
Phase 1 (skeleton)  ──→  Phase 2 (lifecycle)  ──→  Phase 3 (diagnostics)  ──→  Phase 4 (cleanup)  ──→  Phase 5 (docs/PR)
     │                        │                         │                          │
     └── plugin loads         └── servers start/stop    └── model sees errors      └── core is clean
```

Each phase is independently verifiable. After Phase 3, the feature works end-to-end. Phase 4 is cleanup only.

---

## Risks and Tradeoffs

| Risk | Mitigation |
|------|-----------|
| `transform_tool_result` hook might not receive `args` | Verify by reading `model_tools.py` — confirmed it passes `args=function_args` |
| Baseline capture in `pre_tool_call` may not have the final resolved path | Plugin does its own `os.path.expanduser` + `os.path.abspath` — same as current code |
| Plugin loading order — what if LSP plugin loads before file tools register? | Not an issue — hooks fire at runtime, not import time |
| `patch` internal `write_file()` call — does it trigger hooks? | No — it's a direct method call, not a tool dispatch. Hooks only fire on `handle_function_call()`. This is actually correct behavior. |
| Performance: extra hook invocations on every tool call | `pre_tool_call` check is 3 lines (tool_name check + service None check). Nanoseconds. |

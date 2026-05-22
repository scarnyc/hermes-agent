"""Behavioral tests for the symphony-bridge global wall-clock budget manager
(P153/MOL-506).

Verifier checks at `scripts/hermes-patches/verify_patches.sh` are structural
(class presence + invocation counts + markers). These cases cover the
behavioral surface the verifier can't reach — failure-mode invariants,
construct-and-start contract, gate integration, abort observability.

Loads the runtime module via importlib.util because `~/.hermes/scripts/` is
NOT on the project's pytest discovery path. Skipped when the runtime is not
deployed (matches test_preflight_health.py pattern).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest


def _fake_dry_run_file(exists: bool) -> Mock:
    """Stand-in for `DRY_RUN_FILE` (a `Path`). Path instances are immutable
    so `patch.object(DRY_RUN_FILE, 'exists', ...)` raises "attribute is
    read-only"; swap the whole binding via patch.object on the module."""
    fake = Mock()
    fake.exists.return_value = exists
    return fake


SB_SCRIPT = Path.home() / ".hermes" / "scripts" / "symphony_bridge.py"

pytestmark = pytest.mark.skipif(
    not SB_SCRIPT.exists(),
    reason="symphony_bridge.py not deployed at runtime path",
)


@pytest.fixture(autouse=True)
def _clear_budget_disabled_default(monkeypatch):
    """P163/MOL-523: HERMES_BUDGET_DISABLED is a runtime toggle. If left set
    in an operator's shell (e.g., they're mid-shakeout and run `pytest`), it
    leaks into every BudgetTracker construction here and 15+ pre-existing
    tests fail with confusing `inf == 300.0` mismatches. Default every test
    to "flag unset"; the `TestBudgetDisabled` cases that exercise the flag
    re-set it explicitly via monkeypatch.setenv (which layers on top of this
    autouse fixture in the same test scope).
    """
    monkeypatch.delenv("HERMES_BUDGET_DISABLED", raising=False)


@pytest.fixture(scope="module")
def sb_module():
    """Load symphony_bridge runtime module by path. ~/.hermes/scripts/ must
    be on sys.path because symphony_bridge.py does `import preflight_health`
    at top-level (sibling module in the same directory)."""
    scripts_dir = str(SB_SCRIPT.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        "symphony_bridge_under_test", SB_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["symphony_bridge_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────────────────────────────────────────────────────────
# BudgetTracker — construct-and-start contract
# ──────────────────────────────────────────────────────────────────────────


class TestBudgetTrackerConstruction:
    """The unarmed state is unrepresentable. Construction always either
    succeeds with a real `start_monotonic` (via `start_now()` or explicit
    ctor) or raises on invalid input."""

    def test_start_now_factory_records_monotonic(self, sb_module):
        with patch.object(sb_module.time, "monotonic", return_value=1000.0):
            t = sb_module.BudgetTracker.start_now(budget_seconds=600)
        assert t._start_monotonic == 1000.0
        assert t._budget == 600

    def test_start_now_uses_module_defaults(self, sb_module):
        t = sb_module.BudgetTracker.start_now()
        assert t._budget == sb_module._GLOBAL_BUDGET_SECONDS
        assert t._min_phase == sb_module._MIN_PHASE_BUDGET

    def test_explicit_ctor_for_rehydration(self, sb_module):
        """Direct ctor exists for state-file rehydration (future Phase 4
        daemon work). Caller supplies the persisted monotonic value
        instead of capturing time.monotonic() at construction."""
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=42.0,
        )
        assert t._start_monotonic == 42.0

    def test_ctor_rejects_zero_budget(self, sb_module):
        with pytest.raises(ValueError, match="budget_seconds must be positive"):
            sb_module.BudgetTracker(
                budget_seconds=0, min_phase_budget=0, start_monotonic=0.0,
            )

    def test_ctor_rejects_negative_budget(self, sb_module):
        with pytest.raises(ValueError, match="budget_seconds must be positive"):
            sb_module.BudgetTracker(
                budget_seconds=-1, min_phase_budget=0, start_monotonic=0.0,
            )

    def test_ctor_rejects_negative_min_phase(self, sb_module):
        with pytest.raises(ValueError, match="min_phase_budget=-1"):
            sb_module.BudgetTracker(
                budget_seconds=600, min_phase_budget=-1, start_monotonic=0.0,
            )

    def test_ctor_rejects_min_phase_exceeding_budget(self, sb_module):
        with pytest.raises(ValueError, match=r"min_phase_budget=601"):
            sb_module.BudgetTracker(
                budget_seconds=600, min_phase_budget=601, start_monotonic=0.0,
            )

    def test_ctor_accepts_min_phase_equal_to_budget(self, sb_module):
        """Boundary: min_phase == budget is the looser invariant; only
        min_phase > budget should reject."""
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=600, start_monotonic=0.0,
        )
        assert t._min_phase == 600


# ──────────────────────────────────────────────────────────────────────────
# BudgetTracker — accounting
# ──────────────────────────────────────────────────────────────────────────


class TestBudgetTrackerAccounting:
    """Time-mocked behavior across the budget lifecycle."""

    def test_elapsed_uses_monotonic_delta(self, sb_module):
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=100.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=130.5):
            assert t.elapsed() == pytest.approx(30.5)

    def test_remaining_decreases_with_elapsed(self, sb_module):
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=100.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=400.0):
            assert t.remaining() == pytest.approx(300.0)

    def test_remaining_clamps_to_zero_past_budget(self, sb_module):
        """At t=budget+overrun, remaining must NOT go negative — downstream
        comparison logic assumes a non-negative scalar."""
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=100.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=900.0):
            assert t.remaining() == 0.0

    @pytest.mark.parametrize(
        "elapsed_secs,min_phase,expected",
        [
            (0, 300, True),
            (299, 300, True),       # remaining=601 >= 300
            (300, 300, True),       # remaining=600 >= 300
            (599, 300, True),       # remaining=301 >= 300
            (600, 300, True),       # remaining=300 == 300 (boundary)
            (601, 300, False),      # remaining=299 < 300 — gate fires
            (900, 300, False),
            (900, 0, True),         # min_phase=0 → always pass while remaining>=0
        ],
    )
    def test_can_start_phase_boundary(
        self, sb_module, elapsed_secs, min_phase, expected,
    ):
        t = sb_module.BudgetTracker(
            budget_seconds=900, min_phase_budget=min_phase, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=float(elapsed_secs)):
            # "unknown-phase" falls back to bare min_phase floor — perfect
            # vehicle for the parametrized table.
            assert t.can_start_phase("unknown-phase") is expected

    def test_summary_rounds_to_two_decimals(self, sb_module):
        """`to_summary()` two-decimal rounding is contract — JSONL log
        consumers and abort banner formatting both depend on the shape."""
        t = sb_module.BudgetTracker(
            budget_seconds=100, min_phase_budget=50, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=33.456789):
            s = t.to_summary()
        assert s["budget_secs"] == 100
        assert s["elapsed_secs"] == 33.46
        assert s["remaining_secs"] == 66.54

    def test_repr_includes_summary(self, sb_module):
        """__repr__ wraps to_summary() — affords dev-time inspection
        without forcing callers to invoke .to_summary() manually."""
        t = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=0.0,
        )
        r = repr(t)
        assert "BudgetTracker" in r
        assert "budget_secs" in r
        assert "600" in r


# ──────────────────────────────────────────────────────────────────────────
# Module constants
# ──────────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_min_phase_budget_is_300(self, sb_module):
        assert sb_module._MIN_PHASE_BUDGET == 300

    def test_global_budget_aliases_total_timeout(self, sb_module):
        """Alias relationship locked — a stealth decoupling can't slide
        through review without tripping this test."""
        assert sb_module._GLOBAL_BUDGET_SECONDS == sb_module._TOTAL_TIMEOUT_SECONDS
        assert sb_module._GLOBAL_BUDGET_SECONDS == 3300

    def test_revise_phases_in_phase_timeouts(self, sb_module):
        """REVISE-loop gate names must be in _PHASE_TIMEOUTS so
        BudgetTracker._effective_min_phase derives the correct floor.
        Without these entries, planner_revise/skeptic_revise fall back
        to the bare 300s floor and the phase-aware-floor attribution
        breaks for the REVISE legs."""
        assert "planner_revise" in sb_module._PHASE_TIMEOUTS
        assert "skeptic_revise" in sb_module._PHASE_TIMEOUTS
        assert sb_module._PHASE_TIMEOUTS["planner_revise"] == sb_module._PHASE_TIMEOUTS["planner"]
        assert sb_module._PHASE_TIMEOUTS["skeptic_revise"] == sb_module._PHASE_TIMEOUTS["skeptic"]

    def test_phase_timeouts_is_immutable(self, sb_module):
        """MappingProxyType wraps the dict — a stray mutation from a test
        fixture or future helper raises TypeError instead of silently
        rewriting the runtime contract."""
        with pytest.raises(TypeError):
            sb_module._PHASE_TIMEOUTS["builder"] = 60  # type: ignore

    def test_cc_max_turns_is_immutable(self, sb_module):
        with pytest.raises(TypeError):
            sb_module._CC_MAX_TURNS["planner"] = 1  # type: ignore


# ──────────────────────────────────────────────────────────────────────────
# Phase-aware floor — locks the per-phase floor table against drift
# ──────────────────────────────────────────────────────────────────────────


class TestPhaseAwareFloor:
    """`can_start_phase` uses max(_MIN_PHASE_BUDGET, _PHASE_TIMEOUTS.get(phase, 0)).
    Without the phase-aware lift, builder (1800s budget) starts with 300s
    remaining, fails on retry-budget guard, and the failure surfaces as
    'all tiers failed' instead of 'budget exhausted'.
    """

    @pytest.mark.parametrize(
        "phase,remaining_secs,expected",
        [
            # planner: floor = max(300, 600) = 600
            ("planner",  601, True),
            ("planner",  600, True),
            ("planner",  599, False),
            # skeptic: floor = max(300, 300) = 300
            ("skeptic",  300, True),
            ("skeptic",  299, False),
            # builder: floor = max(300, 1800) = 1800
            ("builder", 1800, True),
            ("builder", 1799, False),
            ("builder",  400, False),   # under bare 300s floor would pass; phase-aware rejects
            # reviewer: floor = max(300, 900) = 900
            ("reviewer", 900, True),
            ("reviewer", 500, False),
            # planner_revise: floor = max(300, 600) = 600
            ("planner_revise", 600, True),
            ("planner_revise", 599, False),
            # skeptic_revise: floor = max(300, 300) = 300
            ("skeptic_revise", 300, True),
            ("skeptic_revise", 299, False),
            # Unknown phase falls back to bare _MIN_PHASE_BUDGET = 300
            ("unknown-phase",  300, True),
            ("unknown-phase",  299, False),
        ],
    )
    def test_phase_aware_floor(self, sb_module, phase, remaining_secs, expected):
        t = sb_module.BudgetTracker(
            budget_seconds=3300, min_phase_budget=300, start_monotonic=0.0,
        )
        with patch.object(t, "remaining", return_value=float(remaining_secs)):
            assert t.can_start_phase(phase) is expected


# ──────────────────────────────────────────────────────────────────────────
# Retry-helper — tracker drives the retry gate
# ──────────────────────────────────────────────────────────────────────────


class TestRetryHelperConsultsTracker:
    """`_attempt_tier_with_retry` consults `tracker.remaining()` for the
    retry-budget guard — the cornerstone behavioral contract of P153.
    """

    def _make_tier_call(self, error_text: str, stderr_tail: str = ""):
        """Zero-arg tier_call that always returns the configured failure."""
        def _fn():
            return {
                "success": False,
                "output": "",
                "tier": 1,
                "error": error_text,
                "stderr_tail": stderr_tail,
            }
        return _fn

    def test_retry_skipped_when_tracker_remaining_below_needed(self, sb_module):
        """remaining=5s, tier_timeout=600s, retry_wait=0 → needed=600 >
        remaining=5 → retry skipped with decision=skipped_global_budget."""
        tier_call = self._make_tier_call("HTTP 503 service unavailable")
        events: list[dict[str, Any]] = []

        def fake_log_event(event, **fields):
            events.append({"event": event, **fields})

        tracker = sb_module.BudgetTracker(
            budget_seconds=600, min_phase_budget=300, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=595.0), \
             patch.object(sb_module, "_log_event", side_effect=fake_log_event), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(False)):
            result = sb_module._attempt_tier_with_retry(
                tier_call,
                tier_num=1,
                phase_name="planner",
                tracker=tracker,
                tier_timeout=600,
            )

        assert result["success"] is False
        retry_decisions = [e for e in events if e["event"] == "tier_retry_decision"]
        assert len(retry_decisions) == 1
        assert retry_decisions[0]["decision"] == "skipped_global_budget"
        assert retry_decisions[0]["needed_secs"] == 600
        assert retry_decisions[0]["remaining_secs"] == 5.0
        assert "budget_summary" in retry_decisions[0]
        assert retry_decisions[0]["budget_summary"]["remaining_secs"] == 5.0

    def test_retry_proceeds_with_exact_decision_ordering(self, sb_module):
        """remaining=1500s, tier_timeout=600s → fits. Decisions must
        appear in order: retry (attempt 1 → 2), then exhausted_attempts
        (attempt 2 final). An out-of-order or missing decision would
        slip past `any(... == "retry")` but trips equality."""
        tier_call = self._make_tier_call("HTTP 503 service unavailable")
        events: list[dict[str, Any]] = []

        def fake_log_event(event, **fields):
            events.append({"event": event, **fields})

        tracker = sb_module.BudgetTracker(
            budget_seconds=2000, min_phase_budget=300, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=500.0), \
             patch.object(sb_module, "_log_event", side_effect=fake_log_event), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(False)), \
             patch.object(sb_module.time, "sleep"):
            sb_module._attempt_tier_with_retry(
                tier_call,
                tier_num=1,
                phase_name="planner",
                tracker=tracker,
                tier_timeout=600,
            )

        decisions = [e["decision"] for e in events
                     if e["event"] == "tier_retry_decision"]
        assert decisions == ["retry", "exhausted_attempts"]

    def test_rate_limit_retry_budget_uses_full_60s_wait(self, sb_module):
        """Rate-limit class: retry_wait=60. needed = 60 + tier_timeout.
        remaining=620s should fit needed=660. remaining=600 should NOT.
        Exercise the wait-summed-into-budget math the transient path
        (retry_wait=0) never exercises."""
        tier_call = self._make_tier_call("rate_limit exceeded")
        events: list[dict[str, Any]] = []

        def fake_log_event(event, **fields):
            events.append({"event": event, **fields})

        # remaining=600 — 60s wait + 600s timeout = 660 > 600 → SKIP
        tracker = sb_module.BudgetTracker(
            budget_seconds=620, min_phase_budget=300, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=20.0), \
             patch.object(sb_module, "_log_event", side_effect=fake_log_event), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(False)):
            sb_module._attempt_tier_with_retry(
                tier_call,
                tier_num=1,
                phase_name="planner",
                tracker=tracker,
                tier_timeout=600,
            )

        decisions = [e for e in events if e["event"] == "tier_retry_decision"]
        skipped = [d for d in decisions if d["decision"] == "skipped_global_budget"]
        assert len(skipped) == 1
        assert skipped[0]["needed_secs"] == 660  # 60 wait + 600 timeout
        assert skipped[0]["remaining_secs"] == 600.0

    def test_dry_run_suppresses_rate_limit_sleep(self, sb_module):
        """The DRY_RUN guard is meaningful only when retry_wait > 0
        (transient class wait=0 makes the branch trivially short-circuit).
        Use rate_limit class so the guard actually fires."""
        tier_call = self._make_tier_call("rate_limit exceeded")
        events: list[dict[str, Any]] = []

        def fake_log_event(event, **fields):
            events.append({"event": event, **fields})

        # Tracker with plenty of headroom so the retry proceeds.
        tracker = sb_module.BudgetTracker(
            budget_seconds=3300, min_phase_budget=300, start_monotonic=0.0,
        )
        with patch.object(sb_module.time, "monotonic", return_value=100.0), \
             patch.object(sb_module, "_log_event", side_effect=fake_log_event), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(True)), \
             patch.object(sb_module.time, "sleep") as fake_sleep:
            sb_module._attempt_tier_with_retry(
                tier_call,
                tier_num=1,
                phase_name="planner",
                tracker=tracker,
                tier_timeout=600,
            )

        # Sleep MUST be suppressed under DRY_RUN even when retry_wait=60.
        fake_sleep.assert_not_called()
        # Retry still proceeded — the budget allowed it.
        decisions = [e["decision"] for e in events
                     if e["event"] == "tier_retry_decision"]
        assert "retry" in decisions


# ──────────────────────────────────────────────────────────────────────────
# Abort helper — observability invariants
# ──────────────────────────────────────────────────────────────────────────


class TestAbortBudgetExhausted:
    """`_abort_budget_exhausted` writes state with abort_reason +
    last_status=incomplete + attempt_count++. SIGKILL-recovery sweep
    and max-attempts gate downstream depend on this exact shape.
    """

    def test_writes_state_with_correct_shape(self, sb_module, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(sb_module, "STATE_DIR", state_dir):
            state = {
                "key": "MOL-999", "attempt_count": 2,
                "last_status": "running", "first_seen": "2026-05-11T00:00:00+00:00",
                "last_attempt_ts": "2026-05-11T01:00:00+00:00",
            }
            tracker = sb_module.BudgetTracker.start_now(budget_seconds=3300)
            with patch.object(sb_module, "_log_event"):
                sb_module._abort_budget_exhausted(
                    "MOL-999", state, "builder", tracker,
                )

        assert state["last_status"] == "incomplete"
        assert state["abort_reason"] == "global_budget_exhausted"
        assert state["attempt_count"] == 3

        captured = capsys.readouterr()
        assert "MOL-999" in captured.out
        assert "global budget exhausted" in captured.out
        assert "builder" in captured.out

    def test_increments_attempt_count_from_missing(self, sb_module, tmp_path):
        """State dict without `attempt_count` — abort uses .get(...,0) so
        a malformed state file doesn't crash the abort path."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(sb_module, "STATE_DIR", state_dir):
            state = {"key": "MOL-998"}
            tracker = sb_module.BudgetTracker.start_now(budget_seconds=3300)
            with patch.object(sb_module, "_log_event"):
                sb_module._abort_budget_exhausted(
                    "MOL-998", state, "planner", tracker,
                )
        assert state["attempt_count"] == 1


class TestAbortStderrBannerSurvivesFailures:
    """Banner-first ordering invariant: stderr emit happens BEFORE
    write_state, so a write_state OSError or _log_event failure can't
    silence the abort. Defeats the silent-SIGKILL regression the helper
    was added to make loud.
    """

    def test_banner_is_first_write(self, sb_module, capsys, tmp_path):
        """At the moment write_state is invoked, stderr already contains
        the banner. Order-of-effect locked, not just presence."""
        observed_stderr_at_write_state: list[str] = []

        def capture_stderr_at_write(*_args, **_kwargs):
            observed_stderr_at_write_state.append(capsys.readouterr().err)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(sb_module, "STATE_DIR", state_dir):
            state = {"key": "MOL-Z", "attempt_count": 0}
            tracker = sb_module.BudgetTracker.start_now(budget_seconds=3300)
            with patch.object(sb_module, "_log_event"), \
                 patch.object(sb_module, "write_state",
                              side_effect=capture_stderr_at_write):
                sb_module._abort_budget_exhausted(
                    "MOL-Z", state, "builder", tracker,
                )

        # At write_state time, the banner had already been written to stderr.
        assert observed_stderr_at_write_state, "write_state must have been called"
        assert "global budget exhausted" in observed_stderr_at_write_state[0]
        assert "MOL-Z" in observed_stderr_at_write_state[0]

    def test_abort_survives_write_state_oserror(self, sb_module, capsys):
        """write_state raises OSError (disk full, sandbox-deny). Abort
        must still emit the banner + log the write-failure event."""
        state = {"key": "MOL-Y", "attempt_count": 0}
        tracker = sb_module.BudgetTracker.start_now(budget_seconds=3300)
        events: list[dict] = []

        def capture(event, **fields):
            events.append({"event": event, **fields})

        def _raise_oserror(*_args, **_kwargs):
            raise OSError("ENOSPC: no space left on device")

        with patch.object(sb_module, "_log_event", side_effect=capture), \
             patch.object(sb_module, "write_state", side_effect=_raise_oserror):
            sb_module._abort_budget_exhausted("MOL-Y", state, "planner", tracker)

        captured = capsys.readouterr()
        assert "MOL-Y" in captured.err
        # state_write_failed event captured via _safe_write_state.
        failure_events = [
            e for e in events if e["event"] == "state_write_failed"
        ]
        assert len(failure_events) == 1
        assert "ENOSPC" in failure_events[0]["exc"]
        # And the budget_exhausted_abort event still fires.
        abort_events = [e for e in events if e["event"] == "budget_exhausted_abort"]
        assert len(abort_events) == 1


# ──────────────────────────────────────────────────────────────────────────
# CR-1 — reviewer-gate abort preserves orphan PR number
# ──────────────────────────────────────────────────────────────────────────


class TestAbortPreservesOrphanPrNum:
    """When the reviewer gate fires the abort because the budget can't
    cover Phase 4, the builder has already opened a PR. The PR number
    must survive to disk so the next tick (or a manual sweep) can find
    and triage the un-reviewed PR instead of leaving it orphaned."""

    def test_orphan_pr_num_persists_to_state_file(self, sb_module, tmp_path):
        import json
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(sb_module, "STATE_DIR", state_dir):
            state = {
                "key": "MOL-Q", "attempt_count": 0,
                "orphan_pr_num": "PR-42",
            }
            tracker = sb_module.BudgetTracker.start_now(budget_seconds=3300)
            with patch.object(sb_module, "_log_event"):
                sb_module._abort_budget_exhausted(
                    "MOL-Q", state, "reviewer", tracker,
                )

        persisted_path = state_dir / "MOL-Q.json"
        assert persisted_path.exists()
        persisted = json.loads(persisted_path.read_text())
        assert persisted["orphan_pr_num"] == "PR-42"
        assert persisted["abort_reason"] == "global_budget_exhausted"
        assert persisted["last_status"] == "incomplete"
        assert persisted["attempt_count"] == 1


# ──────────────────────────────────────────────────────────────────────────
# _safe_write_state — broad coverage for the new helper
# ──────────────────────────────────────────────────────────────────────────


class TestSafeWriteState:
    """`_safe_write_state` wraps write_state with failure observability.
    Used at every main() dispatch checkpoint so a state write that fails
    (disk full, sandbox-deny, non-serializable field) gets the loud
    treatment instead of propagating to the rescue loop."""

    def test_success_returns_true(self, sb_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(sb_module, "STATE_DIR", state_dir):
            ok = sb_module._safe_write_state(
                "MOL-OK", {"key": "MOL-OK"}, where="test_success",
            )
        assert ok is True

    def test_failure_returns_false_and_emits_event(self, sb_module, capsys):
        events: list[dict] = []

        def capture(event, **fields):
            events.append({"event": event, **fields})

        def _raise(*_args, **_kwargs):
            raise OSError("EACCES: permission denied")

        with patch.object(sb_module, "_log_event", side_effect=capture), \
             patch.object(sb_module, "write_state", side_effect=_raise):
            ok = sb_module._safe_write_state(
                "MOL-X", {"key": "MOL-X"}, where="test_failure",
            )

        assert ok is False
        captured = capsys.readouterr()
        assert "MOL-X" in captured.err
        assert "test_failure" in captured.err
        failure = [e for e in events if e["event"] == "state_write_failed"]
        assert len(failure) == 1
        assert failure[0]["where"] == "test_failure"
        assert failure[0]["exc_type"] == "OSError"


# ──────────────────────────────────────────────────────────────────────────
# main() — integration tests for the 6-gate dispatch chain
# ──────────────────────────────────────────────────────────────────────────


class TestMainDispatchGates:
    """End-to-end behavioral coverage of the 6 `tracker.can_start_phase(...)`
    gates in main(). The verifier locks gate COUNT (>=6) but a sign-inversion
    bug (`if` vs `if not`) is structurally invisible. These tests lock the
    abort-after-gate-fail flow per phase boundary."""

    def _build_minimal_main_env(self, sb_module, tmp_path):
        """Wire up the minimum mock surface so main() can run to a
        specific gate decision without invoking real subprocesses."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        return state_dir, plans_dir

    @pytest.mark.parametrize(
        "gate_to_fail,expected_phase_in_state",
        [
            ("planner", "planner"),
            ("skeptic", "skeptic"),
            ("builder", "builder"),
            ("reviewer", "reviewer"),
        ],
    )
    def test_gate_failure_aborts_with_correct_phase(
        self, sb_module, tmp_path, gate_to_fail, expected_phase_in_state,
    ):
        """When `can_start_phase(X)` returns False, the abort helper must
        be called with phase_name=X. A bug that aborts at the wrong gate
        (e.g., reviewer-gate-fail labels as builder) corrupts forensics."""
        import json
        state_dir, plans_dir = self._build_minimal_main_env(sb_module, tmp_path)

        # Track which phase the abort was called for. The fake mirrors
        # the real abort behavior (write state to disk) so post-test
        # state-file assertions reflect the real dispatch flow.
        abort_calls: list[str] = []

        def fake_abort(key, state, phase_name, tracker):
            abort_calls.append(phase_name)
            state["last_status"] = "incomplete"
            state["abort_reason"] = "global_budget_exhausted"
            state["attempt_count"] = state.get("attempt_count", 0) + 1
            sb_module.write_state(key, state)

        # gate_to_fail decides which can_start_phase returns False.
        original_can_start = sb_module.BudgetTracker.can_start_phase

        def fake_can_start_phase(self, phase):
            if phase == gate_to_fail:
                return False
            return True

        # Stub phases so they all return success — gate is the only failure.
        def fake_phase_planner(*args, **kwargs):
            return {"ok": True, "plan_content": "stub plan content", "tier": 1}

        def fake_phase_skeptic(*args, **kwargs):
            return {"ok": True, "verdict": "SHIP IT", "output": "VERDICT: SHIP IT",
                    "tier": 1}

        def fake_phase_builder(*args, **kwargs):
            return {"ok": True, "pr_num": "42", "output": "PR_NUM=42", "tier": 1}

        def fake_phase_reviewer(*args, **kwargs):
            return {"ok": True, "passed": True, "gate": 0, "output": "REVIEW: PASS",
                    "tier": 1}

        def fake_query_jira():
            return ["MOL-TEST"]

        def fake_get_ticket_body(key):
            return f"# {key}\n\nrepo: ~/Code/hermes-poc\n"

        def fake_extract_repo(body):
            return "/tmp"

        def fake_get_ticket_summary(key, body):
            return "test summary"

        with patch.object(sb_module, "STATE_DIR", state_dir), \
             patch.object(sb_module, "PLANS_DIR", plans_dir), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(False)), \
             patch.object(sb_module, "PAUSED_FILE", _fake_dry_run_file(False)), \
             patch.object(sb_module.BudgetTracker, "can_start_phase",
                          fake_can_start_phase), \
             patch.object(sb_module, "_abort_budget_exhausted", side_effect=fake_abort), \
             patch.object(sb_module, "phase_planner", side_effect=fake_phase_planner), \
             patch.object(sb_module, "phase_skeptic", side_effect=fake_phase_skeptic), \
             patch.object(sb_module, "phase_builder", side_effect=fake_phase_builder), \
             patch.object(sb_module, "phase_reviewer", side_effect=fake_phase_reviewer), \
             patch.object(sb_module, "query_jira", side_effect=fake_query_jira), \
             patch.object(sb_module, "get_ticket_body", side_effect=fake_get_ticket_body), \
             patch.object(sb_module, "extract_repo", side_effect=fake_extract_repo), \
             patch.object(sb_module, "get_ticket_summary",
                          side_effect=fake_get_ticket_summary), \
             patch.object(sb_module, "_sigkill_recovery_sweep", return_value=0), \
             patch.object(sb_module, "_preflight_probe"), \
             patch.object(sb_module, "preflight_health") as fake_ph:
            fake_ph.check.return_value = (True, {})
            sb_module.main()

        assert abort_calls == [expected_phase_in_state]
        persisted = json.loads((state_dir / "MOL-TEST.json").read_text())
        assert persisted["last_status"] == "incomplete"
        assert persisted["abort_reason"] == "global_budget_exhausted"

    def test_reviewer_gate_fail_records_orphan_pr_num(self, sb_module, tmp_path):
        """The reviewer gate is special — the builder has already opened
        a PR. main() must record state["orphan_pr_num"] BEFORE invoking
        the abort helper so the PR number lands on disk."""
        import json
        state_dir, plans_dir = self._build_minimal_main_env(sb_module, tmp_path)

        captured_state: dict = {}

        def fake_abort(key, state, phase_name, tracker):
            captured_state.update(state)
            state["last_status"] = "incomplete"
            state["abort_reason"] = "global_budget_exhausted"

        def fake_can_start_phase(self, phase):
            return phase != "reviewer"

        def fake_phase_planner(*args, **kwargs):
            return {"ok": True, "plan_content": "stub plan content", "tier": 1}

        def fake_phase_skeptic(*args, **kwargs):
            return {"ok": True, "verdict": "SHIP IT", "output": "VERDICT: SHIP IT",
                    "tier": 1}

        def fake_phase_builder(*args, **kwargs):
            return {"ok": True, "pr_num": "1234", "output": "PR_NUM=1234", "tier": 1}

        def fake_query_jira():
            return ["MOL-ORPHAN"]

        def fake_get_ticket_body(key):
            return f"# {key}\n\nrepo: ~/Code/hermes-poc\n"

        with patch.object(sb_module, "STATE_DIR", state_dir), \
             patch.object(sb_module, "PLANS_DIR", plans_dir), \
             patch.object(sb_module, "DRY_RUN_FILE", _fake_dry_run_file(False)), \
             patch.object(sb_module, "PAUSED_FILE", _fake_dry_run_file(False)), \
             patch.object(sb_module.BudgetTracker, "can_start_phase",
                          fake_can_start_phase), \
             patch.object(sb_module, "_abort_budget_exhausted", side_effect=fake_abort), \
             patch.object(sb_module, "phase_planner", side_effect=fake_phase_planner), \
             patch.object(sb_module, "phase_skeptic", side_effect=fake_phase_skeptic), \
             patch.object(sb_module, "phase_builder", side_effect=fake_phase_builder), \
             patch.object(sb_module, "query_jira", side_effect=fake_query_jira), \
             patch.object(sb_module, "get_ticket_body", side_effect=fake_get_ticket_body), \
             patch.object(sb_module, "extract_repo", return_value="/tmp"), \
             patch.object(sb_module, "get_ticket_summary", return_value="summary"), \
             patch.object(sb_module, "_sigkill_recovery_sweep", return_value=0), \
             patch.object(sb_module, "_preflight_probe"), \
             patch.object(sb_module, "preflight_health") as fake_ph:
            fake_ph.check.return_value = (True, {})
            sb_module.main()

        # At the moment the abort helper was called, orphan_pr_num was already set.
        assert captured_state.get("orphan_pr_num") == "1234"


# ──────────────────────────────────────────────────────────────────────────
# run_one + RunResult (P154/MOL-509)
# ──────────────────────────────────────────────────────────────────────────


class TestRunResultContract:
    """The RunResult dataclass is the bridge between run_one and its
    callers (cron main loop today; daemon tomorrow). Lock the shape."""

    def test_run_result_is_frozen(self, sb_module):
        """RunResult must be immutable — callers must not be able to
        mutate the result after run_one returns it. Frozen dataclasses
        raise dataclasses.FrozenInstanceError, which is a subclass of
        AttributeError. Tightened from (AttributeError, Exception) per
        PR #176 review-pass-1."""
        # "succeeded" requires pr_state="merged" + pr_num set per __post_init__.
        result = sb_module.RunResult(
            dispatched=True, final_status="succeeded",
            pr_num="42", pr_state="merged",
        )
        with pytest.raises(AttributeError):
            result.final_status = "failed"

    def test_post_init_rejects_unknown_final_status(self, sb_module):
        """Literal[...] is only mypy-enforced; __post_init__ guards
        runtime construction so daemon/cron callers can't pass a typo."""
        with pytest.raises(ValueError, match="not in FinalStatus"):
            sb_module.RunResult(dispatched=False, final_status="skipped_typo")

    def test_post_init_rejects_dispatched_false_with_non_skip_status(
        self, sb_module,
    ):
        """dispatched=False is only valid with skipped_*. A bug that
        returns RunResult(dispatched=False, final_status='failed') would
        cause main()'s loop to treat the failure as a skip — silent."""
        with pytest.raises(ValueError, match="dispatched=False requires"):
            sb_module.RunResult(dispatched=False, final_status="failed")

    def test_post_init_rejects_succeeded_without_pr_state_merged(
        self, sb_module,
    ):
        """succeeded ⇒ pr_state='merged'. Constructing succeeded without
        a merged PR would silently mis-state the outcome."""
        with pytest.raises(ValueError, match="requires pr_state='merged'"):
            sb_module.RunResult(
                dispatched=True, final_status="succeeded",
                pr_num="42", pr_state="open",
            )

    def test_post_init_rejects_succeeded_without_pr_num(self, sb_module):
        """succeeded ⇒ pr_num is not None."""
        with pytest.raises(ValueError, match="requires pr_num"):
            sb_module.RunResult(
                dispatched=True, final_status="succeeded",
                pr_state="merged",  # pr_num omitted → defaults to None
            )

    def test_run_result_required_fields(self, sb_module):
        """dispatched + final_status are mandatory; everything else
        defaults to None so callers don't have to spell out unused fields."""
        result = sb_module.RunResult(dispatched=False,
                                     final_status="skipped_max_attempts")
        assert result.dispatched is False
        assert result.final_status == "skipped_max_attempts"
        assert result.pr_num is None
        assert result.pr_state is None
        assert result.error is None
        assert result.detail_line is None

    def test_run_result_all_fields_explicit(self, sb_module):
        """A regression that renames a field (e.g., `pr_num` → `pr_number`)
        is invisible to the two-field-only tests above. Pin every field by
        passing all six on construction."""
        result = sb_module.RunResult(
            dispatched=True,
            final_status="succeeded",
            pr_num="42",
            pr_state="merged",
            error=None,
            detail_line="- Dispatched: MOL-FOO (status=succeeded)",
        )
        assert result.dispatched is True
        assert result.final_status == "succeeded"
        assert result.pr_num == "42"  # str, not int (matches phase_builder regex group)
        assert result.pr_state == "merged"
        assert result.error is None
        assert result.detail_line.startswith("- Dispatched:")


class TestRunOneSkipLogic:
    """run_one's skip-logic (max_attempts, recently-running) lives inside
    the function because BOTH cron and daemon need it. These tests lock
    the RunResult shape per skip-branch."""

    def test_max_attempts_returns_skipped_not_dispatched(
        self, sb_module, tmp_path, capsys,
    ):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        (state_dir / "MOL-MAX.json").write_text(json.dumps({
            "key": "MOL-MAX",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_attempt_ts": "2026-05-10T00:00:00+00:00",
            "attempt_count": 3,
            "last_status": "failed",
        }))

        with patch.object(sb_module, "STATE_DIR", state_dir):
            result = sb_module.run_one("MOL-MAX")

        assert result.dispatched is False
        assert result.final_status == "skipped_max_attempts"
        # Loud-warn emitted to stdout per existing contract.
        out = capsys.readouterr().out
        assert "max attempts exceeded" in out

    def test_recently_running_returns_skipped_not_dispatched(
        self, sb_module, tmp_path,
    ):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        (state_dir / "MOL-RECENT.json").write_text(json.dumps({
            "key": "MOL-RECENT",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_attempt_ts": recent,
            "attempt_count": 1,
            "last_status": "running",
        }))

        with patch.object(sb_module, "STATE_DIR", state_dir):
            result = sb_module.run_one("MOL-RECENT")

        assert result.dispatched is False
        assert result.final_status == "skipped_recently_running"

    def test_succeeded_in_progress_returns_loud_skip(
        self, sb_module, tmp_path, capsys,
    ):
        """state=succeeded + Jira shows 'In Progress' → loud-skip with
        stderr/stdout banner. Pre-P154 only verified by string-grep in
        the verifier; this locks the control flow."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        (state_dir / "MOL-LOUD.json").write_text(json.dumps({
            "key": "MOL-LOUD",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_attempt_ts": "2026-05-10T00:00:00+00:00",
            "attempt_count": 1,
            "last_status": "succeeded",
        }))

        def fake_run_jira(args, timeout=30):
            # Simulate `jira issue view MOL-LOUD --plain` returning
            # a body still showing "In Progress" status.
            return (True, "Status: In Progress\nSummary: ...", "")

        with patch.object(sb_module, "STATE_DIR", state_dir), \
             patch.object(sb_module, "_run_jira", side_effect=fake_run_jira):
            result = sb_module.run_one("MOL-LOUD")

        assert result.dispatched is False
        assert result.final_status == "skipped_succeeded_in_progress"
        out = capsys.readouterr().out
        assert "marked succeeded but still In Progress" in out

    def test_succeeded_progressed_returns_silent_skip(
        self, sb_module, tmp_path, capsys,
    ):
        """state=succeeded + Jira has progressed past 'In Progress'
        (e.g., Done/Testing) → silent-skip, no banner."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import json
        (state_dir / "MOL-SILENT.json").write_text(json.dumps({
            "key": "MOL-SILENT",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_attempt_ts": "2026-05-10T00:00:00+00:00",
            "attempt_count": 1,
            "last_status": "succeeded",
        }))

        def fake_run_jira(args, timeout=30):
            return (True, "Status: Done\nSummary: ...", "")

        with patch.object(sb_module, "STATE_DIR", state_dir), \
             patch.object(sb_module, "_run_jira", side_effect=fake_run_jira):
            result = sb_module.run_one("MOL-SILENT")

        assert result.dispatched is False
        assert result.final_status == "skipped_progressed"
        # silent-skip: no user-facing stderr banner
        out = capsys.readouterr().out
        assert "marked succeeded" not in out


class TestRunOneDryRun:
    """DRY_RUN mode short-circuits before the LIVE dispatch block. No
    state mutation; a detail_line for the cron summary."""

    def test_dry_run_returns_dispatched_with_detail_line(
        self, sb_module, tmp_path,
    ):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with patch.object(sb_module, "STATE_DIR", state_dir):
            result = sb_module.run_one("MOL-DRY", dry_run=True)

        assert result.dispatched is True
        assert result.final_status == "dry_run"
        assert result.detail_line == "- Would-dispatch: MOL-DRY (DRY RUN)"
        # No state file written under DRY_RUN.
        assert not (state_dir / "MOL-DRY.json").exists()


class TestRunOneCallableStandalone:
    """run_one() is the primitive both daemon (P156) and cron (current
    main()) call. Lock that it's importable + invocable without any of
    main()'s pre-flight machinery."""

    def test_run_one_is_module_level_function(self, sb_module):
        assert callable(sb_module.run_one)
        # Signature: (key, *, dry_run=False) -> RunResult
        import inspect
        sig = inspect.signature(sb_module.run_one)
        assert "key" in sig.parameters
        assert "dry_run" in sig.parameters
        assert sig.parameters["dry_run"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_run_one_rejects_positional_dry_run(self, sb_module):
        """Verifier grep-locks the literal `*,` in the signature, but
        only behavior proves keyword-only is enforced. A regression
        that drops the `*` would still match the literal if the rest
        of the signature is intact via a different code path."""
        with pytest.raises(TypeError):
            sb_module.run_one("MOL-TEST", True)  # dry_run positional → rejected


class TestRunOneLiveDispatch:
    """The strangler-fig refactor's load-bearing claim is `run_one`
    preserves main()'s pre-P154 dispatch semantics. The 4-gate aborts
    are covered by TestMainDispatchGates (via main → run_one). These
    cases cover the success path + the gh-pr-merge-nonzero branch
    DIRECTLY at the run_one level."""

    def _build_state_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        return state_dir, plans_dir

    def _phase_stubs(self, sb_module):
        """Stubs for the 4 phase functions + ticket/repo helpers."""
        return {
            "phase_planner": lambda *a, **kw: {
                "ok": True, "plan_content": "stub plan", "tier": 1,
            },
            "phase_skeptic": lambda *a, **kw: {
                "ok": True, "verdict": "SHIP IT",
                "output": "VERDICT: SHIP IT", "tier": 1,
            },
            "phase_builder": lambda *a, **kw: {
                "ok": True, "pr_num": "42", "output": "PR_NUM=42", "tier": 1,
            },
            "phase_reviewer": lambda *a, **kw: {
                "ok": True, "passed": True, "gate": 0,
                "output": "REVIEW: PASS", "tier": 1,
            },
            "get_ticket_body": lambda key: f"# {key}\n\nrepo: ~/Code/hermes-poc\n",
            "extract_repo": lambda body: "/tmp",
            "get_ticket_summary": lambda key, body: "test summary",
        }

    def test_all_gates_pass_returns_succeeded(self, sb_module, tmp_path):
        """Happy path: phases 1-4 succeed AND gh pr merge returns 0 →
        RunResult(dispatched=True, final_status='succeeded',
                 pr_num='42', pr_state='merged'). The verifier locks
        the signature/markers but never the success-path RunResult
        shape; this is the only direct success-path test."""
        state_dir, plans_dir = self._build_state_dir(tmp_path)
        stubs = self._phase_stubs(sb_module)

        # Simulate `gh pr merge` returning 0 (success).
        def fake_subprocess_run(*args, **kwargs):
            return Mock(returncode=0, stderr="", stdout="merged")

        patches = [
            patch.object(sb_module, "STATE_DIR", state_dir),
            patch.object(sb_module, "PLANS_DIR", plans_dir),
            patch.object(sb_module, "subprocess",
                         Mock(run=fake_subprocess_run,
                              TimeoutExpired=Exception)),
        ]
        for name, fn in stubs.items():
            patches.append(patch.object(sb_module, name, side_effect=fn))

        for p in patches:
            p.start()
        try:
            result = sb_module.run_one("MOL-OK")
        finally:
            for p in patches:
                p.stop()

        assert result.dispatched is True
        assert result.final_status == "succeeded"
        assert result.pr_num == "42"
        assert result.pr_state == "merged"

    def test_gh_pr_merge_nonzero_marks_pr_state_open(
        self, sb_module, tmp_path, capsys,
    ):
        """Reviewer passes (gate=0) but `gh pr merge` returns non-zero
        (branch protection, auth fail, conflict). Per the contract-
        guard pattern, pr_state must stay 'open' — pre-fix this was a
        silent failure marking it 'merged' regardless. Locks the
        comment at symphony_bridge.py: 'Capturing the exit code
        prevents falsely marking the state as merged'."""
        state_dir, plans_dir = self._build_state_dir(tmp_path)
        stubs = self._phase_stubs(sb_module)

        def fake_subprocess_run(*args, **kwargs):
            return Mock(returncode=1, stderr="ERROR: branch protected\n",
                        stdout="")

        patches = [
            patch.object(sb_module, "STATE_DIR", state_dir),
            patch.object(sb_module, "PLANS_DIR", plans_dir),
            patch.object(sb_module, "subprocess",
                         Mock(run=fake_subprocess_run,
                              TimeoutExpired=Exception)),
        ]
        for name, fn in stubs.items():
            patches.append(patch.object(sb_module, name, side_effect=fn))

        for p in patches:
            p.start()
        try:
            result = sb_module.run_one("MOL-MERGEFAIL")
        finally:
            for p in patches:
                p.stop()

        # PR open, dispatch incomplete, attempt_count incremented.
        assert result.dispatched is True
        assert result.final_status == "incomplete"
        assert result.pr_num == "42"
        assert result.pr_state == "open"
        # stderr banner emitted (last-resort channel for merge failure).
        err = capsys.readouterr().err
        assert "gh pr merge" in err


class TestRunOneReviseFlow:
    """Phase 2 verdict='REVISE' re-runs Phase 1 with skeptic findings,
    then re-runs Phase 2. The deepest non-error control flow in
    run_one(); zero coverage pre-fix-pass-1."""

    def test_revise_verdict_re_runs_planner_with_findings(
        self, sb_module, tmp_path,
    ):
        """First skeptic call returns REVISE; second returns SHIP IT.
        Asserts: planner called twice, second call's ticket_body
        contains 'Skeptic findings:' from first skeptic's output."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()

        planner_calls: list[str] = []
        skeptic_call_count = [0]

        def fake_planner(key, summary, ticket_body, repo_path, tracker=None):
            planner_calls.append(ticket_body)
            return {"ok": True, "plan_content": "plan v" + str(len(planner_calls)),
                    "tier": 1}

        def fake_skeptic(key, repo_path, tracker=None):
            skeptic_call_count[0] += 1
            if skeptic_call_count[0] == 1:
                return {"ok": True, "verdict": "REVISE",
                        "output": "Issues: X needs Y", "tier": 1}
            return {"ok": True, "verdict": "SHIP IT",
                    "output": "VERDICT: SHIP IT", "tier": 1}

        def fake_builder(*args, **kwargs):
            return {"ok": True, "pr_num": "99", "output": "PR_NUM=99",
                    "tier": 1}

        def fake_reviewer(*args, **kwargs):
            return {"ok": True, "passed": True, "gate": 0,
                    "output": "REVIEW: PASS", "tier": 1}

        def fake_subprocess_run(*args, **kwargs):
            return Mock(returncode=0, stderr="", stdout="merged")

        with patch.object(sb_module, "STATE_DIR", state_dir), \
             patch.object(sb_module, "PLANS_DIR", plans_dir), \
             patch.object(sb_module, "phase_planner", side_effect=fake_planner), \
             patch.object(sb_module, "phase_skeptic", side_effect=fake_skeptic), \
             patch.object(sb_module, "phase_builder", side_effect=fake_builder), \
             patch.object(sb_module, "phase_reviewer", side_effect=fake_reviewer), \
             patch.object(sb_module, "get_ticket_body",
                          side_effect=lambda k: f"# {k}\nrepo: ~/Code/hermes-poc\n"), \
             patch.object(sb_module, "extract_repo", return_value="/tmp"), \
             patch.object(sb_module, "get_ticket_summary",
                          return_value="test summary"), \
             patch.object(sb_module, "subprocess",
                          Mock(run=fake_subprocess_run,
                               TimeoutExpired=Exception)):
            result = sb_module.run_one("MOL-REVISE")

        # planner ran twice: once initially, once with skeptic findings.
        assert len(planner_calls) == 2
        # Second call's ticket_body must include the REVISE findings.
        assert "Skeptic findings:" in planner_calls[1]
        assert "Issues: X needs Y" in planner_calls[1]
        # End-state: succeeded (skeptic-2 says SHIP IT, builder/reviewer pass).
        assert result.dispatched is True
        assert result.final_status == "succeeded"
        assert result.pr_num == "99"


# P163/MOL-523: HERMES_BUDGET_DISABLED env-flag for E2E shakeout.
# Covers the contract that the flag short-circuits can_start_phase() AND
# remaining(). Tests both sides so a future refactor can't silently break
# one without breaking the test suite.

class TestBudgetDisabled:
    """The HERMES_BUDGET_DISABLED env-flag must:
    1. Default OFF — absent env var = current production behavior.
    2. Recognize standard truthy strings (1, true, yes, on) — case-insensitive.
    3. Recognize standard falsy strings (0, false, no, off, "") as still-off.
    4. **Fail closed on unrecognized values** — typos/garbage stay OFF.
    5. When ON: remaining() returns math.inf (in-process numeric API).
    6. When ON: can_start_phase(any phase) returns True even if elapsed >> budget.
    7. When ON: to_summary() includes disabled=True AND emits remaining_secs=None
       (RFC 8259 JSON-safe — math.inf would serialize to non-standard `Infinity`).
    8. When OFF: pre-patch behavior unchanged (regression guard).
    """

    def test_helper_default_is_false(self, sb_module, monkeypatch):
        monkeypatch.delenv("HERMES_BUDGET_DISABLED", raising=False)
        assert sb_module._budget_disabled() is False

    def test_helper_truthy_values(self, sb_module, monkeypatch):
        # Tri-state, fail-closed: only the canonical truthy set returns True.
        for v in ("1", "true", "True", "TRUE", "yes", "Yes", "on", "ON"):
            monkeypatch.setenv("HERMES_BUDGET_DISABLED", v)
            assert sb_module._budget_disabled() is True, f"value={v!r} should be truthy"

    def test_helper_explicit_falsy_values(self, sb_module, monkeypatch):
        for v in ("0", "false", "FALSE", "no", "NO", "off", ""):
            monkeypatch.setenv("HERMES_BUDGET_DISABLED", v)
            assert sb_module._budget_disabled() is False, f"value={v!r} should be falsy"

    def test_helper_unrecognized_values_fail_closed(self, sb_module, monkeypatch):
        """P163 tri-state safety: typos and garbage must NOT silently disable
        the safety gate. An operator who types `=disabled` (intending OFF) or
        `=enable` (intending ON) gets gate-active either way. To actually
        disable, they must type a recognized truthy value."""
        for v in (
            "disable", "disabled", "enable", "enabled",
            "null", "None", "0.0", "2", "-1",
            "tru", "fals", "y", "n",
            "tr ue",  # internal whitespace breaks recognition (strip only trims outer)
            "anything-non-falsy",  # would have been True pre-tri-state
        ):
            monkeypatch.setenv("HERMES_BUDGET_DISABLED", v)
            assert sb_module._budget_disabled() is False, (
                f"value={v!r} unrecognized; must fail closed (gate active)"
            )

    def test_helper_strips_surrounding_whitespace(self, sb_module, monkeypatch):
        # Outer whitespace is stripped so plist values with stray spaces still
        # parse correctly. Internal whitespace is not stripped (see
        # test_helper_unrecognized_values_fail_closed).
        for v in (" 1", "1 ", "  true  ", "\tyes\n"):
            monkeypatch.setenv("HERMES_BUDGET_DISABLED", v)
            assert sb_module._budget_disabled() is True, f"value={v!r} should strip+truthy"

    def test_remaining_returns_inf_when_disabled(self, sb_module, monkeypatch):
        monkeypatch.setenv("HERMES_BUDGET_DISABLED", "1")
        # Build a tracker with budget effectively exhausted by mocking
        # _start_monotonic to a value far in the past.
        tracker = sb_module.BudgetTracker(
            budget_seconds=10, min_phase_budget=5, start_monotonic=0.0,
        )
        # time.monotonic() returns a large positive number -> elapsed >>> budget
        # -> remaining() pre-patch would be 0.0. With disable flag, it's inf.
        import math
        assert tracker.remaining() == math.inf

    def test_can_start_phase_always_true_when_disabled(self, sb_module, monkeypatch):
        monkeypatch.setenv("HERMES_BUDGET_DISABLED", "1")
        # Tracker with no remaining time (pre-patch) -- builder needs 1800s.
        tracker = sb_module.BudgetTracker(
            budget_seconds=10, min_phase_budget=5, start_monotonic=0.0,
        )
        # Pre-patch this would be False (10s budget < 1800s builder floor).
        # With flag on, MUST be True.
        assert tracker.can_start_phase("builder") is True
        assert tracker.can_start_phase("planner") is True
        assert tracker.can_start_phase("skeptic") is True
        assert tracker.can_start_phase("reviewer") is True

    def test_to_summary_includes_disabled_flag(self, sb_module, monkeypatch):
        monkeypatch.setenv("HERMES_BUDGET_DISABLED", "1")
        tracker = sb_module.BudgetTracker.start_now(
            budget_seconds=3300, min_phase_budget=300,
        )
        summary = tracker.to_summary()
        assert summary["disabled"] is True
        # P163 JSON-safe: emits None (→ JSON null), NOT math.inf (→ non-standard `Infinity`).
        assert summary["remaining_secs"] is None
        # budget_secs still shows the construction-time budget for context
        assert summary["budget_secs"] == 3300

    def test_to_summary_when_disabled_is_json_serializable(self, sb_module, monkeypatch):
        """Strict consumers (Go `encoding/json`, browser `JSON.parse`, ELK)
        reject `Infinity`. The disabled summary must round-trip through
        standard JSON without precision loss or rejection."""
        import json
        monkeypatch.setenv("HERMES_BUDGET_DISABLED", "1")
        tracker = sb_module.BudgetTracker.start_now(
            budget_seconds=3300, min_phase_budget=300,
        )
        summary = tracker.to_summary()
        # allow_nan=False is RFC 8259 strict mode; pre-patch math.inf would raise.
        encoded = json.dumps(summary, allow_nan=False)
        decoded = json.loads(encoded)
        assert decoded["disabled"] is True
        assert decoded["remaining_secs"] is None

    def test_to_summary_when_enabled_no_disabled_key(self, sb_module, monkeypatch):
        monkeypatch.delenv("HERMES_BUDGET_DISABLED", raising=False)
        tracker = sb_module.BudgetTracker.start_now(
            budget_seconds=3300, min_phase_budget=300,
        )
        summary = tracker.to_summary()
        # Regression guard: enabled path produces the pre-patch summary shape.
        assert "disabled" not in summary
        assert isinstance(summary["remaining_secs"], (int, float))
        assert summary["remaining_secs"] > 0

    def test_disabled_does_not_affect_construction_validation(self, sb_module, monkeypatch):
        """The flag bypasses runtime gating, NOT construction-time invariants.
        Negative budget / out-of-range min_phase still raise ValueError even
        when disabled - loud failure on misconfig is preserved."""
        monkeypatch.setenv("HERMES_BUDGET_DISABLED", "1")
        import pytest
        with pytest.raises(ValueError):
            sb_module.BudgetTracker(
                budget_seconds=-1, min_phase_budget=5, start_monotonic=0.0,
            )
        with pytest.raises(ValueError):
            sb_module.BudgetTracker(
                budget_seconds=10, min_phase_budget=20, start_monotonic=0.0,
            )

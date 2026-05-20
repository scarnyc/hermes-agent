"""Tests for tools/runtime_fingerprint.py — P181 H1 auto-recovery."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.runtime_fingerprint import (
    h1_pre_write_guard,
    h1_record_post_write,
    load_last_hermes_hashes,
    STATE_DIR,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_state_dir(tmp_path):
    """Redirect STATE_DIR to a temp directory for isolated testing."""
    with patch(
        "tools.runtime_fingerprint.STATE_DIR",
        tmp_path / "state",
    ):
        (tmp_path / "state" / "session-fingerprints").mkdir(parents=True)
        # Write a valid hash state so load_last_hermes_hashes doesn't return {}
        hash_file = tmp_path / "state" / "hermes-last-write-hashes.json"
        hash_file.write_text(json.dumps({
            "/fake/test-file.md": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }))
        yield tmp_path


def _write_fingerprint(tmp_path, session_id, hashes):
    """Write a session fingerprint file with the given hashes."""
    fp_dir = tmp_path / "state" / "session-fingerprints"
    fp_dir.mkdir(parents=True, exist_ok=True)
    fp_path = fp_dir / f"{session_id}.json"
    fp_path.write_text(json.dumps({
        "ts": "2026-05-20T12:00:00Z",
        "session_id": session_id,
        "hashes": hashes,
    }))


def _stale_baseline(hash_val):
    """Return a stale baseline lookup that returns the given value for any key."""
    def _lookup():
        return {"/fake/test-file.md": hash_val}
    return _lookup


# ── Tests ──────────────────────────────────────────────────────────────

def test_auto_heal_succeeds_when_session_fingerprint_matches_disk(
    tmp_state_dir, monkeypatch,
):
    """Stale baseline + matching session fingerprint → auto-heal returns 'proceed'."""
    STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    DISK = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    monkeypatch.setenv("HERMES_SESSION_ID", "test-session-1")

    _write_fingerprint(tmp_state_dir, "test-session-1", {
        "/fake/test-file.md": DISK,
    })

    with patch(
        "tools.runtime_fingerprint.load_last_hermes_hashes",
        _stale_baseline(STALE),
    ), patch(
        "tools.runtime_fingerprint._hash_one",
        return_value=DISK,
    ), patch(
        "tools.runtime_fingerprint._is_interactive",
        return_value=False,
    ), patch(
        "tools.runtime_fingerprint.emit_audit_jsonl",
    ), patch(
        "tools.runtime_fingerprint.h1_record_post_write",
    ) as mock_record:
        result = h1_pre_write_guard("/fake/test-file.md", caller="test_auto_heal")

    assert result == "proceed"
    mock_record.assert_called_once()


def test_abort_when_session_fingerprint_differs_from_disk(
    tmp_state_dir, monkeypatch,
):
    """Stale baseline + NON-matching session fingerprint → genuine mutation, abort."""
    STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    DISK = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    SESSION = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

    monkeypatch.setenv("HERMES_SESSION_ID", "test-session-2")

    _write_fingerprint(tmp_state_dir, "test-session-2", {
        "/fake/test-file.md": SESSION,  # different from DISK
    })

    with patch(
        "tools.runtime_fingerprint.load_last_hermes_hashes",
        _stale_baseline(STALE),
    ), patch(
        "tools.runtime_fingerprint._hash_one",
        return_value=DISK,
    ), patch(
        "tools.runtime_fingerprint._is_interactive",
        return_value=False,
    ), patch(
        "tools.runtime_fingerprint.emit_audit_jsonl",
    ), patch(
        "tools.runtime_fingerprint.h1_record_post_write",
    ) as mock_record:
        result = h1_pre_write_guard("/fake/test-file.md", caller="test_abort")

    assert result == "abort"
    mock_record.assert_not_called()


def test_abort_when_no_session_fingerprint_file(
    tmp_state_dir, monkeypatch,
):
    """Stale baseline + missing fingerprint file → graceful fallback, abort."""
    STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    DISK = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    monkeypatch.setenv("HERMES_SESSION_ID", "nonexistent-session")

    with patch(
        "tools.runtime_fingerprint.load_last_hermes_hashes",
        _stale_baseline(STALE),
    ), patch(
        "tools.runtime_fingerprint._hash_one",
        return_value=DISK,
    ), patch(
        "tools.runtime_fingerprint._is_interactive",
        return_value=False,
    ), patch(
        "tools.runtime_fingerprint.emit_audit_jsonl",
    ), patch(
        "tools.runtime_fingerprint.h1_record_post_write",
    ) as mock_record:
        result = h1_pre_write_guard("/fake/test-file.md", caller="test_missing")

    assert result == "abort"
    mock_record.assert_not_called()


def test_abort_when_hermes_session_id_not_set(
    tmp_state_dir, monkeypatch,
):
    """HERMES_SESSION_ID unset (cron/shell) → skip auto-heal, abort as before."""
    STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    DISK = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    with patch(
        "tools.runtime_fingerprint.load_last_hermes_hashes",
        _stale_baseline(STALE),
    ), patch(
        "tools.runtime_fingerprint._hash_one",
        return_value=DISK,
    ), patch(
        "tools.runtime_fingerprint._is_interactive",
        return_value=False,
    ), patch(
        "tools.runtime_fingerprint.emit_audit_jsonl",
    ), patch(
        "tools.runtime_fingerprint.h1_record_post_write",
    ) as mock_record:
        result = h1_pre_write_guard("/fake/test-file.md", caller="test_no_env")

    assert result == "abort"
    mock_record.assert_not_called()


def test_auto_heal_emits_audit_event(
    tmp_state_dir, monkeypatch,
):
    """Auto-heal emits the correct audit event with old/new baselines."""
    STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    DISK = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    monkeypatch.setenv("HERMES_SESSION_ID", "test-session-3")

    _write_fingerprint(tmp_state_dir, "test-session-3", {
        "/fake/test-file.md": DISK,
    })

    mock_emit = MagicMock()
    with patch(
        "tools.runtime_fingerprint.load_last_hermes_hashes",
        _stale_baseline(STALE),
    ), patch(
        "tools.runtime_fingerprint._hash_one",
        return_value=DISK,
    ), patch(
        "tools.runtime_fingerprint._is_interactive",
        return_value=False,
    ), patch(
        "tools.runtime_fingerprint.emit_audit_jsonl",
        mock_emit,
    ), patch(
        "tools.runtime_fingerprint.h1_record_post_write",
    ):
        h1_pre_write_guard("/fake/test-file.md", caller="test_audit")

    mock_emit.assert_called_once()
    call_args = mock_emit.call_args
    payload = call_args[0][1]  # emit_audit_jsonl(log_path, payload)
    assert payload["event"] == "stale_baseline_auto_healed"
    assert payload["old_baseline"] == STALE
    assert payload["new_baseline"] == DISK
    assert payload["session_id"] == "test-session-3"

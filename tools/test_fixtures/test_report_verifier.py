"""Tests for tools.report_verifier — P252/MOL-2040 manifest-strip coverage.

Covers the P252 change: verify_and_annotate strips the raw WORK_MANIFEST HTML
comment from the DELIVERED (returned) text AFTER _extract_manifest reads it, so
text-mode Telegram no longer shows the verbatim `<!-- WORK_MANIFEST ... -->`
block on claims_expected crons. Extraction/verification are unaffected; the saved
cron output file (written pre-verify) keeps the manifest for CLI replay.

These tests are hermetic — the empty-manifest path returns before any DB write
or filesystem verification, and the one passed-path test (which reaches
_format_header via a file_op claim against a nonexistent path) monkeypatches
_persist_results to skip the real SQLite write. No isolated_db fixture needed.

Run:
    cd ~/.hermes/hermes-agent
    ./venv/bin/python3 -m pytest tools/test_fixtures/test_report_verifier.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from tools import report_verifier as rv  # noqa: E402


_MANIFEST = (
    "<!-- WORK_MANIFEST v1\n"
    "jira_transitions:\n"
    "  - issue: MOL-9999\n"
    '    to: "In Progress"\n'
    "WORK_MANIFEST -->"
)


def test_strip_manifest_removes_comment_block():
    text = f"Knockout report body.\n\n{_MANIFEST}\n"
    out = rv._strip_manifest(text)
    assert "<!-- WORK_MANIFEST" not in out
    assert "WORK_MANIFEST -->" not in out
    assert "Knockout report body." in out


def test_strip_manifest_noop_on_trimmed_manifestless_text():
    # Plain text with no manifest and no surrounding whitespace passes through
    # unchanged. The function is NOT a pure no-op in general — it still .strip()s
    # leading/trailing whitespace — so the invariant holds only for already-
    # trimmed, manifestless input. (Mid-body blank runs are covered separately.)
    text = "Plain report with no manifest at all."
    assert rv._strip_manifest(text) == text


def test_strip_manifest_collapses_seam_blank_runs():
    # Stripping a trailing manifest collapses the seam (manifest + its
    # surrounding newlines) and must not leave a 3+ blank-line gap.
    text = f"Body line.\n\n\n{_MANIFEST}"
    out = rv._strip_manifest(text)
    assert "\n\n\n" not in out
    assert out == "Body line."


def test_strip_manifest_preserves_body_blank_runs():
    # The strip is anchored to the manifest SEAM, not global. A 3+ blank-line
    # run elsewhere in the report body (not adjacent to the manifest) must
    # survive byte-for-byte — the prior global `re.sub(r"\n{3,}", ...)` would
    # have collapsed it. This documents the P252 LOW-finding fix.
    text = f"Section A.\n\n\nSection B.\n\n{_MANIFEST}"
    out = rv._strip_manifest(text)
    assert out == "Section A.\n\n\nSection B."


def test_extract_reads_before_strip():
    # The manifest must remain machine-readable up to the moment it is stripped.
    body = rv._extract_manifest(_MANIFEST)
    assert body is not None and "MOL-9999" in body
    assert "WORK_MANIFEST" not in rv._strip_manifest(_MANIFEST)


def test_verify_and_annotate_strips_delivered_manifest():
    # Empty manifest → empty-results branch → returns the ℹ️ header + stripped
    # body. This is the exact production-leak case (abort/no-claim runs). The
    # strip statement runs once before branching, so this exercises the P252 line.
    empty_manifest = "<!-- WORK_MANIFEST v1\nWORK_MANIFEST -->"
    response = f"Autonomous knockout aborted: gateway down.\n\n{empty_manifest}"
    job = {"id": "test", "name": "knockout", "claims_expected": True}

    out = rv.verify_and_annotate(job, response, "unit_test", 0.0)

    assert "<!-- WORK_MANIFEST" not in out
    assert "WORK_MANIFEST -->" not in out
    assert "gateway down" in out


def test_verify_and_annotate_skips_when_not_opted_in():
    # No claims_expected and no whitelist → returns verbatim (manifest intact),
    # confirming the strip is gated behind opt-in and never fires unsolicited.
    response = f"Some report.\n\n{_MANIFEST}"
    job = {"id": "test", "name": "noopt"}

    out = rv.verify_and_annotate(job, response, "unit_test", 0.0)

    assert out == response


def test_verify_and_annotate_strips_on_passed_path(monkeypatch):
    # Non-empty results → _format_header branch (the empty-manifest test only
    # covers the ℹ️ early-return). A file_op claim against a nonexistent path
    # fails verification with no network/DB; monkeypatching _persist_results
    # keeps the SQLite write out of the test. Asserts the raw manifest is
    # stripped from the delivered copy AND the verification header is emitted.
    monkeypatch.setattr(rv, "_persist_results", lambda *a, **k: None)
    manifest = (
        "<!-- WORK_MANIFEST v1\n"
        "file_ops:\n"
        "  - path: /nonexistent/p252-test-file\n"
        "    action: created\n"
        "WORK_MANIFEST -->"
    )
    response = f"Knockout dispatched MOL-1234.\n\n{manifest}"
    job = {"id": "test", "name": "knockout", "claims_expected": True}

    out = rv.verify_and_annotate(job, response, "unit_test", 0.0)

    assert "<!-- WORK_MANIFEST" not in out
    assert "WORK_MANIFEST -->" not in out
    assert "Knockout dispatched MOL-1234." in out
    assert "VERIFICATION:" in out  # _format_header ran

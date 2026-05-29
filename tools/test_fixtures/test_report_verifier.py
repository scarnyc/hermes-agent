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

import subprocess
import sys
from pathlib import Path

import pytest

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


# ─────────────────────────────────────────────────────────────────────────
# P272/MOL-2219: git_commits + jira_status claim types (results-reporting).
# These are the two ground-truth checks that close the dispatch-receipt gap:
# a swarm that silently no-op'd FAILS git_commits (skeptic #1), and a ticket
# that never reached Done FAILS jira_status.
# ─────────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """A hermetic two-commit repo. Returns (repo_path, head_before, new_sha):
    `head_before` is the SHA before the second commit ("pre-swarm HEAD"),
    `new_sha` is the second commit ("the cherry-pick that landed this run").
    The second commit's subject contains 'fix(agent): set tool_name'."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first commit")
    head_before = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("2\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "fix(agent): set tool_name on result")
    new_sha = _git(repo, "rev-parse", "HEAD")
    return repo, head_before, new_sha


def test_git_commit_landed_this_run(git_repo):
    repo, head_before, new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": new_sha, "head_before": head_before}
    )
    assert r.verified is True
    assert "landed this run" in r.message


def test_git_commit_noop_already_present_fails(git_repo):
    # The no-op blind spot: claim a SHA that was ALREADY an ancestor of the
    # pre-swarm HEAD. Presence-only would pass; the absent-before check fails it.
    repo, head_before, _new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": head_before, "head_before": head_before}
    )
    assert r.verified is False
    assert "already present before run" in r.message


def test_git_commit_bogus_sha_fails(git_repo):
    repo, head_before, _new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": "deadbeefdeadbeef", "head_before": head_before}
    )
    assert r.verified is False
    assert "unknown commit" in r.message


def test_git_commit_subject_mismatch_fails(git_repo):
    repo, head_before, new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": new_sha, "head_before": head_before,
         "subject": "totally different subject"}
    )
    assert r.verified is False
    assert "subject" in r.message.lower()


def test_git_commit_subject_match_passes(git_repo):
    repo, head_before, new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": new_sha, "head_before": head_before,
         "subject": "set tool_name"}
    )
    assert r.verified is True


def test_git_commit_presence_only_when_no_baseline(git_repo):
    # No head_before supplied → degrade to presence-only, verified True but the
    # message must NOT claim "landed this run" (honest labeling, skeptic #1).
    repo, _head_before, new_sha = git_repo
    r = rv._verify_git_commit({"repo": str(repo), "sha": new_sha})
    assert r.verified is True
    assert "landed this run" not in r.message
    assert "not proven" in r.message.lower()


def test_git_commit_missing_fields_fails():
    r = rv._verify_git_commit({"repo": "", "sha": ""})
    assert r.verified is False
    assert "missing" in r.message.lower()


def test_git_commit_unresolvable_baseline_degrades(git_repo):
    # head_before that doesn't resolve must NOT silently flip to "landed this
    # run" (that would re-open the blind spot via a bad baseline). Degrade to a
    # present-in-HEAD pass with an explicit "baseline unresolvable" note.
    repo, _head_before, new_sha = git_repo
    r = rv._verify_git_commit(
        {"repo": str(repo), "sha": new_sha, "head_before": "00000000nonexistent"}
    )
    assert r.verified is True
    assert "landed this run" not in r.message
    assert "baseline" in r.message.lower()


def _fake_jira_status(monkeypatch, status_name):
    monkeypatch.setattr(rv, "_jira_auth", lambda: ("e@e", "tok"))
    monkeypatch.setattr(
        rv, "_jira_get",
        lambda path, auth, timeout=10.0: {"fields": {"status": {"name": status_name}}},
    )


def test_jira_status_match_passes(monkeypatch):
    _fake_jira_status(monkeypatch, "Done")
    r = rv._verify_jira_status({"issue": "MOL-708", "status": "Done"})
    assert r.verified is True
    assert "Done" in r.message


def test_jira_status_mismatch_fails(monkeypatch):
    _fake_jira_status(monkeypatch, "In Progress")
    r = rv._verify_jira_status({"issue": "MOL-708", "status": "Done"})
    assert r.verified is False
    assert "In Progress" in r.message


def test_jira_status_case_insensitive(monkeypatch):
    _fake_jira_status(monkeypatch, "done")
    r = rv._verify_jira_status({"issue": "MOL-708", "status": "Done"})
    assert r.verified is True


def test_jira_status_no_creds_fails(monkeypatch):
    monkeypatch.setattr(rv, "_jira_auth", lambda: None)
    r = rv._verify_jira_status({"issue": "MOL-708", "status": "Done"})
    assert r.verified is False
    assert "credentials" in r.message.lower()


def test_jira_status_missing_fields_fails():
    r = rv._verify_jira_status({"issue": "", "status": ""})
    assert r.verified is False
    assert "missing" in r.message.lower()


def test_manifest_parses_new_claim_types():
    body = (
        "git_commits:\n"
        "  - repo: ~/.hermes/hermes-agent\n"
        "    sha: a12ea7674\n"
        "    head_before: deadbeef\n"
        "jira_status:\n"
        "  - issue: MOL-708\n"
        '    status: "Done"\n'
    )
    parsed = rv._parse_manifest(body)
    assert len(parsed["git_commits"]) == 1
    assert parsed["git_commits"][0]["sha"] == "a12ea7674"
    assert len(parsed["jira_status"]) == 1
    assert parsed["jira_status"][0]["issue"] == "MOL-708"


def test_verify_and_annotate_dispatches_git_commit(monkeypatch, git_repo):
    # End-to-end: a manifest with a git_commits claim flows through
    # verify_and_annotate → dispatch loop → _verify_git_commit → header.
    repo, head_before, new_sha = git_repo
    monkeypatch.setattr(rv, "_persist_results", lambda *a, **k: None)
    manifest = (
        "<!-- WORK_MANIFEST v1\n"
        "git_commits:\n"
        f"  - repo: {repo}\n"
        f"    sha: {new_sha}\n"
        f"    head_before: {head_before}\n"
        "WORK_MANIFEST -->"
    )
    response = f"Knockout results for MOL-708.\n\n{manifest}"
    job = {"id": "test", "name": "knockout-results", "claims_expected": True}
    out = rv.verify_and_annotate(job, response, "unit_test", 0.0)
    assert "VERIFICATION: 1/1 claims passed" in out
    assert "git_commit" in out

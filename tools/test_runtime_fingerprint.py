"""Tests for runtime_fingerprint.py (P180/MOL-557 shared utility)."""

# P180/MOL-557 test suite.

from __future__ import annotations

import hashlib
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sandboxed_home(monkeypatch, tmp_path):
    """Point HERMES_HOME at a tmp dir and reload the module so paths re-bind."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()
    (tmp_path / "skills").mkdir()
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "tools.runtime_fingerprint" in sys.modules:
        del sys.modules["tools.runtime_fingerprint"]
    import tools.runtime_fingerprint as rf  # noqa: E402

    yield rf, tmp_path
    if "tools.runtime_fingerprint" in sys.modules:
        del sys.modules["tools.runtime_fingerprint"]


def test_hash_matches_shasum(sandboxed_home):
    rf, home = sandboxed_home
    target = home / "config.yaml"
    target.write_bytes(b"key: value\nnested:\n  a: 1\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    sh_out = subprocess.run(
        ["shasum", "-a", "256", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    py_out = rf._hash_one(target)
    assert py_out == expected == sh_out


def test_hash_missing_file_returns_marker(sandboxed_home):
    rf, home = sandboxed_home
    assert rf._hash_one(home / "nope.txt") == "MISSING"


def test_compute_fingerprint_keys_are_absolute(sandboxed_home):
    rf, home = sandboxed_home
    f = home / "a"
    f.write_text("x")
    fp = rf.compute_fingerprint([f, str(f), home / ".." / home.name / "a"])
    assert len(fp) == 1
    (k,) = fp.keys()
    assert k.startswith(str(home))


def test_default_target_set_matches_cc_hook(sandboxed_home):
    rf, home = sandboxed_home
    expected = {
        str(home / "config.yaml"),
        str(home / "cron" / "jobs.json"),
        str(home / "hermes-agent" / "run_agent.py"),
        str(home / "hermes-agent" / "gateway" / "run.py"),
        str(home / "hermes-agent" / "tools" / "environments" / "local.py"),
    }
    fixed = {str(p) for p in rf._FIXED_SURFACES}
    assert fixed == expected


def test_default_surface_includes_all_skill_md(sandboxed_home):
    rf, home = sandboxed_home
    (home / "skills" / "alpha").mkdir()
    (home / "skills" / "alpha" / "SKILL.md").write_text("alpha")
    (home / "skills" / "beta" / "nested").mkdir(parents=True)
    (home / "skills" / "beta" / "nested" / "SKILL.md").write_text("beta")
    (home / "skills" / "alpha" / "README.md").write_text("ignore")
    fp = rf.compute_default_surface_fingerprint()
    skill_keys = [k for k in fp if "/skills/" in k]
    assert len(skill_keys) == 2
    assert all(k.endswith("SKILL.md") for k in skill_keys)


def test_compare_fingerprints_classes(sandboxed_home):
    rf, _ = sandboxed_home
    before = {"a": "h1", "b": "h2", "c": "h3"}
    after = {"a": "h1", "b": "h2-new", "d": "h4"}
    diff = rf.compare_fingerprints(before, after)
    assert diff == {"b": "CHANGED", "c": "DELETED", "d": "ADDED"}


def test_record_then_load_roundtrip(sandboxed_home):
    rf, home = sandboxed_home
    f = home / "x.txt"
    f.write_text("hello")
    hashes = rf.compute_fingerprint([f])
    rf.record_hermes_write([f], hashes)
    assert rf.load_last_hermes_hashes() == hashes


def test_record_merges_does_not_overwrite_other_keys(sandboxed_home):
    rf, home = sandboxed_home
    a = home / "a"
    b = home / "b"
    a.write_text("1")
    b.write_text("2")
    rf.record_hermes_write([a], rf.compute_fingerprint([a]))
    rf.record_hermes_write([b], rf.compute_fingerprint([b]))
    loaded = rf.load_last_hermes_hashes()
    assert str(a.resolve()) in loaded
    assert str(b.resolve()) in loaded


def test_load_corrupt_state_returns_empty(sandboxed_home):
    rf, home = sandboxed_home
    rf.LAST_WRITE_HASHES.write_text("not json {{{")
    assert rf.load_last_hermes_hashes() == {}


def test_load_non_dict_state_returns_empty(sandboxed_home):
    rf, _ = sandboxed_home
    rf.LAST_WRITE_HASHES.write_text("[1, 2, 3]")
    assert rf.load_last_hermes_hashes() == {}


def _concurrent_writer(home_str: str, path_str: str, value: str) -> None:
    os.environ["HERMES_HOME"] = home_str
    sys.path.insert(0, "/Users/wills_mac_mini/.hermes/hermes-agent")
    if "tools.runtime_fingerprint" in sys.modules:
        del sys.modules["tools.runtime_fingerprint"]
    import tools.runtime_fingerprint as rf  # noqa: E402

    p = Path(path_str)
    p.write_text(value)
    rf.record_hermes_write([p], rf.compute_fingerprint([p]))


def test_flock_serializes_concurrent_writers(sandboxed_home):
    rf, home = sandboxed_home
    targets = [home / f"f{i}.txt" for i in range(8)]
    for i, t in enumerate(targets):
        t.write_text(f"init-{i}")
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_concurrent_writer, args=(str(home), str(t), f"v-{i}"))
        for i, t in enumerate(targets)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    assert all(p.exitcode == 0 for p in procs)
    loaded = rf.load_last_hermes_hashes()
    assert sum(1 for k in loaded if "/f" in k and k.endswith(".txt")) == 8


def test_helpers_never_raise(sandboxed_home):
    rf, home = sandboxed_home
    assert rf._hash_one(home / "ghost" / "sub" / "file") == "MISSING"
    assert rf.compute_fingerprint([]) == {}
    assert rf.compare_fingerprints({}, {}) == {}

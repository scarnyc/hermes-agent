"""Tests for MOL-246 evening enrichment section-anchored append.

Targets ``enrichment/append.py`` — byte-faithful bullet injection at h1 anchors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REF_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REF_DIR))

from enrichment.append import append_items, SectionNotFoundError  # noqa: E402
from enrichment.types import SignalItem  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEED = FIXTURES / "evening_seed.md"


def _load_seed() -> str:
    return SEED.read_text(encoding="utf-8")


def test_append_preservation_and_empty_noop():
    src = _load_seed()

    # (a) Empty items is a byte-exact no-op.
    assert append_items(src, "# NOW", []) == src

    # (b) Appending two dummy items preserves marker counts outside the splice
    # and grows the `- [ ]` bullet count by exactly 2.
    items = [
        SignalItem(
            source="calendar",
            section="# NOW",
            body=f"Prep for item {i}",
            source_id=f"cal:{i}",
        )
        for i in (0, 1)
    ]
    output = append_items(src, "# NOW", items)

    assert output.count("**") == src.count("**")
    assert output.count("`") == src.count("`")
    assert output.count("\t") == src.count("\t")
    assert output.count("- [ ]") == src.count("- [ ]") + 2


@pytest.mark.parametrize("section", ["# NOW", "# This Week", "# Later"])
def test_append_each_section_anchor(section):
    src = _load_seed()
    unique_body = f"anchor probe for {section}"
    item = SignalItem(
        source="calendar",
        section=section,
        body=unique_body,
        source_id=f"probe:{section}",
    )
    output = append_items(src, section, [item])

    lines = output.splitlines()
    # Find the new bullet's line index.
    new_bullet = f"- [ ] {unique_body}"
    bullet_idx = None
    for i, ln in enumerate(lines):
        if ln == new_bullet:
            bullet_idx = i
            break
    assert bullet_idx is not None, f"new bullet not found in output for {section}"

    # Find the target heading line index.
    target_heading_idx = None
    for i, ln in enumerate(lines):
        if ln == section:
            target_heading_idx = i
            break
    assert target_heading_idx is not None, f"heading {section} not in output"

    # Find the NEXT heading after target (if any).
    next_heading_idx = None
    for i in range(target_heading_idx + 1, len(lines)):
        if lines[i].startswith("# "):
            next_heading_idx = i
            break

    assert bullet_idx > target_heading_idx, (
        f"bullet at {bullet_idx} must be AFTER heading at {target_heading_idx}"
    )
    if next_heading_idx is not None:
        assert bullet_idx < next_heading_idx, (
            f"bullet at {bullet_idx} must be BEFORE next heading at "
            f"{next_heading_idx} (section={section})"
        )
    # else: EOF section — any index past the heading is fine.


def test_append_eof_section():
    src = _load_seed()
    item = SignalItem(
        source="calendar",
        section="# Later",
        body="EOF append probe",
        source_id="probe:eof",
    )
    output = append_items(src, "# Later", [item])

    # Single-newline tail (no double-newline pile-up).
    assert output.endswith("\n"), "output must end with a newline"
    assert not output.endswith("\n\n\n"), (
        "EOF append should not produce a triple-newline tail"
    )

    # The new bullet must be the LAST `- [ ]` in the file.
    bullets = [ln for ln in output.splitlines() if ln.startswith("- [ ]")]
    assert bullets, "expected at least one - [ ] bullet in output"
    assert bullets[-1] == "- [ ] EOF append probe"


def test_append_missing_section_raises():
    src = _load_seed()
    dummy = SignalItem(
        source="calendar",
        section="# Nonexistent",
        body="should not land",
        source_id="probe:missing",
    )
    with pytest.raises(SectionNotFoundError):
        append_items(src, "# Nonexistent", [dummy])

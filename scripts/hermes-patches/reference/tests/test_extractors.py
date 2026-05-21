"""Tests for enrichment.extractors (MOL-246).

Covers the two verification gates called out in the spec:

1. Calendar timeout degradation — monkeypatched subprocess raises
   TimeoutExpired; extractor returns [] and emits DEGRADED_CALENDAR.
2. Gmail parser against the captured live fixture — verifies the
   header-derived column slicer plus the noise filter.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# Make ``enrichment`` importable when pytest collects this file from the
# repo root (reference/ is not on PYTHONPATH by default).
REFERENCE_DIR = Path(__file__).resolve().parents[1]
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from enrichment.extractors import (  # noqa: E402
    _parse_gmail_triage,
    extract_calendar_items,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_calendar_timeout_degrades(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """subprocess timeout → extractor returns [] + DEGRADED_CALENDAR on stderr."""

    def fake_run(*_args, **_kwargs) -> None:
        raise subprocess.TimeoutExpired(cmd="gws", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    tomorrow = date.today() + timedelta(days=1)
    result = extract_calendar_items(tomorrow)

    assert result == [], f"expected [] on timeout, got {result!r}"

    captured = capsys.readouterr()
    assert "DEGRADED_CALENDAR: timeout" in captured.err, (
        f"expected DEGRADED_CALENDAR: timeout in stderr, got {captured.err!r}"
    )


def test_gmail_parser_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real captured fixture: all 18 rows are noise — result is [].

    Noise breakdown (18 total rows):
      * 12 LinkedIn Job Alerts (jobalerts-noreply@linkedin.com)
      * 1 Particle newsletter (newsletter@email.particle.news)
      * 2 Cohere/Ashby confirmations (no-reply@ashbyhq.com)
      * 1 self-forward (billyscardino@gmail.com)
      * 1 Lenny's Newsletter (@substack.com)
      * 1 LinkedIn messaging digest (messaging-digest-noreply@linke… truncated)

    The parser MUST handle the truncated ``messaging-digest-noreply@linke…``
    display by substring-matching — hence the filter uses a shortened prefix.
    If noise filtering regresses, this test will flag the exact row.
    """
    fixture_path = FIXTURES_DIR / "gmail_triage_sample.txt"
    text = fixture_path.read_text()

    items = _parse_gmail_triage(text)

    # All 18 rows are noise — every item should be filtered.
    assert items == [], (
        f"expected all rows filtered as noise, got {len(items)} items: "
        f"{[i.body for i in items]}"
    )

    # Noise filter should NOT have logged parse failures — every row was
    # a legitimate, parseable row that happened to match a noise pattern.
    captured = capsys.readouterr()
    assert "DEGRADED_GMAIL" not in captured.err, (
        f"noise-matched rows should silently drop, not emit DEGRADED_GMAIL: "
        f"{captured.err!r}"
    )


def test_gmail_parser_emits_non_noise_items() -> None:
    """Synthetic test: a parseable non-noise row produces a well-formed SignalItem.

    Guards against over-aggressive noise filtering: if someone adds a new
    noise pattern that accidentally matches a normal sender, this test
    fails.
    """
    # Build a fake tabular output matching the gws gmail +triage format.
    # Column starts from the real header: date=0, from=39, id=101, subject=119.
    header = (
        "date                                   "  # 0..39
        "from                                                          "  # 39..101
        "id                "  # 101..119
        "subject                                                     "  # 119..
    )
    # Real row: a normal human sender, nothing matching noise patterns.
    row = (
        "Tue, 21 Apr 2026 09:33:01 -0400        "  # date
        "Alice Example <alice@example.com>                             "  # from
        "19db03eb974bc24c  "  # id
        "Quarterly review notes                                      "  # subject
    )
    text = f"{header}\n{row}\n"

    items = _parse_gmail_triage(text)
    assert len(items) == 1
    item = items[0]
    assert item.source == "gmail"
    assert item.section == "# This Week"
    assert item.source_id == "19db03eb974bc24c"
    assert len(item.source_id) == 16
    assert item.body.startswith("Reply:")
    assert "Quarterly review notes" in item.body
    assert "Alice Example" in item.body

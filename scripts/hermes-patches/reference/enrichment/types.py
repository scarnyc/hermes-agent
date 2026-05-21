"""Shared dataclass for MOL-246 evening enrichment.

Kept in its own module so extractors, dedup, and append can import it without
pulling each other's implementation surface.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignalItem:
    """One action-item candidate produced by an extractor.

    Attributes:
        source: short tag — ``"calendar"`` | ``"gmail"`` | ``"granola"``.
        section: target heading — ``"# NOW"`` | ``"# This Week"``. The
            orchestrator groups items by this value before calling
            ``append_items`` once per distinct heading.
        body: bullet text without the leading ``"- [ ] "`` marker. The
            append helper renders the list-item prefix itself.
        source_id: deterministic identity for dedup (Gmail thread id,
            Calendar event id, or ``sha1("YYYY-MM-DD:body")[:12]`` for
            Granola items whose meeting-id lives inside prose).
    """

    source: str
    section: str
    body: str
    source_id: str

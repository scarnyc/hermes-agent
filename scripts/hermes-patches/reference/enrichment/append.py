"""Section-anchored bullet append helper for MOL-246 evening enrichment.

Byte-faithful append: tokenize with markdown-it-py, find the named h1 section,
splice new `- [ ]` bullets at the last non-blank line of the section, return the
rebuilt text. Bytes outside the splice point are unchanged.

Key invariant: `text.splitlines(keepends=True)` preserves every original line
ending. We only insert new lines; never rewrite existing ones.
"""
from __future__ import annotations

from markdown_it import MarkdownIt

from .types import SignalItem


class SectionNotFoundError(KeyError):
    """Raised by ``_locate_section`` when the named heading isn't in the text."""


def _assert_heading_map_semantics() -> None:
    """Defense-in-depth probe. Fail fast on markdown-it-py .map regression.

    Parses a known input and asserts the .map ranges + inline-token placement
    line up with what ``_locate_section`` relies on. If a future upgrade of
    markdown-it-py shifts heading .map to be multi-line, or moves body-text
    inline tokens into the heading's .map range, this probe fires with a
    pin-or-update hint instead of silently producing wrong splice indices.
    """
    probe = "# A\nbody\n# B\n"
    tokens = MarkdownIt("commonmark").parse(probe)
    headings = [
        (i, t) for i, t in enumerate(tokens) if t.type == "heading_open"
    ]
    if len(headings) != 2:
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            f"expected 2 heading_open tokens, found {len(headings)}; "
            "pin markdown-it-py<5 or update _locate_section"
        )
    first_map = headings[0][1].map
    second_map = headings[1][1].map
    if first_map != [0, 1] and first_map != (0, 1):
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            f"first heading map={first_map}, expected (0, 1); "
            "pin markdown-it-py<5 or update _locate_section"
        )
    if second_map != [2, 3] and second_map != (2, 3):
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            f"second heading map={second_map}, expected (2, 3); "
            "pin markdown-it-py<5 or update _locate_section"
        )
    # Body-text inline at line 1 must NOT fall within either heading's map.
    body_inlines = [
        t for t in tokens
        if t.type == "inline" and t.content.strip() == "body"
    ]
    if not body_inlines:
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            "body paragraph inline token not found; "
            "pin markdown-it-py<5 or update _locate_section"
        )
    body_map = body_inlines[0].map
    body_start = body_map[0] if body_map else None
    if body_start is None:
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            "body inline token has no .map; "
            "pin markdown-it-py<5 or update _locate_section"
        )

    def _in_range(idx: int, rng) -> bool:
        return rng is not None and rng[0] <= idx < rng[1]

    if _in_range(body_start, first_map) or _in_range(body_start, second_map):
        raise RuntimeError(
            "markdown-it-py heading .map semantics regression: "
            f"body inline at line {body_start} falls inside a heading's map; "
            "pin markdown-it-py<5 or update _locate_section"
        )


def _locate_section(tokens, section_header: str) -> tuple[int, int, int]:
    """Return ``(h_start, h_end, next_h_start)`` line indices for the section.

    Matches ``heading_open`` tokens whose level equals
    ``section_header`` heading level (hash count) AND whose immediately-following
    inline content, stripped, equals the header's stripped text.

    ``next_h_start`` is the line index of the next heading at the same or
    shallower level (h1 ≤ h1 for our "# NOW" case). If none exists, returns
    ``len(lines_equivalent)`` — which we compute as the maximum map[1] across
    all tokens, effectively end-of-document.

    Raises ``SectionNotFoundError`` if the heading is absent.
    """
    # Parse header: count leading '#' runs, strip the rest.
    stripped_header = section_header.lstrip()
    hash_run = 0
    for ch in stripped_header:
        if ch == "#":
            hash_run += 1
        else:
            break
    want_tag = f"h{hash_run}" if hash_run else "h1"
    want_text = stripped_header.lstrip("#").strip()

    # Compute document end: max of map[1] for tokens that have a .map.
    doc_end = 0
    for t in tokens:
        if t.map is not None and t.map[1] > doc_end:
            doc_end = t.map[1]

    # Find the matching heading.
    target_idx = None
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open" or tok.tag != want_tag or tok.map is None:
            continue
        # Inline content sits right after heading_open.
        inline_tok = None
        for follower in tokens[i + 1:]:
            if follower.type == "heading_close":
                break
            if follower.type == "inline":
                inline_tok = follower
                break
        if inline_tok is None:
            continue
        if inline_tok.content.strip() == want_text:
            target_idx = i
            break

    if target_idx is None:
        raise SectionNotFoundError(section_header)

    target_tok = tokens[target_idx]
    h_start, h_end = target_tok.map[0], target_tok.map[1]
    target_level_num = int(target_tok.tag.lstrip("h"))

    # Find next heading of same-or-shallower level (smaller h-number = shallower).
    next_h_start = doc_end
    for tok in tokens[target_idx + 1:]:
        if tok.type != "heading_open" or tok.map is None:
            continue
        other_level = int(tok.tag.lstrip("h")) if tok.tag.startswith("h") else 999
        if other_level <= target_level_num:
            next_h_start = tok.map[0]
            break

    return h_start, h_end, next_h_start


def _last_content_line_in(lines: list[str], start: int, end: int) -> int:
    """Return index of the last non-blank line in ``[start, end)``.

    Returns ``start - 1`` if the slice is empty or all blank — signalling
    "insert at ``start``" to the caller (``insert_idx = returned + 1``).
    """
    if start >= end:
        return start - 1
    # Walk backwards.
    for idx in range(end - 1, start - 1, -1):
        if idx >= len(lines):
            continue
        if lines[idx].strip() != "":
            return idx
    return start - 1


def _detect_eol(lines: list[str], idx: int) -> str:
    """Return the line ending used by ``lines[idx]``.

    Falls back to ``'\\n'`` on out-of-range indices or a line without a trailing
    newline (e.g., EOF line with no final \\n).
    """
    if idx < 0 or idx >= len(lines):
        return "\n"
    line = lines[idx]
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return "\n"


def append_items(text: str, section_header: str, items: list[SignalItem]) -> str:
    """Append bullets at the end of the named section, byte-faithful outside the splice.

    Uses ``text.splitlines(keepends=True)`` to preserve every original line
    ending, finds the target section via ``_locate_section``, inserts
    ``f"- [ ] {it.body}{eol}"`` after the last non-blank line within the
    section, and rejoins.

    Returns ``text`` unchanged if ``items`` is empty (no-op — covers both
    "no signals" and "section exists but nothing to append" paths).

    Raises ``SectionNotFoundError`` if ``section_header`` isn't in the text.
    """
    _assert_heading_map_semantics()

    if not items:
        return text

    tokens = MarkdownIt("commonmark").parse(text)
    h_start, h_end, next_h_start = _locate_section(tokens, section_header)

    lines = text.splitlines(keepends=True)
    last_content_idx = _last_content_line_in(lines, h_end, next_h_start)
    insert_idx = last_content_idx + 1

    eol_src_idx = last_content_idx if last_content_idx >= h_end else h_start
    eol = _detect_eol(lines, eol_src_idx)

    new_bullets = [f"- [ ] {it.body}{eol}" for it in items]

    rebuilt = lines[:insert_idx] + new_bullets + lines[insert_idx:]
    return "".join(rebuilt)

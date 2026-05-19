"""P146/MOL-496 — Hermes skill_load tool for dynamic SKILL.md loading.

Lets the Hermes LLM load full SKILL.md content by name at runtime,
rather than relying only on pre-loaded system prompt summaries.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILLS_ROOT = Path("~/.hermes/skills").expanduser()


def skill_load(skill_name: str) -> str:
    """Load a SKILL.md by name and return its full markdown content.

    Searches ~/.hermes/skills/**/SKILL.md for matching 'name:' frontmatter.
    Returns the skill content on match, or an error message if not found.

    Examples:
        skill_load("plan-skeptic")       → plan-skeptic SKILL.md content
        skill_load("context-compression") → context-engineering skill
    """
    if not _SKILLS_ROOT.is_dir():
        return (
            f"Error: skills directory not found at {_SKILLS_ROOT}. "
            "Run 'hermes skills list' to verify skill installation."
        )
    matches = []
    for skill_file in sorted(_SKILLS_ROOT.rglob("SKILL.md")):
        try:
            content = skill_file.read_text()
        except OSError as exc:
            logger.warning("skill_load: cannot read %s: %s", skill_file, exc)
            continue
        # Match YAML frontmatter 'name:' field anywhere in the frontmatter block
        # (between first --- and second ---).  Handles 'name:' on any line,
        # CRLF line endings, and extra whitespace around the field value.
        fm_match = re.search(
            r"^---\s*\n(.*?)^---\s*\n",
            content,
            re.MULTILINE | re.DOTALL,
        )
        if not fm_match:
            continue
        fm_block = fm_match.group(1)
        if re.search(
            rf"^name:\s*{re.escape(skill_name)}\s*$",
            fm_block,
            re.MULTILINE,
        ):
            matches.append((skill_file, content))
    if not matches:
        return (
            f"Skill '{skill_name}' not found. "
            f"Available skills can be listed with `hermes skills list` "
            f"or by browsing {_SKILLS_ROOT}."
        )
    if len(matches) > 1:
        paths = ", ".join(str(m[0].relative_to(_SKILLS_ROOT)) for m in matches)
        return (
            f"Multiple skills match '{skill_name}': {paths}. "
            "Use the full path to disambiguate."
        )
    return matches[0][1]

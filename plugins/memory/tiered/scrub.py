"""Credential and PII scrubbing for tiered memory.

Reuses Hermes agent.redact for credential patterns (50+).
Ports 6 PII patterns from moltworker-poc.
Fail-closed: refuses to store unscrubbed data in production.
"""

import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Import Hermes credential scrubbing (50+ patterns)
try:
    from agent.redact import redact_sensitive_text
    _HAS_REDACT = True
except ImportError:
    _HAS_REDACT = False
    logger.warning("agent.redact not available — credential scrubbing DISABLED (dev-only)")
    def redact_sensitive_text(text: str) -> str:  # type: ignore[misc]
        return text

# PII patterns ported from moltworker-poc/src/security/pii-filter.ts
# Order matters: IBAN must precede CC to avoid partial CC match inside IBAN.
PII_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN]'),
    (re.compile(r'\b\d{3} \d{2} \d{4}\b'), '[SSN]'),
    (re.compile(r'(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b'), '[PHONE]'),
    (re.compile(r'\b[A-Z]{2}\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{0,2}\b'), '[IBAN]'),
    (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[CC]'),
    (re.compile(r'(?:born|dob|birthday|date of birth|birthdate|d\.o\.b)[:\s]+(?:on\s+)?\d{4}[-/]\d{2}[-/]\d{2}\b', re.IGNORECASE), '[DOB]'),
]


def scrub_content(text: str, *, allow_no_redact: bool = False) -> str:
    """Scrub credentials + PII. Defense-in-depth before any persistence.

    Raises RuntimeError in production if agent.redact is unavailable,
    unless allow_no_redact=True (tests only).
    """
    if not _HAS_REDACT and not allow_no_redact:
        raise RuntimeError(
            "Cannot scrub content: agent.redact unavailable — refusing to store unscrubbed data"
        )
    text = redact_sensitive_text(text)
    for pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

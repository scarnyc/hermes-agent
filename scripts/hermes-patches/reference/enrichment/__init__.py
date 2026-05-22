"""Evening enrichment package (MOL-246).

Peer of MOL-239's morning preserve-then-mutate composer. Injects prep bullets
from Calendar / Gmail / Granola into today's task-list file at section-header
anchors. Morning job carries them forward via existing byte-faithful drop pass.
"""
from .types import SignalItem

__all__ = ["SignalItem"]

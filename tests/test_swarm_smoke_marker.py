"""Tests for swarm_smoke_marker — MOL-2067 smoke test."""

import importlib.util
import sys
from pathlib import Path


def _load_swarm_smoke_marker():
    """Load the swarm_smoke_marker script as a module for testing."""
    path = Path(__file__).parent.parent / "scripts" / "swarm_smoke_marker.py"
    spec = importlib.util.spec_from_file_location("swarm_smoke_marker", path)
    assert spec is not None, f"Could not find {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["swarm_smoke_marker"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ssm = _load_swarm_smoke_marker()


def test_swarm_smoke_returns_canonical_string():
    """Verify swarm_smoke() returns the exact literal smoke OK string."""
    assert _ssm.swarm_smoke() == "MOL-2052 swarm smoke OK"

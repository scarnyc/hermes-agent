"""P71/MOL-250 — cron_health.py elides Launchd Agents section on error.

P67 softened the error vocabulary but retained trigger words ("exited 1",
"sandbox restriction") that the briefing LLM kept misclassifying as
INFRA:DEGRADED. P71 removes the section entirely on error so there is no
text for the LLM to latch onto. These tests lock that contract in place.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class TestCronHealthLaunchdElision(unittest.TestCase):
    """main() must omit the `## Launchd Agents` section on error status
    and emit it normally on ok/empty.
    """

    def setUp(self):
        # Minimal jobs.json fixture — main() requires it exists.
        self._tmpdir = tempfile.mkdtemp(prefix="p71-cron-health-")
        self._jobs_path = os.path.join(self._tmpdir, "jobs.json")
        with open(self._jobs_path, "w") as f:
            json.dump({"jobs": [
                {"enabled": True, "name": "Test Job",
                 "last_status": "ok",
                 "last_run_at": "2026-04-24T04:00:00",
                 "next_run_at": "2026-04-25T04:00:00"},
            ]}, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run_main(self):
        import cron_health
        with patch.object(cron_health, "JOBS_PATH", self._jobs_path):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cron_health.main()
            return buf.getvalue()

    def test_error_status_elides_launchd_section_entirely(self):
        """When get_launchd_status() returns 'error', main() must emit
        zero mentions of 'Launchd Agents' and zero trigger words
        ('launchctl', 'sandbox restriction', 'not queryable')."""
        import cron_health
        with patch.object(
            cron_health, "get_launchd_status",
            return_value=("error", "launchctl exited 1 (likely sandbox restriction)"),
        ):
            out = self._run_main()
        self.assertNotIn("## Launchd Agents", out)
        self.assertNotIn("launchctl", out)
        self.assertNotIn("sandbox restriction", out)
        self.assertNotIn("not queryable", out)
        # Jobs table must still be present — load-bearing health signal.
        self.assertIn("## Cron Jobs (jobs.json)", out)
        self.assertIn("Test Job", out)

    def test_ok_status_emits_launchd_section(self):
        """Positive control — ok status must still produce the section."""
        import cron_health
        with patch.object(
            cron_health, "get_launchd_status",
            return_value=("ok", ["123\t0\tai.hermes.gateway",
                                 "-\t1\tai.hermes.canary"]),
        ):
            out = self._run_main()
        self.assertIn("## Launchd Agents", out)
        self.assertIn("ai.hermes.gateway", out)

    def test_empty_status_emits_launchd_section_with_note(self):
        """Empty status — launchctl ran fine but no ai.hermes agents found.
        Section header still prints; branch exercises the no-entries path."""
        import cron_health
        with patch.object(
            cron_health, "get_launchd_status",
            return_value=("empty", []),
        ):
            out = self._run_main()
        self.assertIn("## Launchd Agents", out)
        self.assertIn("No ai.hermes launchd agents registered.", out)

    def test_unknown_status_raises_contract_guard(self):
        """Contract guard — a hypothetical 4th status MUST raise so it
        doesn't silently reuse the sandbox-expected elision."""
        import cron_health
        with patch.object(
            cron_health, "get_launchd_status",
            return_value=("mystery_status", "new failure mode"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._run_main()
            self.assertIn("unexpected launchd status", str(ctx.exception))
            self.assertIn("mystery_status", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

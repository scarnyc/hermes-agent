#!/usr/bin/env python3
"""
MOL-568: Tmux-based Claude Code fallback runner.

When claude -p hits Anthropic rate limits, interactive claude inside tmux
may retry rate limits internally where -p mode exits immediately.

Provides:
  - check_tmux()              — verify tmux binary is on PATH
  - create_session(name, ...) — tmux new-session -d -s <name>
  - inject_prompt(ses, text)  — tmux send-keys and press Enter
  - wait_for_completion(ses)  — poll capture-pane until stable
  - capture_output(ses)       — tmux capture-pane -p
  - kill_session(ses)         — tmux kill-session
  - run_claude_tmux(...)      — high-level orchestrator
"""

import logging
import shutil
import subprocess
import time
import uuid

logger = logging.getLogger(__name__)


# ── Prerequisite check ────────────────────────────────────────────────────

def check_tmux() -> bool:
    """Return True if the tmux binary is available on PATH."""
    return shutil.which("tmux") is not None


# ── Session management ────────────────────────────────────────────────────

def create_session(name: str, workdir: str | None = None) -> str:
    """Create a detached tmux session.

    Args:
        name: Unique session name (e.g. hermes-fb-<uuid4>)
        workdir: Optional starting working directory

    Returns:
        Session name on success. Raises RuntimeError on failure.
    """
    argv = ["tmux", "new-session", "-d", "-s", name]
    if workdir:
        argv.extend(["-c", workdir])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux new-session -s {name} failed: {result.stderr.strip()}"
        )
    logger.debug("tmux session created: %s (workdir=%s)", name, workdir)
    return name


def inject_prompt(session: str, prompt: str) -> None:
    """Send text to a tmux session and press Enter.

    Uses tmux send-keys with the literal prompt argument (no shell escaping
    needed — tmux handles special characters through the argv interface).
    """
    result = subprocess.run(
        ["tmux", "send-keys", "-t", session, prompt, "Enter"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux send-keys -t {session} failed: {result.stderr.strip()}"
        )


def capture_output(session: str) -> str:
    """Capture visible pane content from a tmux session via capture-pane -p."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux capture-pane -t {session} failed: {result.stderr.strip()}"
        )
    return result.stdout


def wait_for_completion(
    session: str,
    idle_timeout: int = 5,
    max_wait_seconds: int = 3600,
) -> str:
    """Poll capture-pane every second until output is stable.

    Returns the final captured output when the pane content hasn't changed
    for ``idle_timeout`` consecutive seconds.  Caps total wait at
    ``max_wait_seconds``.

    Args:
        session: Tmux session name.
        idle_timeout: Seconds of unchanged output to consider "done".
        max_wait_seconds: Hard cap on total polling time.

    Returns:
        Final captured pane text (partial if max_wait exceeded).
    """
    prev = ""
    stable_for = 0
    elapsed = 0
    while elapsed < max_wait_seconds:
        time.sleep(1)
        elapsed += 1
        try:
            current = capture_output(session)
        except RuntimeError:
            # Session may have exited — return whatever we captured last
            return prev if prev else ""
        if current == prev:
            stable_for += 1
            if stable_for >= idle_timeout:
                return current
        else:
            stable_for = 0
            prev = current
    logger.warning(
        "wait_for_completion: max_wait_seconds=%d exceeded for session %s",
        max_wait_seconds, session,
    )
    return prev if prev else ""


def kill_session(session: str) -> None:
    """Kill a tmux session. No-op if session doesn't exist."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True, text=True, timeout=15,
    )
    # Don't raise — session may already be dead


# ── TmuxRunner class ──────────────────────────────────────────────────────

class TmuxRunner:
    """Manages a single named tmux session lifecycle.

    Usage:
        runner = TmuxRunner("hermes-fallback-abc123")
        runner.create(workdir="/path/to/repo")
        runner.inject("claude")
        output = runner.wait_for_completion()
        runner.kill()

    Or use the context manager form:
        with TmuxRunner("hermes-fallback-abc123") as r:
            r.create()
            r.inject("echo hello")
            output = r.wait_for_completion()
    """

    def __init__(self, session_name: str | None = None) -> None:
        self.session = session_name or f"hermes-fb-{uuid.uuid4().hex[:12]}"

    # ── Prerequisite ──────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """Return True if the tmux binary is on PATH."""
        return check_tmux()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def create(self, workdir: str | None = None) -> "TmuxRunner":
        """Create the detached tmux session. Returns self for chaining."""
        create_session(self.session, workdir=workdir)
        return self

    def inject(self, text: str) -> None:
        """Send text + Enter to the session pane."""
        inject_prompt(self.session, text)

    def capture(self) -> str:
        """Return current visible pane content."""
        return capture_output(self.session)

    def wait_for_completion(
        self,
        idle_timeout: int = 5,
        max_wait_seconds: int = 3600,
    ) -> str:
        """Poll until output is stable; return final pane content."""
        return wait_for_completion(
            self.session,
            idle_timeout=idle_timeout,
            max_wait_seconds=max_wait_seconds,
        )

    def kill(self) -> None:
        """Kill the session (no-op if already dead)."""
        kill_session(self.session)

    # ── Context manager ───────────────────────────────────────────────────

    def __enter__(self) -> "TmuxRunner":
        return self

    def __exit__(self, *_) -> None:
        self.kill()


# ── High-level runner ─────────────────────────────────────────────────────

def run_claude_tmux(
    repo_path: str,
    goal: str,
    context: str = "",
    timeout_seconds: int = 1800,
) -> str:
    """Run a Claude Code task inside a tmux session as rate-limit fallback.

    1. Creates a detached tmux session named hermes-fb-<uuid4>
    2. Navigates to repo_path, launches interactive claude
    3. Injects the task prompt via send-keys
    4. Waits for completion via idle-timeout detection (5 s stable pane)
    5. Captures output, cleans up the session

    Args:
        repo_path: Absolute path to the git working directory.
        goal: Task description / goal.
        context: Optional context text (appended after goal).
        timeout_seconds: Max wall-clock time for the entire run.

    Returns:
        Captured pane output string.  May be empty or partial on timeout.
    """
    if not check_tmux():
        raise RuntimeError("tmux not available on PATH")

    session_name = f"hermes-fb-{uuid.uuid4().hex[:12]}"
    prompt_text = f"{goal}\n\nContext: {context}" if context else goal

    logger.info(
        "run_claude_tmux: session=%s repo=%s timeout=%ds goal_preview=%s",
        session_name, repo_path, timeout_seconds, goal[:120],
    )

    try:
        create_session(session_name, workdir=repo_path)

        # Give the shell a moment to initialize
        time.sleep(0.5)

        # Launch interactive claude (no -p — interactive mode has built-in
        # rate-limit retry that batch mode lacks).
        inject_prompt(session_name, "claude")

        # Wait for claude to reach its prompt
        time.sleep(4)

        # Inject the task as a single message
        inject_prompt(session_name, prompt_text)

        # Wait for the subagent to finish (idle detection)
        output = wait_for_completion(
            session_name,
            idle_timeout=5,
            max_wait_seconds=timeout_seconds,
        )

        logger.info(
            "run_claude_tmux: session=%s done, captured %d chars",
            session_name, len(output),
        )
        return output

    except RuntimeError:
        # Re-raise runtime errors (tmux failures, session issues)
        raise
    except Exception as exc:
        logger.error(
            "run_claude_tmux: unexpected error in session %s: %s",
            session_name, exc,
        )
        raise
    finally:
        kill_session(session_name)

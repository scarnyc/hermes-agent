"""Runtime fingerprint helper for Hermes-side pollution guards (P180/MOL-557).

Mirrors the target set in `session-start-fingerprint.sh` (`TARGETS` array) —
sha256 over 5 fixed runtime files plus every SKILL.md under ~/.hermes/skills/.
Used by H1 (pre-write guard), H2 (gateway snapshot), H3 (verifier bracket),
H4 (process counts), H5 (cron CRUD), H6 (symphony bracket).

Fail-open contract: helpers MUST NOT raise. Hash errors return "MISSING" or
"ERROR:<msg>"; flock failures fall back to non-locked write with audit entry.
"""

# P180/MOL-557 — Hermes-side runtime pollution guard. Pair: ~/.claude/hooks/session-start-fingerprint.sh.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
LOG_DIR = HERMES_HOME / "logs"
LAST_WRITE_HASHES = STATE_DIR / "hermes-last-write-hashes.json"
FINGERPRINT_LOCK = STATE_DIR / "hermes-fingerprint.lock"
TELEGRAM_ALERT_STATE = STATE_DIR / "hermes-telegram-alert-last-ts.json"
TELEGRAM_ALERT_LOCK = STATE_DIR / "hermes-telegram-alert.lock"
WRITE_COLLISION_LOG = LOG_DIR / "hermes-write-collision.jsonl"

_CALLER_MAX_LEN = 200

HashStatus = Literal["MISSING"]
DiffKind = Literal["CHANGED", "DELETED", "ADDED"]
H1Outcome = Literal["proceed", "overwrite", "abort"]
TelegramStatus = Literal["sent", "throttled"]

# Must stay byte-identical to the `TARGETS` array in session-start-fingerprint.sh.
_FIXED_SURFACES: tuple[Path, ...] = (
    HERMES_HOME / "config.yaml",
    HERMES_HOME / "cron" / "jobs.json",
    HERMES_HOME / "hermes-agent" / "run_agent.py",
    HERMES_HOME / "hermes-agent" / "gateway" / "run.py",
    HERMES_HOME / "hermes-agent" / "tools" / "environments" / "local.py",
)
_SKILLS_ROOT = HERMES_HOME / "skills"

_CHUNK = 1 << 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_one(path: Path) -> str:
    """sha256 of file bytes; byte-identical to `shasum -a 256`. Never raises."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                buf = f.read(_CHUNK)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except FileNotFoundError:
        return "MISSING"
    except OSError as e:
        return f"ERROR:{e.__class__.__name__}"


def compute_fingerprint(paths: Iterable[Path | str]) -> dict[str, str]:
    """Hash each path; key is absolute string path. Missing → 'MISSING'."""
    out: dict[str, str] = {}
    for p in paths:
        ap = str(Path(p).expanduser().resolve(strict=False))
        out[ap] = _hash_one(Path(ap))
    return out


def _default_target_paths() -> list[Path]:
    paths: list[Path] = list(_FIXED_SURFACES)
    try:
        paths.extend(sorted(_SKILLS_ROOT.rglob("SKILL.md")))
    except OSError:
        pass
    return paths


def compute_default_surface_fingerprint() -> dict[str, str]:
    """5 fixed Hermes runtime files + every SKILL.md under ~/.hermes/skills/."""
    return compute_fingerprint(_default_target_paths())


def compare_fingerprints(
    before: dict[str, str], after: dict[str, str]
) -> dict[str, DiffKind]:
    """{path: 'CHANGED'|'DELETED'|'ADDED'} for paths that differ. Stable order."""
    diff: dict[str, DiffKind] = {}
    for path in sorted(set(before) | set(after)):
        b = before.get(path)
        a = after.get(path)
        if b is None and a is not None:
            diff[path] = "ADDED"
        elif b is not None and a is None:
            diff[path] = "DELETED"
        elif b != a:
            diff[path] = "CHANGED"
    return diff


def _ensure_state_dir() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


class _FileLock:
    """fcntl.LOCK_EX wrapper. Never raises — bool .acquired reports state."""

    def __init__(self, path: Path, timeout: float = 2.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None
        self.acquired = False

    def __enter__(self) -> "_FileLock":
        _ensure_state_dir()
        deadline = time.monotonic() + self.timeout
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return self
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                if self.acquired:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass


def load_last_hermes_hashes() -> dict[str, str]:
    """Read recorded post-write hashes. Missing/corrupt → empty dict."""
    try:
        with open(LAST_WRITE_HASHES, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def record_hermes_write(
    paths: Iterable[Path | str], hashes_after: dict[str, str]
) -> None:
    """Merge per-path post-write hashes into state file under flock. Fail-open."""
    keys = [str(Path(p).expanduser().resolve(strict=False)) for p in paths]
    with _FileLock(FINGERPRINT_LOCK):
        current = load_last_hermes_hashes()
        for k in keys:
            v = hashes_after.get(k)
            if isinstance(v, str):
                current[k] = v
        try:
            tmp = LAST_WRITE_HASHES.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(current, f, sort_keys=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, LAST_WRITE_HASHES)
        except OSError:
            pass


def now_iso() -> str:
    """Exposed UTC timestamp helper for callers that emit audit JSONL alongside."""
    return _now_iso()


# ----- Shared audit + alert primitives (used by H1, H2, H3, H5, H6) ---------


def emit_audit_jsonl(log_path: Path, payload: dict) -> None:
    """Append a JSON line to an audit log. Lazy-creates parent dir. Fail-open."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _read_alert_state() -> dict[str, float]:
    try:
        with open(TELEGRAM_ALERT_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return {}


def _write_alert_state(state: dict[str, float]) -> None:
    try:
        tmp = TELEGRAM_ALERT_STATE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TELEGRAM_ALERT_STATE)
    except OSError:
        pass


def telegram_alert_rate_limited(
    event_key: str, message: str, throttle_seconds: int = 3600
) -> str:
    """Send a Telegram alert, throttled per event_key. Returns status string.

    'sent' | 'throttled' | 'skipped:<reason>' | 'error:<msg>'. Never raises —
    a broken alerter must not block the dispatch path that called us.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return "skipped:no_credentials"

    now = time.time()
    with _FileLock(TELEGRAM_ALERT_LOCK):
        state = _read_alert_state()
        last = state.get(event_key, 0.0)
        if (now - last) < throttle_seconds:
            return "throttled"
        state[event_key] = now
        _write_alert_state(state)

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": message[:4000]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 — fixed https Telegram API URL, not user input
            if 200 <= resp.status < 300:
                return "sent"
            return f"error:http_{resp.status}"
    except urllib.error.URLError as e:
        return f"error:{e.__class__.__name__}"
    except OSError as e:
        return f"error:{e.__class__.__name__}"


# ----- H1: pre-write external-mutation guard -------------------------------


def _is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (OSError, ValueError):
        return False


def _trim_caller(caller: str) -> str:
    s = str(caller)
    if len(s) > _CALLER_MAX_LEN:
        return s[:_CALLER_MAX_LEN] + "…"
    return s


def h1_pre_write_guard(path: Path | str, caller: str) -> H1Outcome:
    """Detect external mutation before a Hermes write.

    Returns one of:
      'proceed'   — clean (matches last recorded hash, OR first write, OR
                    fail-open after a hash/state-read error).
      'overwrite' — mutation detected, TTY user chose to overwrite anyway.
      'abort'     — mutation detected, abort the write. Audit + Telegram emitted.

    Callers MUST NOT raise based on the result — translate 'abort' to whatever
    "skip this write" semantics fit (return early, set a sentinel, etc).

    `caller` is required so audit attribution lands on the call site; pass the
    identity (e.g. "save_config", "save_jobs:job_id=abc"). Empty values fall
    back to a static sentinel — no stack walk.
    """
    p = Path(str(path)).expanduser().resolve(strict=False)
    key = str(p)
    caller_label = _trim_caller(caller) if caller else "<missing_caller>"

    last = load_last_hermes_hashes().get(key)
    current = _hash_one(p)

    if current.startswith("MISSING"):
        # New file — nothing to compare against. Proceed; post-write records hash.
        return "proceed"
    if current.startswith("ERROR:"):
        emit_audit_jsonl(
            WRITE_COLLISION_LOG,
            {
                "ts": _now_iso(),
                "event": "hash_error_fail_open",
                "path": key,
                "error": current,
                "caller": caller_label,
            },
        )
        return "proceed"
    if last is None:
        # First Hermes write since the guard landed — warm-start tolerance.
        return "proceed"
    if current == last:
        return "proceed"

    # External mutation detected.
    # P181 — stale-baseline auto-recovery: if the file hasn't changed since
    # session-start (fingerprinter agrees with disk), the baseline is stale,
    # not the file. Auto-heal by updating the baseline and proceeding.
    session_id = os.environ.get("HERMES_SESSION_ID")
    if session_id:
        fp_path = STATE_DIR / "session-fingerprints" / f"{session_id}.json"
        try:
            with open(fp_path, "r", encoding="utf-8") as _fp:
                sf = json.load(_fp)
            sf_hashes = sf.get("hashes", {}) if isinstance(sf, dict) else {}
            session_hash = sf_hashes.get(key)
            if session_hash is not None and session_hash == current:
                # File was stable at session start — baseline is stale.
                emit_audit_jsonl(
                    WRITE_COLLISION_LOG,
                    {
                        "ts": _now_iso(),
                        "event": "stale_baseline_auto_healed",
                        "path": key,
                        "old_baseline": last,
                        "new_baseline": current,
                        "session_id": session_id,
                        "caller": caller_label,
                    },
                )
                h1_record_post_write(p)
                return "proceed"
        except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError):
            # Fingerprint unavailable — genuine mutation possible, fall through.
            pass

    if _is_interactive():
        try:
            sys.stderr.write(
                f"\n[H1/MOL-557] External mutation detected on {key}\n"
                f"  recorded hash: {last[:12]}…\n"
                f"  on-disk hash:  {current[:12]}…\n"
                f"  caller:        {caller_label}\n"
                f"Action? [o]verwrite / [a]bort (default) / [d]iff: "
            )
            sys.stderr.flush()
            choice = sys.stdin.readline().strip().lower()
        except (OSError, EOFError):
            choice = "a"
        if choice == "o":
            emit_audit_jsonl(
                WRITE_COLLISION_LOG,
                {
                    "ts": _now_iso(),
                    "event": "external_mutation_detected",
                    "path": key,
                    "last_hermes_hash": last,
                    "current_hash": current,
                    "action": "overwrite",
                    "caller": caller_label,
                    "tty": True,
                },
            )
            return "overwrite"
        if choice == "d":
            try:
                sys.stderr.write(
                    "\n(can't reconstruct prior bytes from a hash; showing current file)\n"
                )
                r = subprocess.run(
                    ["head", "-50", str(p)],
                    check=False, capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    sys.stderr.write(r.stdout)
                else:
                    sys.stderr.write(
                        f"(head failed: rc={r.returncode}; stderr={r.stderr[:200]})\n"
                    )
            except (OSError, ValueError, subprocess.TimeoutExpired):
                sys.stderr.write("(diff helper unavailable)\n")
        # Default / 'a' / 'd' both abort.
        emit_audit_jsonl(
            WRITE_COLLISION_LOG,
            {
                "ts": _now_iso(),
                "event": "external_mutation_detected",
                "path": key,
                "last_hermes_hash": last,
                "current_hash": current,
                "action": "abort",
                "caller": caller_label,
                "tty": True,
            },
        )
        return "abort"

    # Non-TTY: abort, audit, alert.
    emit_audit_jsonl(
        WRITE_COLLISION_LOG,
        {
            "ts": _now_iso(),
            "event": "external_mutation_detected",
            "path": key,
            "last_hermes_hash": last,
            "current_hash": current,
            "action": "abort",
            "caller": caller_label,
            "tty": False,
        },
    )
    telegram_alert_rate_limited(
        event_key=f"h1_external_mutation:{key}",
        message=(
            f"⚠️ [H1/MOL-557] External mutation on {p.name}\n"
            f"Path: {key}\n"
            f"Caller: {caller_label}\n"
            f"Last hash: {last[:12]}…\n"
            f"On disk: {current[:12]}…\n"
            f"Aborted Hermes write."
        ),
    )
    return "abort"


def h1_record_post_write(path: Path | str) -> None:
    """Recompute hash after a successful write and store as the new baseline."""
    p = Path(str(path)).expanduser().resolve(strict=False)
    record_hermes_write([p], compute_fingerprint([p]))


# ----- H2/H3/H4: gateway-lifecycle brackets --------------------------------

GATEWAY_FINGERPRINT_DIR = STATE_DIR / "gateway-fingerprints"
GATEWAY_STARTUP_LOG = LOG_DIR / "gateway-startup.jsonl"
GATEWAY_STARTUP_DRIFT_LOG = LOG_DIR / "gateway-startup-drift.jsonl"
VERIFIER_DRIFT_LOG = LOG_DIR / "verifier-drift.jsonl"
VERIFIER_LAST_COUNT = STATE_DIR / "verifier-last-count.txt"
VERIFIER_BRACKET_META = STATE_DIR / "gateway-verifier-bracket.json"


def _previous_gateway_snapshot(self_path: Path | None) -> tuple[Path, dict] | None:
    try:
        if not GATEWAY_FINGERPRINT_DIR.is_dir():
            return None
        candidates = sorted(
            (p for p in GATEWAY_FINGERPRINT_DIR.glob("*.json") if p != self_path),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in candidates:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("hashes"), dict):
                    return p, data
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return None


def _prune_gateway_snapshots(keep: int = 10) -> None:
    try:
        files = sorted(
            GATEWAY_FINGERPRINT_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _process_counts() -> dict[str, int]:
    """ps-based counts. Uses bracket trick (e.g. [c]laude) to avoid self-counting."""
    import subprocess

    def _count(pattern: str) -> int:
        try:
            r = subprocess.run(
                ["bash", "-c", f"ps auxww | grep -c '{pattern}'"],
                capture_output=True, text=True, timeout=5,
            )
            n = int(r.stdout.strip() or "0")
            return max(0, n)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return -1

    # Patterns are anchor substrings, NOT regex anchors — the leading `[c]` /
    # `[g]` / `[s]` bracket trick excludes the grep process itself.
    # hermes_count scopes to the canonical gateway command `gateway run --replace`
    # (matches both main + per-profile gateways; excludes node daemons under
    # ~/.hermes/hermes-agent/node_modules/, stale symphony helpers, and other
    # path-substring noise that the older `[h]ermes-agent` pattern swept in
    # — false-positive H4 alert RCA, MOL-557).
    return {
        "claude_count": _count("[c]laude "),
        "hermes_count": _count("[g]ateway run --replace"),
        "symphony_subprocess_count": _count("[s]ymphony_bridge"),
    }


VerifierFailureReason = Literal["timeout", "missing", "os_error"]


def _run_verifier_count(timeout_seconds: int = 30) -> tuple[int | None, VerifierFailureReason | None]:
    """Run verify_patches.sh and return (count_of_✗_lines, failure_reason).

    On success: (count, None). On failure: (None, reason). Callers should emit
    an audit row whenever reason is non-None instead of treating None as
    "no drift" (the original collapsed-None bug masked silent failures).
    """
    import subprocess

    script = Path.home() / "Code" / "hermes-poc" / "scripts" / "hermes-patches" / "verify_patches.sh"
    if not script.is_file():
        return None, "missing"
    try:
        r = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "os_error"
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    return sum(1 for line in output.splitlines() if line.startswith("✗")), None


def _read_verifier_last_count() -> int | None:
    try:
        with open(VERIFIER_LAST_COUNT, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return int(raw) if raw else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _write_verifier_last_count(count: int) -> None:
    """Plain-integer file format, byte-compat with session-stop-runtime-diff.sh."""
    try:
        VERIFIER_LAST_COUNT.parent.mkdir(parents=True, exist_ok=True)
        tmp = VERIFIER_LAST_COUNT.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{count}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, VERIFIER_LAST_COUNT)
    except OSError:
        pass


def _detect_orphaned_bracket_meta() -> dict | None:
    """If a prior gateway recorded VERIFIER_BRACKET_META but exited without the
    shutdown hook firing (SIGKILL / launchd timeout), the meta file persists
    with a now-dead pid. Detect that on next startup so we can audit it.
    """
    try:
        if not VERIFIER_BRACKET_META.is_file():
            return None
        with open(VERIFIER_BRACKET_META, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            return None
        pid = meta.get("gateway_pid")
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return meta
        except (PermissionError, OSError):
            # Process exists or we can't tell — treat as live (conservative).
            return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


def gateway_startup_hook(gateway_pid: int) -> dict:
    """H2 + H3 (startup leg) + H4. Returns a dict with the recorded startup state
    so the shutdown hook can cross-reference. Fail-open at every step.
    """
    startup_ts = _now_iso()
    out: dict = {
        "ts": startup_ts,
        "gateway_pid": int(gateway_pid),
        "snapshot_path": None,
        "drift": {},
        "verifier_startup_count": None,
        "process_counts": {},
    }
    try:
        GATEWAY_FINGERPRINT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # SIGKILL/timeout detection — if a prior gateway never ran its shutdown
    # hook, its bracket meta is still on disk with a dead pid. Emit an audit row.
    orphan = _detect_orphaned_bracket_meta()
    if orphan is not None:
        emit_audit_jsonl(
            GATEWAY_STARTUP_DRIFT_LOG,
            {
                "ts": startup_ts,
                "event": "previous_shutdown_skipped",
                "gateway_pid": int(gateway_pid),
                "previous_meta": orphan,
            },
        )

    # H2 — capture this-startup snapshot.
    hashes = compute_default_surface_fingerprint()
    snapshot = {"ts": startup_ts, "gateway_pid": int(gateway_pid), "hashes": hashes}
    snapshot_path: Path | None = GATEWAY_FINGERPRINT_DIR / f"{startup_ts.replace(':', '-')}.json"
    try:
        if snapshot_path is not None:
            with open(snapshot_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, sort_keys=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            out["snapshot_path"] = str(snapshot_path)
    except OSError:
        snapshot_path = None

    # H2 — diff vs previous snapshot.
    prev = _previous_gateway_snapshot(snapshot_path)
    if prev is not None:
        prev_path, prev_data = prev
        drift = compare_fingerprints(prev_data.get("hashes", {}), hashes)
        recorded = load_last_hermes_hashes()
        unexplained = {
            p: kind for p, kind in drift.items()
            if recorded.get(p) != hashes.get(p)
        }
        out["drift"] = drift
        if unexplained:
            emit_audit_jsonl(
                GATEWAY_STARTUP_DRIFT_LOG,
                {
                    "ts": startup_ts,
                    "event": "gateway_startup_drift",
                    "gateway_pid": int(gateway_pid),
                    "previous_snapshot": str(prev_path),
                    "drift": drift,
                    "unexplained": unexplained,
                },
            )
            telegram_alert_rate_limited(
                event_key="h2_gateway_startup_drift",
                message=(
                    f"⚠️ [H2/MOL-557] Gateway startup drift detected.\n"
                    f"Files changed since last gateway start: {len(unexplained)}\n"
                    f"PID: {gateway_pid}\n"
                    f"See ~/.hermes/logs/gateway-startup-drift.jsonl"
                ),
            )

    # H3 — startup verifier count. Audit when the verifier itself failed.
    current_fail, reason = _run_verifier_count()
    if reason is not None:
        emit_audit_jsonl(
            VERIFIER_DRIFT_LOG,
            {
                "ts": startup_ts,
                "event": "verifier_unavailable",
                "phase": "startup",
                "gateway_pid": int(gateway_pid),
                "reason": reason,
            },
        )
    if current_fail is not None:
        out["verifier_startup_count"] = current_fail
        _write_verifier_last_count(current_fail)
        try:
            with open(VERIFIER_BRACKET_META, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ts": startup_ts,
                        "count": current_fail,
                        "gateway_pid": int(gateway_pid),
                    },
                    f, sort_keys=True, indent=2,
                )
        except OSError:
            pass

    # H4 — process counts.
    counts = _process_counts()
    out["process_counts"] = counts

    emit_audit_jsonl(
        GATEWAY_STARTUP_LOG,
        {
            "ts": startup_ts,
            "event": "gateway_startup",
            "gateway_pid": int(gateway_pid),
            "claude_count": counts.get("claude_count"),
            "hermes_count": counts.get("hermes_count"),
            "symphony_subprocess_count": counts.get("symphony_subprocess_count"),
            "verifier_startup_count": current_fail,
            "drift_count": len(out["drift"]),
        },
    )
    hermes_count = counts.get("hermes_count", 0) or 0
    if hermes_count > 1:
        try:
            sys.stderr.write(
                f"[H4/MOL-557] WARNING: hermes_count={hermes_count} (>1). "
                f"P17 gateway flock may be broken — investigate.\n"
            )
        except OSError:
            pass
        telegram_alert_rate_limited(
            event_key="h4_multiple_gateways",
            message=(
                f"⚠️ [H4/MOL-557] Multiple hermes-agent processes detected.\n"
                f"hermes_count={hermes_count} on gateway PID {gateway_pid}.\n"
                f"P17 flock should serialize launchd — investigate."
            ),
        )

    _prune_gateway_snapshots(keep=10)
    return out


def gateway_shutdown_hook(startup_state: dict | None) -> None:
    """H3 shutdown leg. Re-runs the verifier and alerts if ✗-count grew."""
    if not isinstance(startup_state, dict):
        return
    startup_count = startup_state.get("verifier_startup_count")
    startup_ts = startup_state.get("ts")
    gateway_pid = startup_state.get("gateway_pid")
    if startup_count is None:
        # No baseline — clear bracket meta so next startup doesn't flag a SIGKILL.
        _clear_bracket_meta()
        return
    current_fail, reason = _run_verifier_count()
    if reason is not None:
        emit_audit_jsonl(
            VERIFIER_DRIFT_LOG,
            {
                "ts": _now_iso(),
                "event": "verifier_unavailable",
                "phase": "shutdown",
                "gateway_pid": gateway_pid,
                "startup_ts": startup_ts,
                "reason": reason,
            },
        )
    if current_fail is None or current_fail <= startup_count:
        if current_fail is not None:
            _write_verifier_last_count(current_fail)
        _clear_bracket_meta()
        return
    snapshot_path = startup_state.get("snapshot_path")
    explained_by_pr = False
    try:
        if snapshot_path:
            sp = Path(snapshot_path)
            if sp.is_file():
                with open(sp, "r", encoding="utf-8") as f:
                    snap = json.load(f)
                surface_now = compute_default_surface_fingerprint()
                diff = compare_fingerprints(snap.get("hashes", {}), surface_now)
                explained_by_pr = bool(diff)
    except (OSError, json.JSONDecodeError):
        explained_by_pr = False
    delta = current_fail - startup_count
    emit_audit_jsonl(
        VERIFIER_DRIFT_LOG,
        {
            "ts": _now_iso(),
            "event": "verifier_drift",
            "gateway_pid": gateway_pid,
            "startup_ts": startup_ts,
            "startup_count": startup_count,
            "shutdown_count": current_fail,
            "delta": delta,
            "explained_by_surface_change": explained_by_pr,
        },
    )
    if not explained_by_pr:
        telegram_alert_rate_limited(
            event_key="h3_verifier_drift",
            message=(
                f"⚠️ [H3/MOL-557] Verifier ✗-count grew during gateway run.\n"
                f"{startup_count} → {current_fail} (+{delta})\n"
                f"PID: {gateway_pid}\n"
                f"No runtime surface change explains it.\n"
                f"See ~/.hermes/logs/verifier-drift.jsonl"
            ),
        )
    _write_verifier_last_count(current_fail)
    _clear_bracket_meta()


def _clear_bracket_meta() -> None:
    """Remove the in-flight bracket meta file. Called after a clean shutdown
    leg so the next startup doesn't flag a SIGKILL false-positive."""
    try:
        VERIFIER_BRACKET_META.unlink(missing_ok=True)
    except (OSError, AttributeError):
        try:
            if VERIFIER_BRACKET_META.is_file():
                VERIFIER_BRACKET_META.unlink()
        except OSError:
            pass

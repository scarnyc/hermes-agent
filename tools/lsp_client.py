"""Stdio Language Server Protocol client for post-edit linting.

Goal
----
Replace ``npx tsc --noEmit <single-file>`` (and the ``go vet`` / ``rustfmt
--check`` siblings in ``tools/file_operations.LINTERS``) with project-aware
diagnostics from a real language server, so the agent stops chasing phantom
"Cannot find module" errors that vanish the moment ``tsconfig.json`` is in
scope.

Why a hand-rolled client (vs. an existing OSS library)
-------------------------------------------------------
Three options were evaluated before writing this:

* **microsoft/multilspy** (v0.0.15, "pre-alpha" per its own README) is the
  closest fit on paper, but it is async-first, drags in a pinned
  ``jedi-language-server`` server as a hard dependency just to wire up
  Python, and auto-downloads server binaries to ``~/.multilspy/`` —
  which collides with HERMES_HOME profile isolation and the user's own
  toolchain. Its public API is built around navigation
  (``request_definition`` / ``request_references`` / ``request_hover``)
  rather than the diagnostic flow we need, so we'd be working against
  the grain of the documented surface.
* **sansio-lsp-client** (~3K monthly downloads, single primary
  maintainer) is sans-I/O — it gives us ~50 lines of framing in exchange
  for a new dependency, but every other moving part below (subprocess
  pump, reader thread, request demux, per-``(env, root)`` cache, idle
  reaper, error containment for the lint hot path) would still be ours.
* **pygls** is server-side only; client support is "Coming Soon" in the
  v2 docs and isn't usable yet.

So the cost/benefit of a library is "import a pre-alpha or thin
dependency, still write 80% of this module, and inherit binary-download
magic we don't want." Better to own the ~500 lines, document the
contract, and pull in ``lsprotocol`` for typed messages later if/when we
need pull diagnostics or progress tokens.

What this module owns
---------------------
* Stdio JSON-RPC transport with Content-Length framing.
* Per ``(language, project_root, server_cmd)`` cached client, kept alive
  across edits so the agent only pays cold-start once per project.
* ``diagnostics(path, content)`` returns one shot of ``Diagnostic`` items
  via ``textDocument/didOpen`` + ``publishDiagnostics`` (push) with a
  short settle window. Pull diagnostics (LSP 3.17) is not used here — the
  push flow works on every server we care about, including
  ``typescript-language-server`` and ``rust-analyzer``.
* Idle reaper thread shuts down servers after a configurable quiet
  period so long-lived sessions do not accumulate processes.

Out of scope (deliberately deferred)
-------------------------------------
* Container / SSH / Modal / Daytona backends — the server has to live
  where the files live, which means baking it into the sandbox image or
  installing on first use. That is the gnarly half and gets its own PR.
* didChange / live editing — every Hermes call here re-opens the file
  with the freshly-written contents, so we never have an in-flight buffer
  to keep in sync.
* workspace/configuration, pull diagnostics, file watching — none of
  those are needed for the "one diagnostic snapshot per write" use case
  we are solving.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project-root discovery
# ---------------------------------------------------------------------------

# Filenames whose presence in a directory marks it as a project root for the
# given language id. Order matters within each tuple: the first marker found
# walking upward wins, so users can opt into a sub-project root by dropping a
# more-specific marker (``tsconfig.json``) inside a parent that also has a
# generic one (``package.json``).
PROJECT_ROOT_MARKERS: dict[str, tuple[str, ...]] = {
    "typescript": ("tsconfig.json", "jsconfig.json", "package.json"),
    "typescriptreact": ("tsconfig.json", "jsconfig.json", "package.json"),
    "javascript": ("jsconfig.json", "package.json"),
    "javascriptreact": ("jsconfig.json", "package.json"),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
}


def find_project_root(file_path: str, language_id: str) -> Optional[str]:
    """Walk upward from *file_path* looking for the first project marker.

    Returns the absolute directory path or ``None`` if no marker is found
    before hitting the filesystem root. ``None`` is the signal to skip
    LSP and fall back to the shell linter — an orphan file with no
    project context will produce the same phantom errors LSP was meant to
    eliminate.
    """
    markers = PROJECT_ROOT_MARKERS.get(language_id)
    if not markers:
        return None
    try:
        start = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    cursor = start.parent if start.is_file() or not start.exists() else start
    visited = 0
    while True:
        for marker in markers:
            if (cursor / marker).exists():
                return str(cursor)
        parent = cursor.parent
        if parent == cursor:
            return None
        cursor = parent
        visited += 1
        # Defensive cap: the deepest reasonable monorepo is well under this.
        if visited > 64:
            return None


# ---------------------------------------------------------------------------
# LSP framing
# ---------------------------------------------------------------------------

_HEADER_TERMINATOR = b"\r\n\r\n"


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _read_message(stream) -> Optional[dict[str, Any]]:
    """Read a single LSP message from *stream*. Returns ``None`` at EOF."""
    header = b""
    while _HEADER_TERMINATOR not in header:
        chunk = stream.read(1)
        if not chunk:
            return None
        header += chunk
        # Pathological server: header floods without terminator. Cap so we
        # do not spin forever consuming bytes.
        if len(header) > 8192:
            raise RuntimeError("LSP header exceeded 8 KiB without terminator")
    header_text, _, _ = header.partition(_HEADER_TERMINATOR)
    content_length: Optional[int] = None
    for line in header_text.decode("ascii", errors="replace").splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                content_length = None
    if content_length is None or content_length < 0:
        raise RuntimeError(f"LSP header missing Content-Length: {header_text!r}")
    body = b""
    while len(body) < content_length:
        chunk = stream.read(content_length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LSP body was not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Diagnostic shape (subset of the LSP type)
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    """Subset of the LSP ``Diagnostic`` we surface to the lint layer."""

    line: int  # 1-based; LSP is 0-based and we convert at parse time.
    column: int  # 1-based for the same reason.
    severity: int = 1  # 1=error, 2=warning, 3=info, 4=hint
    message: str = ""
    source: str = ""
    code: str = ""

    @classmethod
    def from_lsp(cls, raw: dict[str, Any]) -> "Diagnostic":
        rng = raw.get("range") or {}
        start = rng.get("start") or {}
        line = int(start.get("line", 0)) + 1
        col = int(start.get("character", 0)) + 1
        code = raw.get("code", "")
        return cls(
            line=line,
            column=col,
            severity=int(raw.get("severity", 1)),
            message=str(raw.get("message", "")).strip(),
            source=str(raw.get("source", "")),
            code=str(code) if code != "" else "",
        )

    def format(self, path: str) -> str:
        sev = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(self.severity, "error")
        prefix = f"{path}:{self.line}:{self.column}"
        tail = f" [{self.source}{':' + self.code if self.code else ''}]" if self.source else ""
        return f"{prefix}: {sev}: {self.message}{tail}"


def format_diagnostics(path: str, diags: Iterable[Diagnostic]) -> str:
    """Render a diagnostic list to the same `path:line:col: msg` shape that
    the existing shell linters produce, so ``_check_lint_delta`` can keep
    diffing pre/post by line equality."""
    return "\n".join(d.format(path) for d in diags)


# ---------------------------------------------------------------------------
# Pending-request bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Any = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LspClient:
    """Long-lived stdio JSON-RPC client for one (server, project root)."""

    def __init__(
        self,
        server_cmd: Sequence[str],
        project_root: str,
        language_id: str,
        *,
        startup_timeout: float = 15.0,
    ) -> None:
        self.server_cmd = list(server_cmd)
        self.project_root = project_root
        self.language_id = language_id
        self.startup_timeout = startup_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._writer_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._next_id = 1
        self._next_id_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diag_events: dict[str, threading.Event] = {}
        self._diag_lock = threading.Lock()
        self._closed = False
        self._last_used = time.monotonic()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.server_cmd,
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Capture stderr separately so server-side noise (rust-analyzer
                # progress, ts-server compile warnings) never corrupts the
                # JSON-RPC stream on stdout.
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise RuntimeError(f"failed to spawn LSP server {self.server_cmd[0]!r}: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"lsp-reader[{self.language_id}]",
            daemon=True,
        )
        self._reader_thread.start()

        try:
            self._initialize()
        except Exception:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Best-effort graceful shutdown: send `shutdown` + `exit`. If the
        # server is unresponsive we just kill the process — the reader
        # thread is daemonized and will die with stdout EOF.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self._request("shutdown", None, timeout=2.0)
            except Exception:
                pass
            try:
                self._notify("exit", None)
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None

    # -- JSON-RPC ----------------------------------------------------------

    def _allocate_id(self) -> int:
        with self._next_id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("LSP client not started")
        data = _encode_message(payload)
        with self._writer_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"LSP server stdin closed: {exc}") from exc

    def _notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Any, *, timeout: float) -> Any:
        request_id = self._allocate_id()
        pending = _Pending()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            })
            if not pending.event.wait(timeout):
                raise TimeoutError(f"LSP {method!r} timed out after {timeout:.1f}s")
            if pending.error is not None:
                raise RuntimeError(f"LSP {method!r} error: {pending.error}")
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    # -- reader -------------------------------------------------------------

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while not self._closed:
                try:
                    msg = _read_message(proc.stdout)
                except RuntimeError as exc:
                    logger.debug("LSP reader bailing on %s: %s", self.server_cmd[0], exc)
                    break
                if msg is None:
                    break
                self._handle_message(msg)
        finally:
            # Wake up every waiter so callers do not hang forever when the
            # server dies mid-request.
            with self._pending_lock:
                for pending in self._pending.values():
                    if pending.error is None and pending.result is None:
                        pending.error = "server stream closed"
                    pending.event.set()
            with self._diag_lock:
                for event in self._diag_events.values():
                    event.set()

    def _handle_message(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            try:
                request_id = int(msg["id"])
            except (TypeError, ValueError):
                return
            with self._pending_lock:
                pending = self._pending.get(request_id)
            if pending is None:
                return
            pending.result = msg.get("result")
            pending.error = msg.get("error")
            pending.event.set()
            return

        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = params.get("uri")
            raw_list = params.get("diagnostics") or []
            if not isinstance(uri, str):
                return
            parsed = [Diagnostic.from_lsp(d) for d in raw_list if isinstance(d, dict)]
            with self._diag_lock:
                self._diagnostics[uri] = parsed
                event = self._diag_events.get(uri)
                if event is not None:
                    event.set()
            return

        # Server-issued requests (e.g. workspace/configuration). Reply with
        # a generic null so the server stops waiting. We do not implement
        # any server features beyond what's needed to coax diagnostics out.
        if "id" in msg and "method" in msg:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    # -- handshake ----------------------------------------------------------

    def _initialize(self) -> None:
        root_uri = path_to_uri(self.project_root)
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "hermes-agent", "version": "1"},
            "rootUri": root_uri,
            "rootPath": self.project_root,
            "workspaceFolders": [{"uri": root_uri, "name": Path(self.project_root).name}],
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "willSave": False, "dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": False, "versionSupport": False},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                },
            },
            "initializationOptions": {},
        }
        self._request("initialize", params, timeout=self.startup_timeout)
        self._notify("initialized", {})

    # -- public API ---------------------------------------------------------

    def diagnostics(
        self,
        file_path: str,
        content: str,
        *,
        timeout: float = 10.0,
        settle_ms: int = 400,
    ) -> list[Diagnostic]:
        """Open *file_path* with *content* and collect one diagnostic snapshot.

        The flow:
          1. Register a fresh wait-event for the URI.
          2. ``didOpen`` with the new content (LSP version 1).
          3. Block until the first ``publishDiagnostics`` for the URI or
             *timeout* seconds elapse.
          4. Sleep *settle_ms* milliseconds and re-snapshot, since some
             servers (notably ``typescript-language-server``) emit a
             "no diagnostics yet" notification while the program graph is
             still loading and only the second batch carries real errors.
          5. ``didClose`` so the server frees the buffer.
        """
        if self._closed or self._proc is None:
            raise RuntimeError("LSP client is not running")

        uri = path_to_uri(file_path)
        event = threading.Event()
        with self._diag_lock:
            self._diagnostics.pop(uri, None)
            self._diag_events[uri] = event

        try:
            self._notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.language_id,
                    "version": 1,
                    "text": content,
                },
            })
            self._last_used = time.monotonic()

            event.wait(timeout)

            if settle_ms > 0:
                time.sleep(settle_ms / 1000.0)

            with self._diag_lock:
                diags = list(self._diagnostics.get(uri, []))
        finally:
            with self._diag_lock:
                self._diag_events.pop(uri, None)
            try:
                self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except RuntimeError:
                pass

        return diags

    @property
    def last_used(self) -> float:
        return self._last_used

    @property
    def is_alive(self) -> bool:
        proc = self._proc
        return not self._closed and proc is not None and proc.poll() is None


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------


def path_to_uri(path: str) -> str:
    """Convert a filesystem path to a ``file://`` URI.

    Uses ``Path.as_uri`` after resolving so symlinks and ``..`` components
    do not produce two URIs for the same file (which would cause the
    diagnostics dict to miss the response).
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(path).expanduser()
    try:
        return resolved.as_uri()
    except ValueError:
        # ``as_uri`` rejects relative paths; the resolve() above almost
        # always prevents this, but fall back to a manual file:// prefix
        # so we never raise from inside the lint hot path.
        return "file://" + str(resolved).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Process-wide registry + idle reaper
# ---------------------------------------------------------------------------


_clients_lock = threading.Lock()
_clients: dict[tuple[str, str, tuple[str, ...]], LspClient] = {}
_reaper_thread: Optional[threading.Thread] = None
_reaper_stop = threading.Event()
_reaper_idle_seconds = 600.0
_reaper_interval = 30.0


def get_or_start_client(
    language_id: str,
    project_root: str,
    server_cmd: Sequence[str],
    *,
    idle_seconds: float = 600.0,
) -> Optional[LspClient]:
    """Return a started client for the (language, root, cmd) triple.

    Returns ``None`` if the server binary is missing or fails to start —
    callers are expected to treat that as "fall back to shell linter"
    rather than a hard error.
    """
    if not server_cmd:
        return None
    binary = server_cmd[0]
    if not _which(binary):
        return None

    key = (language_id, project_root, tuple(server_cmd))
    with _clients_lock:
        client = _clients.get(key)
        if client is not None and client.is_alive:
            return client
        if client is not None:
            # Stale entry — server crashed or was reaped. Drop it before
            # respawning so we never hand a dead client back to a caller.
            _clients.pop(key, None)

        client = LspClient(server_cmd, project_root, language_id)
        try:
            client.start()
        except Exception as exc:
            logger.warning("LSP server %s failed to start: %s", server_cmd[0], exc)
            return None
        _clients[key] = client
        global _reaper_idle_seconds
        _reaper_idle_seconds = max(_reaper_idle_seconds, idle_seconds)
        _ensure_reaper()
        return client


def shutdown_all_clients() -> None:
    """Stop every cached server. Intended for tests and process exit."""
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        try:
            client.shutdown()
        except Exception:
            pass
    _reaper_stop.set()


def _which(binary: str) -> Optional[str]:
    # Honour absolute / relative paths verbatim — ``shutil.which`` only
    # consults PATH, and a user pointing at ``./bin/typescript-language-server``
    # in their config should still work.
    if os.sep in binary or (os.altsep and os.altsep in binary):
        return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def _ensure_reaper() -> None:
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return
    _reaper_stop.clear()
    _reaper_thread = threading.Thread(
        target=_reaper_loop,
        name="lsp-reaper",
        daemon=True,
    )
    _reaper_thread.start()


def _reaper_loop() -> None:
    while not _reaper_stop.wait(_reaper_interval):
        now = time.monotonic()
        idle_cutoff = now - _reaper_idle_seconds
        to_close: list[LspClient] = []
        with _clients_lock:
            for key, client in list(_clients.items()):
                if not client.is_alive or client.last_used < idle_cutoff:
                    _clients.pop(key, None)
                    to_close.append(client)
        for client in to_close:
            try:
                client.shutdown()
            except Exception:
                pass

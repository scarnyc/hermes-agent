"""Tests for ``tools.lsp_client``.

The bulk of this module is covered by exercising the real JSON-RPC stack
against a *fake* language server we ship in-tree as a tiny Python script.
That keeps the tests hermetic — no system typescript-language-server / gopls
required — while still proving the framing, request/response demux, and
publishDiagnostics wait loop behave end-to-end.

Pure helper functions (path → URI, project-root walk, message framing) are
covered in plain unit tests above.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tools import lsp_client
from tools.lsp_client import (
    Diagnostic,
    LspClient,
    _encode_message,
    _read_message,
    find_project_root,
    format_diagnostics,
    get_or_start_client,
    path_to_uri,
    shutdown_all_clients,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestProjectRoot:
    def test_walks_up_to_tsconfig(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        (root / "src" / "components").mkdir(parents=True)
        (root / "tsconfig.json").write_text("{}")
        deep = root / "src" / "components" / "Button.tsx"
        deep.write_text("export {}\n")
        assert find_project_root(str(deep), "typescript") == str(root.resolve())

    def test_subproject_marker_wins_over_parent(self, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        inner = outer / "packages" / "ui"
        (inner / "src").mkdir(parents=True)
        (outer / "package.json").write_text("{}")
        (inner / "tsconfig.json").write_text("{}")
        leaf = inner / "src" / "App.tsx"
        leaf.write_text("export {}\n")
        # tsconfig appears first in the marker tuple, so the inner package
        # wins — even though package.json exists higher up.
        assert find_project_root(str(leaf), "typescriptreact") == str(inner.resolve())

    def test_returns_none_when_no_marker(self, tmp_path: Path) -> None:
        orphan = tmp_path / "orphan.ts"
        orphan.write_text("export {}\n")
        assert find_project_root(str(orphan), "typescript") is None

    def test_returns_none_for_unknown_language(self, tmp_path: Path) -> None:
        f = tmp_path / "foo.lol"
        f.write_text("")
        assert find_project_root(str(f), "klingon") is None


class TestUri:
    def test_round_trip_absolute_path(self, tmp_path: Path) -> None:
        path = tmp_path / "x.ts"
        path.write_text("")
        uri = path_to_uri(str(path))
        assert uri.startswith("file://")
        assert uri.endswith("x.ts")

    def test_relative_path_does_not_raise(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        # Should not raise even before the file exists.
        uri = path_to_uri("not-yet-created.ts")
        assert uri.startswith("file://")


class TestFraming:
    def test_encode_then_decode_roundtrip(self) -> None:
        payload = {"jsonrpc": "2.0", "id": 7, "method": "x", "params": {"a": 1}}
        wire = _encode_message(payload)
        assert wire.startswith(b"Content-Length: ")
        # Read it back through _read_message using a BytesIO.
        decoded = _read_message(io.BytesIO(wire))
        assert decoded == payload

    def test_eof_returns_none(self) -> None:
        assert _read_message(io.BytesIO(b"")) is None

    def test_garbage_header_raises(self) -> None:
        # Header without Content-Length (8KB cap doesn't kick in here, but
        # parser should still reject after seeing the terminator).
        bad = b"X-Garbage: yes\r\n\r\n"
        with pytest.raises(RuntimeError):
            _read_message(io.BytesIO(bad))


class TestDiagnostic:
    def test_from_lsp_converts_zero_based_to_one_based(self) -> None:
        raw: dict[str, Any] = {
            "range": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 8}},
            "severity": 1,
            "message": "Cannot find name 'foo'.",
            "source": "ts",
            "code": 2304,
        }
        d = Diagnostic.from_lsp(raw)
        assert d.line == 10
        assert d.column == 5
        assert d.severity == 1
        assert d.code == "2304"

    def test_format_includes_path_and_source(self) -> None:
        d = Diagnostic(line=3, column=2, severity=2, message="unused", source="eslint", code="no-unused")
        text = d.format("/tmp/x.ts")
        assert text.startswith("/tmp/x.ts:3:2:")
        assert "warning" in text
        assert "unused" in text
        assert "eslint" in text

    def test_format_diagnostics_joins_with_newlines(self) -> None:
        out = format_diagnostics(
            "/tmp/x.ts",
            [
                Diagnostic(line=1, column=1, message="a"),
                Diagnostic(line=2, column=1, message="b"),
            ],
        )
        assert out.split("\n") == [
            "/tmp/x.ts:1:1: error: a",
            "/tmp/x.ts:2:1: error: b",
        ]


# ---------------------------------------------------------------------------
# Fake LSP server (a short Python script we exec via subprocess)
# ---------------------------------------------------------------------------


_FAKE_SERVER_TEMPLATE = r"""
import json
import sys
import struct

# Modes:
#   "ok"        -> publish empty diagnostics for any didOpen URI.
#   "errors"    -> publish two diagnostics for any didOpen URI.
#   "settle"    -> publish empty diagnostics first, then publish two
#                  diagnostics 50ms later. Exercises the settle window.
MODE = {mode!r}

def _read():
    header = b''
    while b'\r\n\r\n' not in header:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        header += chunk
    length = int(header.split(b'Content-Length:')[1].split(b'\r\n')[0].strip())
    body = b''
    while len(body) < length:
        chunk = sys.stdin.buffer.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode('utf-8'))

def _write(payload):
    body = json.dumps(payload).encode('utf-8')
    sys.stdout.buffer.write(b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    sys.stdout.buffer.flush()

def _diags(uri):
    return [
        {{"range": {{"start": {{"line": 0, "character": 0}}, "end": {{"line": 0, "character": 1}}}},
          "severity": 1, "message": "boom", "source": "fake", "code": "E1"}},
        {{"range": {{"start": {{"line": 1, "character": 4}}, "end": {{"line": 1, "character": 6}}}},
          "severity": 2, "message": "warn", "source": "fake"}},
    ]

while True:
    msg = _read()
    if msg is None:
        break
    if 'id' in msg and msg.get('method') == 'initialize':
        _write({{"jsonrpc": "2.0", "id": msg['id'], "result": {{"capabilities": {{}}}}}})
    elif 'id' in msg and msg.get('method') == 'shutdown':
        _write({{"jsonrpc": "2.0", "id": msg['id'], "result": None}})
    elif msg.get('method') == 'exit':
        break
    elif msg.get('method') == 'textDocument/didOpen':
        uri = msg['params']['textDocument']['uri']
        if MODE == 'ok':
            _write({{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                     "params": {{"uri": uri, "diagnostics": []}}}})
        elif MODE == 'errors':
            _write({{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                     "params": {{"uri": uri, "diagnostics": _diags(uri)}}}})
        elif MODE == 'settle':
            _write({{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                     "params": {{"uri": uri, "diagnostics": []}}}})
            import time as _t; _t.sleep(0.05)
            _write({{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                     "params": {{"uri": uri, "diagnostics": _diags(uri)}}}})
"""


@pytest.fixture
def fake_server_factory(tmp_path: Path):
    """Return a callable that materialises a fake-LSP launch command.

    We materialise per-mode so tests can choose between the empty and
    error-emitting servers without juggling globals.
    """

    def _make(mode: str) -> list[str]:
        script = tmp_path / f"fake_lsp_{mode}.py"
        script.write_text(_FAKE_SERVER_TEMPLATE.format(mode=mode))
        # Use the same interpreter that's running pytest so the fake
        # server inherits the same Python ABI on every CI image.
        return [sys.executable, str(script)]

    return _make


@pytest.fixture(autouse=True)
def _shutdown_registry_between_tests():
    """Make sure the module-global client cache cannot leak servers
    across tests — every cached subprocess is real and would survive."""
    yield
    shutdown_all_clients()


class TestClientHandshakeAndDiagnostics:
    def test_clean_file_returns_no_diagnostics(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("ok")
        client = LspClient(cmd, str(tmp_path), "typescript")
        client.start()
        try:
            diags = client.diagnostics(str(tmp_path / "a.ts"), "export {}\n", timeout=3.0, settle_ms=0)
        finally:
            client.shutdown()
        assert diags == []

    def test_dirty_file_surfaces_error_and_warning(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("errors")
        client = LspClient(cmd, str(tmp_path), "typescript")
        client.start()
        try:
            diags = client.diagnostics(str(tmp_path / "a.ts"), "x;\n", timeout=3.0, settle_ms=0)
        finally:
            client.shutdown()
        # Two diagnostics with severities 1 (error) and 2 (warning).
        assert sorted(d.severity for d in diags) == [1, 2]
        assert any("boom" in d.message for d in diags)

    def test_settle_window_picks_up_late_diagnostics(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("settle")
        client = LspClient(cmd, str(tmp_path), "typescript")
        client.start()
        try:
            # Without a settle window we'd only see the empty first batch;
            # the 200ms window must be long enough to capture the second
            # publishDiagnostics that lands ~50ms after didOpen.
            diags = client.diagnostics(str(tmp_path / "a.ts"), "x;\n", timeout=3.0, settle_ms=200)
        finally:
            client.shutdown()
        assert len(diags) == 2

    def test_request_timeout_does_not_corrupt_state(self, tmp_path: Path) -> None:
        # A "server" that reads but never writes anything back. Initialize
        # must time out cleanly without deadlocking the writer lock.
        script = tmp_path / "silent_lsp.py"
        script.write_text("import sys\nwhile sys.stdin.buffer.read(1):\n    pass\n")
        client = LspClient([sys.executable, str(script)], str(tmp_path), "typescript", startup_timeout=0.5)
        with pytest.raises(TimeoutError):
            client.start()


class TestRegistry:
    def test_missing_binary_returns_none(self, tmp_path: Path) -> None:
        result = get_or_start_client(
            "typescript",
            str(tmp_path),
            ["definitely-not-a-real-binary-xyz123"],
        )
        assert result is None

    def test_caches_per_root(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("ok")
        a = get_or_start_client("typescript", str(tmp_path), cmd)
        b = get_or_start_client("typescript", str(tmp_path), cmd)
        assert a is not None and b is not None
        assert a is b

    def test_separate_roots_get_separate_clients(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("ok")
        root_a = tmp_path / "a"; root_a.mkdir()
        root_b = tmp_path / "b"; root_b.mkdir()
        a = get_or_start_client("typescript", str(root_a), cmd)
        b = get_or_start_client("typescript", str(root_b), cmd)
        assert a is not None and b is not None
        assert a is not b


class TestShutdown:
    def test_shutdown_terminates_server(self, fake_server_factory, tmp_path: Path) -> None:
        cmd = fake_server_factory("ok")
        client = LspClient(cmd, str(tmp_path), "typescript")
        client.start()
        proc = client._proc
        assert proc is not None
        client.shutdown()
        # The server must exit promptly; otherwise we'd be leaking processes
        # in long-lived agent sessions every time idle_shutdown reaps a client.
        assert proc.wait(timeout=3.0) is not None

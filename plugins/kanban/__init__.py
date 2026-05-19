"""Kanban board plugin — multi-agent collaboration dashboard.

This plugin provides a dashboard tab (``plugins/kanban/dashboard/``)
for the Hermes web UI.  The core kanban functionality — DB layer, CLI
subcommand (``hermes kanban …``), worker tools, dispatcher, and
diagnostics — lives in ``hermes_cli/kanban*.py`` and is always
available.  This plugin *only* governs the dashboard front-end tab and
its REST/WebSocket API layer.

When enabled, the kanban dashboard tab appears in the Hermes web UI
(after the Skills tab).  The dashboard's backend (FastAPI routes in
``plugin_api.py``) is mounted by the web server at
``/api/plugins/kanban/``.

Plugin discovery
----------------
The plugin system (``hermes_cli/plugins.py``) discovers this plugin
via ``plugin.yaml`` and loads this ``register()`` entry point.  The
web server discovers the dashboard tab independently via
``_discover_dashboard_plugins()`` which scans for
``plugins/*/dashboard/manifest.json``.
"""

from __future__ import annotations


def register(ctx) -> None:
    """No-op: kanban tools and CLI commands are registered in the core.

    The 7 worker tools (kanban_show, kanban_complete, etc.) are
    registered statically in ``tools/kanban_tools.py`` via the core
    tool registry.  The ``/kanban`` slash command and ``hermes kanban``
    CLI subcommand are registered in ``hermes_cli/commands.py``.

    This plugin's role is limited to the dashboard tab; see the
    module docstring for details.
    """
    # The dashboard tab is discovered independently by
    # hermes_cli.web_server._discover_dashboard_plugins().
    pass

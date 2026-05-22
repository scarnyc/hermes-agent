"""Per-model analytics over the local SessionDB.

Backs ``hermes models-stats`` CLI subcommand.  No web/HTTP dependency.

Adapts the upstream Models-dashboard analytics path (commit e6b05eaf) to
the v0.8.0 schema by substituting ``message_count`` for ``api_call_count``
in the ``SUM(COALESCE(..., 0)) as api_calls`` rollups — the v0.8.0
``sessions`` table tracks per-turn messages, not per-call API counts.
The ``as api_calls`` alias is preserved so downstream consumers
(``--json`` output, dashboard scrape) stay schema-compatible.

P111/MOL-428 — Per-model analytics CLI (post-MOL-428 manual port from
upstream e6b05eaf6 + 113239f6e; CLI surface replaces the FastAPI endpoint
since v0.8.0 ships no web frontend).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List


def compute_models_analytics(days: int = 30) -> Dict[str, Any]:
    """Return per-model token/cost/session rollups over the last ``days``.

    Mirrors the dict shape returned by the upstream
    ``/api/analytics/models`` endpoint so any downstream JSON consumer
    (dashboard scrape, telemetry pipeline) keeps working unchanged.

    P111/MOL-428 — schema adaptation: v0.8.0 ``sessions`` carries
    ``message_count`` rather than ``api_call_count``; the rollup keeps
    the ``api_calls`` alias for shape stability.
    """
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)

        cur = db._conn.execute(
            """
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(message_count, 0)) as api_calls,
                   SUM(tool_call_count) as tool_calls,
                   MAX(started_at) as last_used_at,
                   AVG(input_tokens + output_tokens) as avg_tokens_per_session
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model, billing_provider
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in cur.fetchall()]

        models: List[Dict[str, Any]] = []
        for row in rows:
            provider = row.get("billing_provider") or ""
            model_name = row["model"]
            caps: Dict[str, Any] = {}
            try:
                from agent.models_dev import get_model_capabilities

                mc = get_model_capabilities(provider=provider, model=model_name)
                if mc is not None:
                    caps = {
                        "supports_tools": mc.supports_tools,
                        "supports_vision": mc.supports_vision,
                        "supports_reasoning": mc.supports_reasoning,
                        "context_window": mc.context_window,
                        "max_output_tokens": mc.max_output_tokens,
                        "model_family": mc.model_family,
                    }
            except Exception:
                pass

            models.append(
                {
                    "model": model_name,
                    "provider": provider,
                    "input_tokens": row["input_tokens"] or 0,
                    "output_tokens": row["output_tokens"] or 0,
                    "cache_read_tokens": row["cache_read_tokens"] or 0,
                    "reasoning_tokens": row["reasoning_tokens"] or 0,
                    "estimated_cost": row["estimated_cost"] or 0,
                    "actual_cost": row["actual_cost"] or 0,
                    "sessions": row["sessions"] or 0,
                    "api_calls": row["api_calls"] or 0,
                    "tool_calls": row["tool_calls"] or 0,
                    "last_used_at": row["last_used_at"],
                    "avg_tokens_per_session": row["avg_tokens_per_session"] or 0,
                    "capabilities": caps,
                }
            )

        totals_cur = db._conn.execute(
            """
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(message_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            """,
            (cutoff,),
        )
        totals_row = totals_cur.fetchone()
        totals = dict(totals_row) if totals_row is not None else {}

        return {
            "models": models,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()


def format_models_table(analytics: Dict[str, Any]) -> str:
    """Render analytics dict as a fixed-width text table.

    Column order: MODEL / SESSIONS / API / INPUT / OUTPUT / REASONING /
    EST. COST.  Trailing totals line summarises ``distinct_models`` and
    grand totals for input/output/cost.
    """
    models = analytics.get("models", []) or []
    if not models:
        period = analytics.get("period_days", 0)
        return f"No model activity in the last {period} day(s)."

    header = (
        f"{'MODEL':<40} {'SESSIONS':>8} {'API':>8} "
        f"{'INPUT':>12} {'OUTPUT':>12} {'REASONING':>10} {'EST. COST':>12}"
    )
    sep = "-" * len(header)
    lines: List[str] = [header, sep]

    for m in models:
        model_label = m["model"]
        provider = m.get("provider") or ""
        if provider:
            model_label = f"{model_label} ({provider})"
        if len(model_label) > 40:
            model_label = model_label[:37] + "..."

        cost = m.get("estimated_cost") or 0
        cost_str = f"${cost:,.4f}" if cost else "$0.0000"
        lines.append(
            f"{model_label:<40} "
            f"{int(m.get('sessions') or 0):>8} "
            f"{int(m.get('api_calls') or 0):>8} "
            f"{int(m.get('input_tokens') or 0):>12,} "
            f"{int(m.get('output_tokens') or 0):>12,} "
            f"{int(m.get('reasoning_tokens') or 0):>10,} "
            f"{cost_str:>12}"
        )

    totals = analytics.get("totals") or {}
    distinct = int(totals.get("distinct_models") or 0)
    total_input = int(totals.get("total_input") or 0)
    total_output = int(totals.get("total_output") or 0)
    total_cost = float(totals.get("total_estimated_cost") or 0)
    period = analytics.get("period_days", 0)
    lines.append(sep)
    lines.append(
        f"Totals over {period}d: {distinct} model(s), "
        f"{total_input:,} input + {total_output:,} output tokens, "
        f"est. ${total_cost:,.4f}"
    )
    return "\n".join(lines)


def cmd_models_stats(args) -> int:
    """``hermes models-stats`` CLI handler.

    Flags:
        --days N     Look-back window (default 30)
        --json       Emit raw JSON instead of the text table
    """
    days = int(getattr(args, "days", 30) or 30)
    analytics = compute_models_analytics(days=days)

    if getattr(args, "json", False):
        print(json.dumps(analytics, indent=2, default=str))
        return 0

    print(format_models_table(analytics))
    return 0

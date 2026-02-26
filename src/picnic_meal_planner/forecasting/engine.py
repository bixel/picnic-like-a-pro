"""Forecasting engine — average interval model.

For each product with >= 2 historical orders:
  1. avg_interval  = average days between consecutive orders
  2. avg_quantity  = average quantity per order
  3. last_ordered  = date of most recent order
  4. days_since    = today - last_ordered
  5. urgency_score = days_since / avg_interval

Products with urgency_score >= 1.0 are overdue.

This module is intentionally decoupled from the rest of the system so that a
more sophisticated model (ML, seasonal decomposition, etc.) can replace just
this file without touching anything else.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_urgency(stats: dict) -> dict:
    """Given a product stats dict (from queries.get_product_order_stats),
    return the dict enriched with urgency fields.

    Returns None if the product has fewer than 2 orders (can't compute interval).
    """
    avg_interval = stats.get("avg_interval_days")
    last_ordered_at = stats.get("last_ordered_at")

    if avg_interval is None or avg_interval <= 0 or last_ordered_at is None:
        return {**stats, "urgency_score": None, "days_since_last": None}

    last_dt = _parse_dt(last_ordered_at)
    days_since = (_today() - last_dt).total_seconds() / 86400
    urgency_score = days_since / avg_interval

    return {
        **stats,
        "days_since_last": round(days_since, 1),
        "urgency_score": round(urgency_score, 3),
    }


def forecast_products(
    all_stats: list[dict],
    horizon_days: int = 7,
) -> list[dict]:
    """Return products predicted to run out within `horizon_days`, ranked by urgency.

    Args:
        all_stats: List of product stat dicts from queries.get_all_product_stats().
        horizon_days: Only include products expected to be needed within this window.

    Returns:
        List of enriched stat dicts, sorted by urgency_score descending.
    """
    enriched = [compute_urgency(s) for s in all_stats]

    # Filter: products already overdue OR due within horizon
    results = []
    for item in enriched:
        score = item.get("urgency_score")
        if score is None:
            continue
        avg_interval = item.get("avg_interval_days")
        days_since = item.get("days_since_last", 0)
        if avg_interval is None:
            continue
        # Estimate days until next needed
        days_until_due = avg_interval - days_since
        if days_until_due <= horizon_days:
            results.append({**item, "days_until_due": round(days_until_due, 1)})

    results.sort(key=lambda x: x["urgency_score"], reverse=True)
    return results


def get_reorder_due(all_stats: list[dict]) -> list[dict]:
    """Return products whose reorder interval has already elapsed (urgency >= 1.0)."""
    enriched = [compute_urgency(s) for s in all_stats]
    overdue = [
        item for item in enriched
        if item.get("urgency_score") is not None and item["urgency_score"] >= 1.0
    ]
    overdue.sort(key=lambda x: x["urgency_score"], reverse=True)
    return overdue

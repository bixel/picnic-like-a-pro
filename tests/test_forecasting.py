"""Unit tests for forecasting/engine.py – the average-interval model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from picnic_meal_planner.db.queries import get_all_product_stats
from picnic_meal_planner.forecasting import engine as forecasting


def _dt(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _make_stats(
    product_id: str = "p_test",
    order_count: int = 5,
    avg_quantity: float = 1.0,
    avg_interval_days: float | None = 7.0,
    last_ordered_at_days_ago: float = 8.0,
) -> dict:
    return {
        "product_id": product_id,
        "name": "Test Product",
        "order_count": order_count,
        "avg_quantity": avg_quantity,
        "avg_interval_days": avg_interval_days,
        "last_ordered_at": _dt(last_ordered_at_days_ago),
    }


class TestComputeUrgency:
    def test_returns_urgency_score(self):
        result = forecasting.compute_urgency(_make_stats())
        assert 1.0 <= result["urgency_score"] <= 1.3

    def test_days_since_last_is_correct(self):
        result = forecasting.compute_urgency(_make_stats(last_ordered_at_days_ago=10.0))
        assert abs(result["days_since_last"] - 10.0) < 0.2

    def test_no_interval_gives_none_urgency(self):
        result = forecasting.compute_urgency(_make_stats(avg_interval_days=None, order_count=1))
        assert result["urgency_score"] is None
        assert result["days_since_last"] is None

    def test_zero_interval_gives_none_urgency(self):
        assert forecasting.compute_urgency(_make_stats(avg_interval_days=0.0))["urgency_score"] is None

    def test_negative_interval_gives_none_urgency(self):
        assert forecasting.compute_urgency(_make_stats(avg_interval_days=-5.0))["urgency_score"] is None

    def test_missing_last_ordered_gives_none_urgency(self):
        stats = _make_stats()
        stats["last_ordered_at"] = None
        assert forecasting.compute_urgency(stats)["urgency_score"] is None

    def test_not_yet_overdue(self):
        result = forecasting.compute_urgency(
            _make_stats(avg_interval_days=14.0, last_ordered_at_days_ago=6.0)
        )
        assert result["urgency_score"] < 1.0

    def test_overdue(self):
        result = forecasting.compute_urgency(
            _make_stats(avg_interval_days=7.0, last_ordered_at_days_ago=9.0)
        )
        assert result["urgency_score"] > 1.0

    def test_enriched_result_preserves_original_fields(self):
        result = forecasting.compute_urgency(_make_stats(product_id="p_abc", order_count=10))
        assert result["product_id"] == "p_abc"
        assert result["order_count"] == 10

    def test_urgency_score_is_rounded_to_3dp(self):
        result = forecasting.compute_urgency(_make_stats())
        assert result["urgency_score"] == round(result["urgency_score"], 3)

    def test_days_since_last_is_rounded_to_1dp(self):
        result = forecasting.compute_urgency(_make_stats(last_ordered_at_days_ago=8.123456))
        assert result["days_since_last"] == round(result["days_since_last"], 1)

    def test_naive_datetime_is_handled(self):
        stats = _make_stats()
        stats["last_ordered_at"] = (datetime.now() - timedelta(days=8)).isoformat()
        assert forecasting.compute_urgency(stats)["urgency_score"] is not None


class TestForecastProducts:
    def test_empty_input_returns_empty(self):
        assert forecasting.forecast_products([]) == []

    def test_product_within_horizon_included(self):
        assert len(forecasting.forecast_products([_make_stats()], horizon_days=7)) == 1

    def test_product_outside_horizon_excluded(self):
        # 28-day interval, 8 days elapsed → 20 days until due, outside 7-day window
        stats = _make_stats(avg_interval_days=28.0, last_ordered_at_days_ago=8.0)
        assert forecasting.forecast_products([stats], horizon_days=7) == []

    def test_wider_horizon_includes_more(self):
        stats = _make_stats(avg_interval_days=14.0, last_ordered_at_days_ago=8.0)
        assert len(forecasting.forecast_products([stats], horizon_days=5)) == 0
        assert len(forecasting.forecast_products([stats], horizon_days=14)) == 1

    def test_sorted_by_urgency_descending(self):
        a = _make_stats("p_a", avg_interval_days=7.0, last_ordered_at_days_ago=12.0)
        b = _make_stats("p_b", avg_interval_days=7.0, last_ordered_at_days_ago=8.0)
        results = forecasting.forecast_products([b, a], horizon_days=7)
        assert results[0]["product_id"] == "p_a"

    def test_days_until_due_included_in_result(self):
        results = forecasting.forecast_products([_make_stats()], horizon_days=7)
        assert "days_until_due" in results[0]

    def test_overdue_product_has_negative_days_until_due(self):
        results = forecasting.forecast_products([_make_stats()], horizon_days=7)
        assert results[0]["days_until_due"] < 0

    def test_product_with_no_interval_excluded(self):
        stats = _make_stats(avg_interval_days=None, order_count=1)
        assert forecasting.forecast_products([stats], horizon_days=7) == []

    def test_multiple_products_mixed_urgency(self):
        overdue = _make_stats("p_overdue", avg_interval_days=7.0, last_ordered_at_days_ago=9.0)
        not_due = _make_stats("p_not", avg_interval_days=28.0, last_ordered_at_days_ago=8.0)
        ids = [r["product_id"] for r in forecasting.forecast_products([overdue, not_due], horizon_days=7)]
        assert "p_overdue" in ids
        assert "p_not" not in ids


class TestGetReorderDue:
    def test_empty_input_returns_empty(self):
        assert forecasting.get_reorder_due([]) == []

    def test_overdue_product_included(self):
        result = forecasting.get_reorder_due([_make_stats()])
        assert len(result) == 1
        assert result[0]["urgency_score"] >= 1.0

    def test_not_overdue_product_excluded(self):
        stats = _make_stats(avg_interval_days=14.0, last_ordered_at_days_ago=8.0)
        assert forecasting.get_reorder_due([stats]) == []

    def test_sorted_by_urgency_descending(self):
        very = _make_stats("p_a", avg_interval_days=7.0, last_ordered_at_days_ago=15.0)
        barely = _make_stats("p_b", avg_interval_days=7.0, last_ordered_at_days_ago=8.0)
        assert forecasting.get_reorder_due([barely, very])[0]["product_id"] == "p_a"

    def test_product_without_interval_excluded(self):
        stats = _make_stats(avg_interval_days=None, order_count=1)
        assert forecasting.get_reorder_due([stats]) == []

    def test_exactly_at_threshold_is_included(self):
        stats = _make_stats(avg_interval_days=7.0, last_ordered_at_days_ago=7.0)
        assert len(forecasting.get_reorder_due([stats])) == 1


class TestFullPipelineWithSeededDB:
    """End-to-end: seeded DB → get_all_product_stats → forecasting engine."""

    async def test_weekly_items_are_overdue(self, seeded_db_session):
        overdue = forecasting.get_reorder_due(await get_all_product_stats(seeded_db_session))
        ids = {r["product_id"] for r in overdue}
        assert {"p_milk_whole", "p_bread_whole", "p_bananas"} <= ids

    async def test_monthly_items_not_overdue(self, seeded_db_session):
        overdue = forecasting.get_reorder_due(await get_all_product_stats(seeded_db_session))
        ids = {r["product_id"] for r in overdue}
        assert "p_olive_oil" not in ids
        assert "p_rice" not in ids

    async def test_biweekly_items_in_14_day_forecast(self, seeded_db_session):
        forecast = forecasting.forecast_products(
            await get_all_product_stats(seeded_db_session), horizon_days=14
        )
        ids = {r["product_id"] for r in forecast}
        assert "p_coffee" in ids
        assert "p_butter" in ids

    async def test_one_time_items_excluded_from_stats(self, seeded_db_session):
        ids = {s["product_id"] for s in await get_all_product_stats(seeded_db_session)}
        assert "p_tomato_sauce" not in ids

    async def test_forecast_results_sorted_by_urgency(self, seeded_db_session):
        forecast = forecasting.forecast_products(
            await get_all_product_stats(seeded_db_session), horizon_days=7
        )
        scores = [f["urgency_score"] for f in forecast]
        assert scores == sorted(scores, reverse=True)

    async def test_weekly_urgency_is_about_1_14(self, seeded_db_session):
        stats = {s["product_id"]: s for s in await get_all_product_stats(seeded_db_session)}
        milk = forecasting.compute_urgency(stats["p_milk_whole"])
        assert 1.0 <= milk["urgency_score"] <= 1.3

    async def test_monthly_urgency_is_well_below_1(self, seeded_db_session):
        stats = {s["product_id"]: s for s in await get_all_product_stats(seeded_db_session)}
        oil = forecasting.compute_urgency(stats["p_olive_oil"])
        assert oil["urgency_score"] < 0.5

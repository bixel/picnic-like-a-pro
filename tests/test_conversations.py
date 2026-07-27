"""Conversation scenario tests – simulated recurring usage over 6 months.

Each scenario replays a realistic sequence of MCP tool calls against the seeded
history and verifies the full pipeline end to end:

    seed_database → db/queries → forecasting/engine → mcp_server tools

Scenario definitions live in tests/fixtures/conversations.py.
"""

from __future__ import annotations

import pytest

from picnic_meal_planner.mcp_server import (
    add_to_cart,
    clear_cart,
    forecast_order,
    get_cart,
    get_categories,
    get_delivery_slots,
    get_frequently_ordered,
    get_order_history,
    get_product_order_stats,
    get_reorder_due,
    record_order,
    remove_from_cart,
    search_products,
)
from tests.fixtures.conversations import (
    ALL_SCENARIOS,
    SCENARIO_FORECAST,
    SCENARIO_RECORD_AND_VERIFY,
    SCENARIO_STATS,
    SCENARIO_WEEKLY_SHOP,
)

_TOOL_MAP = {
    "add_to_cart": add_to_cart,
    "clear_cart": clear_cart,
    "forecast_order": forecast_order,
    "get_cart": get_cart,
    "get_categories": get_categories,
    "get_delivery_slots": get_delivery_slots,
    "get_frequently_ordered": get_frequently_ordered,
    "get_order_history": get_order_history,
    "get_product_order_stats": get_product_order_stats,
    "get_reorder_due": get_reorder_due,
    "record_order": record_order,
    "remove_from_cart": remove_from_cart,
    "search_products": search_products,
}


async def _run_scenario(scenario):
    """Execute every step of *scenario*, checking each assertion in turn."""
    for step in scenario.steps:
        result = await _TOOL_MAP[step.tool](**step.kwargs)
        for index, assertion in enumerate(step.assertions):
            assert assertion(result), (
                f"Scenario {scenario.name!r}, step {step.description!r}, "
                f"assertion #{index} failed. Result: {result!r}"
            )


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
async def test_scenario_runs_end_to_end(scenario, seeded_db_path):
    await _run_scenario(scenario)


class TestWeeklyShopScenario:
    async def test_get_reorder_due_identifies_all_staples(self, seeded_db_path):
        ids = {r["product_id"] for r in await get_reorder_due()}
        for pid in ("p_milk_whole", "p_bread_whole", "p_bananas", "p_yogurt_nat", "p_eggs"):
            assert pid in ids, f"Expected {pid} to be overdue"

    async def test_weekly_urgency_scores_above_1(self, seeded_db_path):
        overdue = await get_reorder_due()
        weekly = [r for r in overdue if r["product_id"] in ("p_milk_whole", "p_bananas")]
        assert weekly
        assert all(r["urgency_score"] >= 1.0 for r in weekly)

    async def test_search_finds_milk(self, seeded_db_path):
        assert any(r["id"] == "p_milk_whole" for r in await search_products("melk", limit=5))

    async def test_add_overdue_items_to_cart(self, seeded_db_path):
        await add_to_cart("p_milk_whole", quantity=2)
        await add_to_cart("p_bread_whole", quantity=1)
        await add_to_cart("p_bananas", quantity=1)
        cart = await get_cart()
        assert cart["total_count"] == 4
        assert cart["total_price"] > 0

    async def test_remove_item_from_cart_mid_session(self, seeded_db_path):
        await add_to_cart("p_milk_whole", quantity=2)
        await add_to_cart("p_chips", quantity=1)
        await remove_from_cart("p_chips")
        assert {i["id"] for i in (await get_cart())["items"]} == {"p_milk_whole"}

    async def test_delivery_slots_available(self, seeded_db_path):
        assert len(await get_delivery_slots()) >= 7


class TestForecastScenario:
    async def test_7day_forecast_contains_weekly_items(self, seeded_db_path):
        ids = {r["product_id"] for r in await forecast_order(horizon_days=7)}
        assert {"p_milk_whole", "p_bread_whole"} <= ids

    async def test_7day_forecast_excludes_monthly_items(self, seeded_db_path):
        ids = {r["product_id"] for r in await forecast_order(horizon_days=7)}
        assert "p_olive_oil" not in ids
        assert "p_rice" not in ids

    async def test_14day_forecast_is_superset_of_7day(self, seeded_db_path):
        seven = {r["product_id"] for r in await forecast_order(horizon_days=7)}
        fourteen = {r["product_id"] for r in await forecast_order(horizon_days=14)}
        assert seven <= fourteen

    async def test_14day_forecast_adds_biweekly_items(self, seeded_db_path):
        fourteen = {r["product_id"] for r in await forecast_order(horizon_days=14)}
        assert "p_coffee" in fourteen
        assert "p_butter" in fourteen

    async def test_forecast_is_sorted_by_urgency(self, seeded_db_path):
        scores = [r["urgency_score"] for r in await forecast_order(horizon_days=14)]
        assert scores == sorted(scores, reverse=True)

    async def test_order_history_last_30_days(self, seeded_db_path):
        assert 3 <= len(await get_order_history(limit=50, days_back=30)) <= 5

    async def test_forecast_results_carry_full_stats(self, seeded_db_path):
        for r in await forecast_order(horizon_days=7):
            assert {"days_until_due", "urgency_score", "avg_interval_days"} <= r.keys()


class TestStatsScenario:
    async def test_milk_stats_weekly_pattern(self, seeded_db_path):
        stats = await get_product_order_stats("p_milk_whole")
        assert stats["order_count"] == 25
        assert stats["avg_quantity"] == 2.0
        assert 6.5 <= stats["avg_interval_days"] <= 7.5

    async def test_coffee_stats_biweekly_pattern(self, seeded_db_path):
        stats = await get_product_order_stats("p_coffee")
        assert stats["order_count"] == 13
        assert 13.0 <= stats["avg_interval_days"] <= 15.0

    async def test_spaghetti_stats_monthly_pattern(self, seeded_db_path):
        stats = await get_product_order_stats("p_spaghetti")
        assert stats["order_count"] == 7
        assert 26.0 <= stats["avg_interval_days"] <= 30.0

    async def test_salmon_stats_occasional_pattern(self, seeded_db_path):
        stats = await get_product_order_stats("p_salmon")
        assert stats["order_count"] == 3
        assert 80.0 <= stats["avg_interval_days"] <= 90.0

    async def test_tomato_sauce_single_order_no_interval(self, seeded_db_path):
        stats = await get_product_order_stats("p_tomato_sauce")
        assert stats["order_count"] == 1
        assert stats["avg_interval_days"] is None

    async def test_never_ordered_product_returns_none(self, seeded_db_path):
        assert await get_product_order_stats("p_quark") is None

    async def test_frequently_ordered_ranks_by_frequency_band(self, seeded_db_path):
        counts = {r["product_id"]: r["order_count"] for r in await get_frequently_ordered(limit=20)}
        assert counts["p_milk_whole"] > counts["p_coffee"] > counts["p_spaghetti"]

    async def test_frequently_ordered_top_is_a_weekly_staple(self, seeded_db_path):
        result = await get_frequently_ordered(limit=20)
        assert result[0]["order_count"] == 25


class TestRecordAndVerifyScenario:
    async def test_recorded_order_appears_in_history(self, seeded_db_path):
        await record_order(
            items=[
                {"product_id": "p_milk_whole", "quantity": 2, "unit_price": 119},
                {"product_id": "p_eggs", "quantity": 1, "unit_price": 349},
            ],
            picnic_order_id="verify_test_001",
        )
        history = await get_order_history(limit=5)
        assert "verify_test_001" in [o["picnic_order_id"] for o in history]

    async def test_recorded_order_increases_order_count(self, seeded_db_path):
        before = (await get_product_order_stats("p_milk_whole"))["order_count"]
        await record_order(
            items=[{"product_id": "p_milk_whole", "quantity": 2, "unit_price": 119}],
            picnic_order_id="count_check_001",
        )
        assert (await get_product_order_stats("p_milk_whole"))["order_count"] == before + 1

    async def test_recorded_order_advances_last_ordered_at(self, seeded_db_path):
        before = (await get_product_order_stats("p_milk_whole"))["last_ordered_at"]
        await record_order(items=[{"product_id": "p_milk_whole", "quantity": 1, "unit_price": 119}])
        assert (await get_product_order_stats("p_milk_whole"))["last_ordered_at"] > before

    async def test_recording_resets_urgency_below_overdue(self, seeded_db_path):
        """After reordering, the item should no longer be flagged as overdue."""
        assert "p_milk_whole" in {r["product_id"] for r in await get_reorder_due()}
        await record_order(items=[{"product_id": "p_milk_whole", "quantity": 2, "unit_price": 119}])
        assert "p_milk_whole" not in {r["product_id"] for r in await get_reorder_due()}

    async def test_multiple_recorded_orders_accumulate(self, seeded_db_path):
        for i in range(3):
            await record_order(
                items=[{"product_id": "p_eggs", "quantity": 1, "unit_price": 349}],
                picnic_order_id=f"accumulate_{i}",
            )
        assert (await get_product_order_stats("p_eggs"))["order_count"] == 28

    async def test_duplicate_picnic_order_id_is_idempotent(self, seeded_db_path):
        """Re-recording the same Picnic order must not create a second order row."""
        before = len(await get_order_history(limit=100))
        for _ in range(2):
            await record_order(
                items=[{"product_id": "p_eggs", "quantity": 1, "unit_price": 349}],
                picnic_order_id="idempotent_001",
            )
        assert len(await get_order_history(limit=100)) == before + 1


class TestCrossScenarioConsistency:
    async def test_reorder_due_is_subset_of_wide_forecast(self, seeded_db_path):
        overdue = {r["product_id"] for r in await get_reorder_due()}
        wide = {r["product_id"] for r in await forecast_order(horizon_days=365)}
        assert overdue <= wide

    async def test_frequently_ordered_matches_per_product_stats(self, seeded_db_path):
        for item in await get_frequently_ordered(limit=5):
            stats = await get_product_order_stats(item["product_id"])
            assert stats["order_count"] == item["order_count"]

    async def test_history_contains_all_seeded_orders(self, seeded_db_path):
        assert len(await get_order_history(limit=100)) == 25

    async def test_cart_operations_do_not_affect_order_stats(self, seeded_db_path):
        before = (await get_product_order_stats("p_milk_whole"))["order_count"]
        await add_to_cart("p_milk_whole", quantity=10)
        await clear_cart(confirm=True)
        assert (await get_product_order_stats("p_milk_whole"))["order_count"] == before

    async def test_search_does_not_create_orders(self, seeded_db_path):
        before = len(await get_order_history(limit=100))
        await search_products("kaas")
        assert len(await get_order_history(limit=100)) == before

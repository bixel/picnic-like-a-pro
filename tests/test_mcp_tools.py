"""Integration tests for all MCP tools exposed in mcp_server.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from picnic_meal_planner.mcp_server import (
    add_to_cart,
    call_tool,
    clear_cart,
    forecast_order,
    get_cart,
    get_categories,
    get_delivery_slots,
    get_frequently_ordered,
    get_order_history,
    get_product_order_stats,
    get_reorder_due,
    get_tool_schemas,
    record_order,
    remove_from_cart,
    search_products,
)


class TestSearchProductsTool:
    async def test_returns_list(self, db_path):
        assert len(await search_products("melk")) > 0

    async def test_results_cached_in_db(self, db_path):
        from picnic_meal_planner.db.queries import get_db, get_product

        await search_products("melk")
        async with get_db() as conn:
            assert await get_product(conn, "p_milk_whole") is not None

    async def test_limit_respected(self, db_path):
        assert len(await search_products("a", limit=2)) <= 2

    async def test_no_match_returns_empty(self, db_path):
        assert await search_products("xyzzy_no_match") == []

    async def test_result_fields(self, db_path):
        for r in await search_products("brood"):
            assert "id" in r
            assert "name" in r


class TestGetCategoriesTool:
    async def test_returns_categories(self):
        assert len(await get_categories()) >= 5

    async def test_categories_have_name(self):
        for cat in await get_categories():
            assert "name" in cat


class TestGetCartTool:
    async def test_empty_cart(self, db_path):
        assert (await get_cart())["items"] == []


class TestAddToCartTool:
    async def test_adds_item_to_cart(self, db_path):
        assert (await add_to_cart("p_milk_whole", quantity=2))["total_count"] == 2

    async def test_accumulates_across_calls(self, db_path):
        await add_to_cart("p_milk_whole", quantity=1)
        assert (await add_to_cart("p_bread_whole", quantity=1))["total_count"] == 2

    async def test_unknown_product_returns_error(self, db_path):
        assert "error" in await add_to_cart("p_nonexistent")


class TestRemoveFromCartTool:
    async def test_removes_item(self, db_path):
        await add_to_cart("p_milk_whole", quantity=1)
        assert (await remove_from_cart("p_milk_whole"))["total_count"] == 0


class TestClearCartTool:
    async def test_without_confirm_returns_warning_and_keeps_cart(self, db_path):
        await add_to_cart("p_milk_whole", quantity=1)
        assert "warning" in await clear_cart(confirm=False)
        assert (await get_cart())["total_count"] == 1

    async def test_default_is_not_confirmed(self, db_path):
        await add_to_cart("p_milk_whole", quantity=1)
        assert "warning" in await clear_cart()
        assert (await get_cart())["total_count"] == 1

    async def test_with_confirm_clears_cart(self, db_path):
        await add_to_cart("p_milk_whole", quantity=1)
        await add_to_cart("p_bread_whole", quantity=1)
        assert "status" in await clear_cart(confirm=True)
        assert (await get_cart())["total_count"] == 0

    async def test_clear_empty_cart_with_confirm(self, db_path):
        assert "status" in await clear_cart(confirm=True)


class TestGetDeliverySlotsTool:
    async def test_returns_list_of_slots(self):
        assert len(await get_delivery_slots()) >= 4

    async def test_slots_have_required_fields(self):
        for slot in await get_delivery_slots():
            assert "window_start" in slot
            assert "window_end" in slot


class TestGetOrderHistoryTool:
    async def test_empty_db_returns_empty(self, db_path):
        assert await get_order_history(limit=10) == []

    async def test_returns_orders_with_items(self, seeded_db_path):
        history = await get_order_history(limit=5)
        assert len(history) == 5
        for order in history:
            assert len(order["items"]) > 0

    async def test_limit_respected(self, seeded_db_path):
        assert len(await get_order_history(limit=3)) == 3

    async def test_days_back_narrows_results(self, seeded_db_path):
        recent = await get_order_history(limit=50, days_back=10)
        assert len(recent) < len(await get_order_history(limit=50))


class TestGetProductOrderStatsTool:
    async def test_returns_none_for_unknown(self, db_path):
        assert await get_product_order_stats("p_unknown") is None

    async def test_returns_stats_for_seeded_product(self, seeded_db_path):
        assert (await get_product_order_stats("p_milk_whole"))["order_count"] == 25

    async def test_weekly_interval_is_about_7_days(self, seeded_db_path):
        stats = await get_product_order_stats("p_milk_whole")
        assert 6.5 <= stats["avg_interval_days"] <= 7.5


class TestGetFrequentlyOrderedTool:
    async def test_empty_db_returns_empty(self, db_path):
        assert await get_frequently_ordered(limit=10) == []

    async def test_sorted_by_frequency(self, seeded_db_path):
        counts = [r["order_count"] for r in await get_frequently_ordered(limit=10)]
        assert counts == sorted(counts, reverse=True)

    async def test_limit_respected(self, seeded_db_path):
        assert len(await get_frequently_ordered(limit=3)) <= 3


class TestRecordOrderTool:
    async def test_record_basic_order(self, db_path):
        await search_products("melk")   # cache product so the FK resolves
        result = await record_order(
            items=[{"product_id": "p_milk_whole", "quantity": 2, "unit_price": 119}]
        )
        assert result["item_count"] == 1
        assert result["order_id"] > 0

    async def test_record_order_with_picnic_id(self, db_path):
        await search_products("brood")
        result = await record_order(
            items=[{"product_id": "p_bread_whole", "quantity": 1, "unit_price": 199}],
            picnic_order_id="picnic_test_001",
        )
        assert result["order_id"] > 0

    async def test_record_order_with_notes(self, db_path):
        await search_products("melk")
        result = await record_order(
            items=[{"product_id": "p_milk_whole", "quantity": 1, "unit_price": 119}],
            notes="Test note",
        )
        assert result["ordered_at"] is not None

    async def test_order_appears_in_history(self, db_path):
        await search_products("melk")
        await record_order(
            items=[{"product_id": "p_milk_whole", "quantity": 1, "unit_price": 119}],
            picnic_order_id="history_check_001",
        )
        history = await get_order_history(limit=5)
        assert any(o["picnic_order_id"] == "history_check_001" for o in history)

    async def test_empty_items_list_is_recorded(self, db_path):
        assert (await record_order(items=[]))["item_count"] == 0

    async def test_quantity_defaults_to_one(self, db_path):
        await search_products("melk")
        await record_order(items=[{"product_id": "p_milk_whole", "unit_price": 119}])
        history = await get_order_history(limit=1)
        assert history[0]["items"][0]["quantity"] == 1


class TestForecastOrderTool:
    async def test_empty_db_returns_empty(self, db_path):
        assert await forecast_order(horizon_days=7) == []

    async def test_weekly_items_in_7_day_forecast(self, seeded_db_path):
        ids = {r["product_id"] for r in await forecast_order(horizon_days=7)}
        assert "p_milk_whole" in ids

    async def test_monthly_items_not_in_7_day_forecast(self, seeded_db_path):
        ids = {r["product_id"] for r in await forecast_order(horizon_days=7)}
        assert "p_olive_oil" not in ids

    async def test_biweekly_in_14_day_forecast(self, seeded_db_path):
        ids = {r["product_id"] for r in await forecast_order(horizon_days=14)}
        assert "p_coffee" in ids

    async def test_results_sorted_by_urgency(self, seeded_db_path):
        scores = [r["urgency_score"] for r in await forecast_order(horizon_days=7)]
        assert scores == sorted(scores, reverse=True)

    async def test_result_has_days_until_due(self, seeded_db_path):
        for r in await forecast_order(horizon_days=7):
            assert "days_until_due" in r


class TestGetReorderDueTool:
    async def test_empty_db_returns_empty(self, db_path):
        assert await get_reorder_due() == []

    async def test_weekly_items_are_overdue(self, seeded_db_path):
        ids = {r["product_id"] for r in await get_reorder_due()}
        assert {"p_milk_whole", "p_bread_whole"} <= ids

    async def test_all_results_have_urgency_ge_1(self, seeded_db_path):
        assert all(r["urgency_score"] >= 1.0 for r in await get_reorder_due())

    async def test_monthly_items_not_overdue(self, seeded_db_path):
        ids = {r["product_id"] for r in await get_reorder_due()}
        assert "p_olive_oil" not in ids


class TestGetToolSchemas:
    def test_returns_list(self):
        assert isinstance(get_tool_schemas(), list)

    def test_has_all_expected_tools(self):
        names = {s["name"] for s in get_tool_schemas()}
        assert {
            "search_products", "get_categories", "get_cart", "add_to_cart",
            "remove_from_cart", "clear_cart", "get_delivery_slots",
            "get_order_history", "get_product_order_stats", "get_frequently_ordered",
            "record_order", "forecast_order", "get_reorder_due",
        } <= names

    def test_each_schema_has_required_fields(self):
        for schema in get_tool_schemas():
            assert {"name", "description", "input_schema"} <= schema.keys()

    def test_input_schemas_are_objects(self):
        for schema in get_tool_schemas():
            assert isinstance(schema["input_schema"], dict)

    def test_schemas_count(self):
        assert len(get_tool_schemas()) >= 13


class TestCallTool:
    async def test_dispatch_known_tool(self):
        assert isinstance(await call_tool("get_categories", {}), list)

    async def test_dispatch_unknown_tool_returns_error(self):
        assert "error" in await call_tool("nonexistent_tool", {})

    async def test_dispatch_tool_with_kwargs(self, db_path):
        assert isinstance(await call_tool("search_products", {"query": "melk", "limit": 3}), list)

    async def test_dispatch_clear_cart_without_confirm(self):
        assert "warning" in await call_tool("clear_cart", {"confirm": False})

    async def test_tool_exception_is_returned_as_error(self, monkeypatch):
        """A raising tool must not propagate — call_tool converts it to an error dict."""
        from picnic_meal_planner import mcp_server

        async def boom(**kwargs):
            raise RuntimeError("kaboom")

        tool = mcp_server.mcp._tool_manager._tools["get_categories"]
        monkeypatch.setattr(tool, "fn", boom)
        result = await call_tool("get_categories", {})
        assert "kaboom" in result["error"]


class TestServerEntryPoint:
    def test_defaults_to_stdio(self, monkeypatch):
        from picnic_meal_planner import mcp_server

        monkeypatch.delenv("MCP_TRANSPORT", raising=False)
        run = MagicMock()
        monkeypatch.setattr(mcp_server.mcp, "run", run)
        mcp_server.main()
        run.assert_called_once_with(transport="stdio")

    @pytest.mark.parametrize("transport", ["sse", "http"])
    def test_network_transports_pass_host_and_port(self, monkeypatch, transport):
        from picnic_meal_planner import mcp_server

        monkeypatch.setenv("MCP_TRANSPORT", transport)
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_PORT", "9001")
        run = MagicMock()
        monkeypatch.setattr(mcp_server.mcp, "run", run)
        mcp_server.main()
        run.assert_called_once_with(transport=transport, host="0.0.0.0", port=9001)

    def test_unknown_transport_raises(self, monkeypatch):
        from picnic_meal_planner import mcp_server

        monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
        with pytest.raises(ValueError, match="Unknown MCP_TRANSPORT"):
            mcp_server.main()

"""Unit tests for the MockPicnicAPI (picnic/mock_client.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from picnic_meal_planner.picnic.mock_client import (
    CATALOG,
    MockPicnicAPI,
    generate_deliveries,
    get_mock_client,
    reset_mock_client,
)


@pytest.fixture
def api():
    """Fresh in-memory MockPicnicAPI with an empty cart."""
    return MockPicnicAPI()


class TestSearch:
    def test_returns_list_of_categories(self, api):
        results = api.search("melk")
        assert isinstance(results, list)
        assert len(results) > 0

    def test_search_result_category_has_items(self, api):
        results = api.search("melk")
        assert len(results[0]["items"]) > 0

    def test_items_contain_required_fields(self, api):
        for item in api.search("melk")[0]["items"]:
            assert {"id", "name", "price"} <= item.keys()

    def test_search_is_case_insensitive(self, api):
        assert len(api.search("melk")[0]["items"]) == len(api.search("MELK")[0]["items"])

    def test_search_partial_match(self, api):
        ids = [it["id"] for it in api.search("brood")[0]["items"]]
        assert "p_bread_white" in ids
        assert "p_bread_whole" in ids

    def test_empty_query_returns_empty(self, api):
        assert api.search("") == []

    def test_whitespace_query_returns_empty(self, api):
        assert api.search("   ") == []

    def test_no_match_returns_empty(self, api):
        assert api.search("xyzzy_no_match_at_all") == []

    def test_catalog_has_enough_products(self):
        assert len(CATALOG) >= 40

    def test_catalog_ids_are_unique(self):
        ids = [p["id"] for p in CATALOG]
        assert len(ids) == len(set(ids))


class TestGetCategories:
    def test_returns_list(self, api):
        assert len(api.get_categories()) >= 5

    def test_categories_have_required_fields(self, api):
        for cat in api.get_categories():
            assert "id" in cat
            assert "name" in cat


class TestCart:
    def test_empty_cart_on_start(self, api):
        cart = api.get_cart()
        assert cart["items"] == []
        assert cart["total_price"] == 0
        assert cart["total_count"] == 0

    def test_add_product(self, api):
        api.add_product("p_milk_whole", count=2)
        cart = api.get_cart()
        assert len(cart["items"]) == 1
        assert cart["total_count"] == 2

    def test_add_same_product_twice_accumulates(self, api):
        api.add_product("p_milk_whole", count=1)
        api.add_product("p_milk_whole", count=1)
        cart = api.get_cart()
        assert len(cart["items"]) == 1
        assert cart["items"][0]["quantity"] == 2

    def test_add_multiple_products(self, api):
        api.add_product("p_milk_whole", count=1)
        api.add_product("p_bread_whole", count=2)
        assert api.get_cart()["total_count"] == 3

    def test_total_price_is_correct(self, api):
        api.add_product("p_milk_whole", count=2)  # 119 cents each
        assert api.get_cart()["total_price"] == 238

    def test_total_price_across_products(self, api):
        api.add_product("p_milk_whole", count=2)   # 238
        api.add_product("p_bread_whole", count=1)  # 199
        assert api.get_cart()["total_price"] == 437

    def test_remove_product(self, api):
        api.add_product("p_milk_whole", count=1)
        api.add_product("p_bread_whole", count=1)
        api.remove_product("p_milk_whole")
        cart = api.get_cart()
        assert len(cart["items"]) == 1
        assert cart["items"][0]["id"] == "p_bread_whole"

    def test_remove_nonexistent_product_is_noop(self, api):
        api.add_product("p_milk_whole", count=1)
        api.remove_product("p_nonexistent")
        assert api.get_cart()["total_count"] == 1

    def test_add_unknown_product_returns_error(self, api):
        result = api.add_product("p_does_not_exist", count=1)
        assert "error" in result
        assert api.get_cart()["total_count"] == 0

    def test_cart_items_have_required_fields(self, api):
        api.add_product("p_milk_whole", count=1)
        assert {"id", "name", "price", "quantity"} <= api.get_cart()["items"][0].keys()


class TestCartPersistence:
    def test_cart_persists_across_instances(self, tmp_path):
        cart_path = tmp_path / "mock_cart.json"
        MockPicnicAPI(persist_cart_path=cart_path).add_product("p_milk_whole", count=3)
        assert cart_path.exists()

        cart = MockPicnicAPI(persist_cart_path=cart_path).get_cart()
        assert cart["total_count"] == 3
        assert cart["items"][0]["id"] == "p_milk_whole"

    def test_removal_is_persisted(self, tmp_path):
        cart_path = tmp_path / "mock_cart.json"
        api = MockPicnicAPI(persist_cart_path=cart_path)
        api.add_product("p_milk_whole", count=1)
        api.remove_product("p_milk_whole")
        assert MockPicnicAPI(persist_cart_path=cart_path).get_cart()["total_count"] == 0

    def test_corrupted_cart_file_starts_empty(self, tmp_path):
        cart_path = tmp_path / "mock_cart.json"
        cart_path.write_text("NOT JSON{{{")
        assert MockPicnicAPI(persist_cart_path=cart_path).get_cart()["items"] == []

    def test_no_persist_path_does_not_write_files(self, api, tmp_path):
        api.add_product("p_milk_whole", count=1)
        # tmp_path also holds the isolated test database, so assert specifically
        # that no cart file was written rather than that the directory is empty.
        assert not list(tmp_path.glob("*cart*.json"))


class TestDeliverySlots:
    def test_returns_dict_with_slots_key(self, api):
        assert "delivery_slots" in api.get_delivery_slots()

    def test_has_multiple_slots(self, api):
        assert len(api.get_delivery_slots()["delivery_slots"]) >= 4

    def test_slots_have_required_fields(self, api):
        for slot in api.get_delivery_slots()["delivery_slots"]:
            assert {"slot_id", "window_start", "window_end", "is_available"} <= slot.keys()

    def test_slots_are_in_the_future(self, api):
        now = datetime.now(timezone.utc).isoformat()
        for slot in api.get_delivery_slots()["delivery_slots"]:
            assert slot["window_start"] > now

    def test_slot_ids_are_unique(self, api):
        ids = [s["slot_id"] for s in api.get_delivery_slots()["delivery_slots"]]
        assert len(ids) == len(set(ids))


class TestDeliveries:
    def test_returns_25_deliveries(self, api):
        assert len(api.get_deliveries()) == 25

    def test_each_delivery_has_id(self, api):
        for d in api.get_deliveries():
            assert "id" in d

    def test_delivery_ids_are_unique(self, api):
        ids = [d["id"] for d in api.get_deliveries()]
        assert len(ids) == len(set(ids))

    def test_get_delivery_returns_details(self, api):
        first_id = api.get_deliveries()[0]["id"]
        detail = api.get_delivery(first_id)
        assert detail["id"] == first_id
        assert "orders" in detail

    def test_get_delivery_items_non_empty(self, api):
        last = api.get_deliveries()[-1]
        assert len(api.get_delivery(last["id"])["orders"][0]["items"]) > 0

    def test_delivery_items_have_required_fields(self, api):
        last = api.get_deliveries()[-1]
        for item in api.get_delivery(last["id"])["orders"][0]["items"]:
            assert {"id", "name", "price", "quantity"} <= item.keys()

    def test_get_delivery_unknown_id_returns_empty(self, api):
        assert api.get_delivery("nonexistent_id") == {}

    def test_latest_delivery_contains_weekly_items(self, api):
        last_id = api.get_deliveries()[-1]["id"]
        item_ids = {it["id"] for it in api.get_delivery(last_id)["orders"][0]["items"]}
        assert "p_milk_whole" in item_ids
        assert "p_bread_whole" in item_ids

    def test_deliveries_ordered_oldest_first(self, api):
        times = [d["creation_time"] for d in api.get_deliveries()]
        assert times == sorted(times)


class TestGenerateDeliveries:
    def test_returns_25_entries(self):
        assert len(generate_deliveries()) == 25

    def test_each_entry_has_summary_and_details(self):
        for d in generate_deliveries():
            assert "summary" in d
            assert "details" in d

    def test_dates_are_in_the_past(self):
        now = datetime.now(timezone.utc).isoformat()
        for d in generate_deliveries():
            assert d["summary"]["creation_time"] < now

    def test_accepts_custom_reference_time(self):
        ref = datetime(2025, 1, 1, tzinfo=timezone.utc)
        last = generate_deliveries(reference_time=ref)[-1]
        expected = (ref - timedelta(days=8)).isoformat()
        assert last["summary"]["creation_time"][:10] == expected[:10]

    def test_weekly_spacing_between_orders(self):
        deliveries = generate_deliveries()
        first = datetime.fromisoformat(deliveries[0]["summary"]["creation_time"])
        second = datetime.fromisoformat(deliveries[1]["summary"]["creation_time"])
        assert abs((second - first).days - 7) <= 1

    def test_total_price_matches_items(self):
        detail = generate_deliveries()[-1]["details"]
        expected = sum(i["price"] * i["quantity"] for i in detail["orders"][0]["items"])
        assert detail["total_price"] == expected


class TestSingleton:
    def test_get_mock_client_returns_same_instance(self, monkeypatch):
        reset_mock_client()
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        assert get_mock_client() is get_mock_client()

    def test_reset_mock_client_creates_fresh_instance(self, monkeypatch):
        reset_mock_client()
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        get_mock_client().add_product("p_milk_whole", count=5)
        reset_mock_client()
        assert get_mock_client().get_cart()["total_count"] == 0

    def test_persist_env_var_creates_cart_file(self, monkeypatch, tmp_path):
        reset_mock_client()
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "true")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "picnic.db"))
        get_mock_client().add_product("p_milk_whole", count=1)
        assert (tmp_path / "mock_cart.json").exists()
        reset_mock_client()

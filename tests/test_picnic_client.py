"""Unit tests for picnic/client.py – the async wrappers around the Picnic API.

The autouse ``fresh_picnic_client`` fixture patches the module singleton to a
MockPicnicAPI, so no test here touches the network.
"""

from __future__ import annotations

import pytest

from picnic_meal_planner.picnic import client as picnic


class TestNormaliseProduct:
    def test_minimal_raw(self):
        result = picnic._normalise_product({"id": "p1", "name": "Milk", "price": 119})
        assert result["id"] == "p1"
        assert result["name"] == "Milk"
        assert result["unit_price"] == 119

    def test_price_zero_becomes_none(self):
        assert picnic._normalise_product({"id": "p1", "price": 0})["unit_price"] is None

    def test_missing_price_becomes_none(self):
        assert picnic._normalise_product({"id": "p1"})["unit_price"] is None

    def test_missing_id_defaults_to_empty_string(self):
        assert picnic._normalise_product({})["id"] == ""

    def test_missing_name_defaults_to_empty_string(self):
        assert picnic._normalise_product({})["name"] == ""

    def test_unit_quantity_from_unit_quantity_field(self):
        raw = {"id": "p1", "price": 100, "unit_quantity": "1 liter"}
        assert picnic._normalise_product(raw)["unit_quantity"] == "1 liter"

    def test_unit_quantity_falls_back_to_quantity(self):
        raw = {"id": "p1", "price": 100, "quantity": "500g"}
        assert picnic._normalise_product(raw)["unit_quantity"] == "500g"

    def test_image_id_forwarded(self):
        raw = {"id": "p1", "price": 100, "image_id": "img_abc"}
        assert picnic._normalise_product(raw)["image_id"] == "img_abc"

    def test_category_always_none(self):
        raw = {"id": "p1", "price": 100, "category": "Dairy"}
        assert picnic._normalise_product(raw)["category"] is None

    def test_all_required_keys_present(self):
        result = picnic._normalise_product({"id": "p1", "price": 100})
        assert {"id", "name", "unit_price", "unit_quantity", "image_id", "category"} <= result.keys()


class TestSearchProducts:
    async def test_returns_list(self):
        results = await picnic.search_products("melk")
        assert isinstance(results, list)
        assert len(results) > 0

    async def test_respects_limit(self):
        assert len(await picnic.search_products("a", limit=2)) <= 2

    async def test_limit_of_one(self):
        assert len(await picnic.search_products("melk", limit=1)) == 1

    async def test_results_are_normalised(self):
        for r in await picnic.search_products("melk"):
            assert "unit_price" in r     # normalised key, not raw "price"
            assert "category" in r

    async def test_empty_query_returns_empty(self):
        assert await picnic.search_products("") == []

    async def test_no_match_returns_empty(self):
        assert await picnic.search_products("xyzzy_none") == []


class TestGetCategories:
    async def test_returns_list(self):
        assert len(await picnic.get_categories()) >= 5


class TestGetCart:
    async def test_empty_cart(self):
        cart = await picnic.get_cart()
        assert cart["items"] == []


class TestAddToCart:
    async def test_adds_item(self):
        assert (await picnic.add_to_cart("p_milk_whole", quantity=2))["total_count"] == 2

    async def test_default_quantity_is_one(self):
        assert (await picnic.add_to_cart("p_milk_whole"))["total_count"] == 1

    async def test_unknown_product_returns_error_dict(self):
        assert "error" in await picnic.add_to_cart("p_nonexistent")


class TestRemoveFromCart:
    async def test_removes_existing_item(self):
        await picnic.add_to_cart("p_milk_whole", quantity=1)
        assert (await picnic.remove_from_cart("p_milk_whole"))["total_count"] == 0

    async def test_remove_nonexistent_is_noop(self):
        await picnic.add_to_cart("p_milk_whole", quantity=1)
        assert (await picnic.remove_from_cart("p_quark"))["total_count"] == 1


class TestClearCart:
    async def test_clears_all_items(self):
        await picnic.add_to_cart("p_milk_whole", quantity=2)
        await picnic.add_to_cart("p_bread_whole", quantity=1)
        assert await picnic.clear_cart() == {"status": "cart cleared"}
        assert (await picnic.get_cart())["total_count"] == 0

    async def test_clear_empty_cart_is_noop(self):
        assert await picnic.clear_cart() == {"status": "cart cleared"}


class TestGetDeliverySlots:
    async def test_returns_flat_list(self):
        slots = await picnic.get_delivery_slots()
        assert isinstance(slots, list)
        assert len(slots) >= 4

    async def test_slots_have_window_start(self):
        for slot in await picnic.get_delivery_slots():
            assert "window_start" in slot


class TestGetDeliveries:
    async def test_returns_list_of_25(self):
        assert len(await picnic.get_deliveries()) == 25

    async def test_deliveries_have_ids(self):
        for d in await picnic.get_deliveries():
            assert "id" in d


class TestGetDelivery:
    async def test_returns_details(self):
        deliveries = await picnic.get_deliveries()
        assert "orders" in await picnic.get_delivery(deliveries[0]["id"])

    async def test_unknown_delivery_returns_empty(self):
        assert await picnic.get_delivery("nonexistent") == {}


class TestClientSelection:
    def test_reset_clears_singleton(self, monkeypatch):
        monkeypatch.setattr(picnic, "_client", object())
        picnic.reset_client()
        assert picnic._client is None

    def test_mock_env_var_selects_mock_client(self, monkeypatch):
        from picnic_meal_planner.picnic.mock_client import MockPicnicAPI, reset_mock_client

        reset_mock_client()
        monkeypatch.setattr(picnic, "_client", None)
        monkeypatch.setenv("PICNIC_MOCK", "true")
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        assert isinstance(picnic._get_client(), MockPicnicAPI)
        reset_mock_client()

    def test_client_is_cached_between_calls(self, monkeypatch):
        from picnic_meal_planner.picnic.mock_client import reset_mock_client

        reset_mock_client()
        monkeypatch.setattr(picnic, "_client", None)
        monkeypatch.setenv("PICNIC_MOCK", "true")
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        assert picnic._get_client() is picnic._get_client()
        reset_mock_client()

    def test_real_api_is_constructed_from_env_when_mock_disabled(self, monkeypatch):
        """Without PICNIC_MOCK, credentials are read from the environment."""
        import python_picnic_api2

        constructed = {}

        def fake_api(**kwargs):
            constructed.update(kwargs)
            return "real-client"

        monkeypatch.setattr(python_picnic_api2, "PicnicAPI", fake_api)
        monkeypatch.setattr(picnic, "_client", None)
        monkeypatch.setenv("PICNIC_MOCK", "false")
        monkeypatch.setenv("PICNIC_USERNAME", "user@example.com")
        monkeypatch.setenv("PICNIC_PASSWORD", "secret")
        monkeypatch.setenv("PICNIC_COUNTRY_CODE", "DE")

        assert picnic._get_client() == "real-client"
        assert constructed == {
            "username": "user@example.com",
            "password": "secret",
            "country_code": "DE",
        }

    def test_country_code_defaults_to_nl(self, monkeypatch):
        import python_picnic_api2

        constructed = {}
        monkeypatch.setattr(
            python_picnic_api2, "PicnicAPI", lambda **kw: constructed.update(kw) or "c"
        )
        monkeypatch.setattr(picnic, "_client", None)
        monkeypatch.setenv("PICNIC_MOCK", "false")
        monkeypatch.setenv("PICNIC_USERNAME", "u")
        monkeypatch.setenv("PICNIC_PASSWORD", "p")
        monkeypatch.delenv("PICNIC_COUNTRY_CODE", raising=False)

        picnic._get_client()
        assert constructed["country_code"] == "NL"

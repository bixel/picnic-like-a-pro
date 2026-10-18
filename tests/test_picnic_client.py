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


class TestRealLibraryModels:
    """python-picnic-api2 2.x returns pydantic models; the wrapper must hand
    callers plain dicts, exactly as 1.x did."""

    @pytest.fixture
    def real(self, monkeypatch):
        class Fake:
            pass

        fake = Fake()
        monkeypatch.setattr(picnic, "_client", fake)
        return fake

    def test_plain_prefers_verbatim_raw_payload(self):
        from python_picnic_api2 import Cart

        payload = {"id": "c1", "items": [], "total_count": 0, "unknown_key": 1}
        assert picnic._plain(Cart.from_api(payload)) is payload

    def test_plain_falls_back_to_model_dump(self):
        from python_picnic_api2 import Cart

        assert picnic._plain(Cart(id="c1"))["id"] == "c1"

    def test_plain_passes_dicts_through(self):
        assert picnic._plain([{"a": 1}]) == [{"a": 1}]

    async def test_search_result_model_is_normalised(self, real):
        from python_picnic_api2 import SearchResult, SearchResultItem

        real.search = lambda q: SearchResult(items=[
            SearchResultItem(id="s1", name="Milk", display_price=119, unit_quantity="1 l"),
            SearchResultItem(id="s2", name="Butter"),
        ])
        results = await picnic.search_products("milk", limit=5)
        assert [r["id"] for r in results] == ["s1", "s2"]
        assert results[0]["unit_price"] == 119
        assert results[0]["unit_quantity"] == "1 l"
        assert results[1]["unit_price"] is None

    async def test_cart_models_become_dicts(self, real):
        from python_picnic_api2 import Cart

        payload = {"id": "c1", "items": [{"id": "line1"}]}
        removed = []
        real.get_cart = lambda: Cart.from_api(payload)
        real.add_product = lambda pid, count=1: Cart.from_api(payload)
        real.remove_product = lambda pid: removed.append(pid) or Cart.from_api(payload)

        assert await picnic.get_cart() == payload
        assert await picnic.add_to_cart("p1") == payload
        assert await picnic.remove_from_cart("p1") == payload
        assert await picnic.clear_cart() == {"status": "cart cleared"}
        assert removed == ["p1", "line1"]

    async def test_delivery_models_become_dicts(self, real):
        from python_picnic_api2 import Delivery, DeliverySlots, DeliverySummary

        slots = {"delivery_slots": [{"slot_id": "s1"}]}
        summary = {"delivery_id": "d1", "status": "COMPLETED"}
        detail = {"delivery_id": "d1", "orders": [{"items": []}]}
        real.get_delivery_slots = lambda: DeliverySlots.from_api(slots)
        real.get_deliveries = lambda: [DeliverySummary.from_api(summary)]
        real.get_delivery = lambda did: Delivery.from_api(detail)

        assert await picnic.get_delivery_slots() == [{"slot_id": "s1"}]
        assert await picnic.get_deliveries() == [summary]
        assert await picnic.get_delivery("d1") == detail

    async def test_removed_categories_endpoint_returns_empty(self, real):
        def gone():
            raise NotImplementedError("removed by picnic")

        real.get_categories = gone
        assert await picnic.get_categories() == []


# Trimmed from python-picnic-api2's tests/fixtures/delivery_de.json: the real
# API nests orders -> ORDER_LINE -> ORDER_ARTICLE, with the count in a
# QUANTITY decorator and the paid price on the line.
REAL_DELIVERY = {
    "type": "DELIVERY",
    "delivery_id": "d0000000000",
    "id": "d0000000000",
    "creation_time": "2026-07-14T22:36:46.115+02:00",
    "slot": {"window_start": "2026-07-15T14:15:00.000+02:00"},
    "status": "COMPLETED",
    "orders": [
        {
            "type": "ORDER",
            "id": "000-000-0000",
            "items": [
                {
                    "type": "ORDER_LINE",
                    "id": "2528",
                    "items": [
                        {
                            "type": "ORDER_ARTICLE",
                            "id": "s1021945",
                            "name": "Edeka Herzstücke Brioche Burger Buns",
                            "image_ids": ["img1", "img2"],
                            "unit_quantity": "4 Stück",
                            "price": 189,
                            "decorators": [
                                {"type": "IMMUTABLE"},
                                {"type": "QUANTITY", "quantity": 2},
                            ],
                        }
                    ],
                    "display_price": 338,
                    "price": 338,
                }
            ],
            "total_price": 4996,
        },
        {"type": "ORDER", "id": "000-000-0001", "items": [], "total_price": 1004},
    ],
    "error": {},
}


class TestDeliveryLineItems:
    def test_real_payload_uses_article_not_order_line(self):
        [item] = picnic.delivery_line_items(REAL_DELIVERY)
        assert item["product"]["id"] == "s1021945"       # not the line id "2528"
        assert item["product"]["name"] == "Edeka Herzstücke Brioche Burger Buns"
        assert item["product"]["unit_quantity"] == "4 Stück"
        assert item["product"]["image_id"] == "img1"

    def test_real_payload_quantity_from_decorator(self):
        [item] = picnic.delivery_line_items(REAL_DELIVERY)
        assert item["quantity"] == 2

    def test_real_payload_unit_price_from_line_price(self):
        [item] = picnic.delivery_line_items(REAL_DELIVERY)
        assert item["unit_price"] == 169
        assert item["product"]["unit_price"] == 169

    def test_multi_article_line_falls_back_to_article_price(self):
        line = {"items": [
            {"id": "a", "price": 100, "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            {"id": "b", "price": 250, "decorators": [{"type": "QUANTITY", "quantity": 1}]},
        ], "price": 999}
        items = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert [i["unit_price"] for i in items] == [100, 250]

    def test_article_without_id_is_skipped(self):
        detail = {"orders": [{"items": [{"items": [{"name": "?"}], "price": 5}]}]}
        assert picnic.delivery_line_items(detail) == []

    async def test_mock_payload_flat_articles(self):
        deliveries = await picnic.get_deliveries()
        detail = await picnic.get_delivery(deliveries[0]["id"])
        items = picnic.delivery_line_items(detail)
        assert items
        milk = next(i for i in items if i["product"]["id"] == "p_milk_whole")
        assert milk["quantity"] == 2
        assert milk["unit_price"] == 119
        assert milk["product"]["image_id"] == "img_milk_whole"

    def test_real_payload_survives_model_round_trip(self):
        """What the importer actually gets: a 2.x model run through _plain()."""
        from python_picnic_api2 import Delivery

        detail = picnic._plain(Delivery.from_api(REAL_DELIVERY))
        assert [i["product"]["id"] for i in picnic.delivery_line_items(detail)] == ["s1021945"]

    def test_legacy_wrapper_keeps_count_and_paid_price(self):
        """``{"product": {...}, "count", "unit_price"}``, as the old importer read it."""
        line = {"product": {"id": "p_milk", "price": 189}, "count": 3, "unit_price": 169}
        [item] = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert item["product"]["id"] == "p_milk"
        assert item["quantity"] == 3
        assert item["unit_price"] == 169

    def test_legacy_wrapper_falls_back_to_product_price(self):
        line = {"product": {"id": "p_milk", "price": 189}}
        [item] = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert (item["quantity"], item["unit_price"]) == (1, 189)


class TestDeliverySummaryFields:
    def test_real_payload_sums_order_totals(self):
        fields = picnic.delivery_summary_fields(REAL_DELIVERY)
        assert fields["total_price"] == 6000
        assert fields["ordered_at"] == "2026-07-14T22:36:46.115+02:00"
        assert fields["delivered_at"] is None

    def test_delivery_time_object_uses_start(self):
        detail = {"delivery_time": {"start": "2026-07-15T14:30:00", "end": "2026-07-15T14:40:00"}}
        assert picnic.delivery_summary_fields(detail)["delivered_at"] == "2026-07-15T14:30:00"

    def test_null_slot_does_not_crash(self):
        assert picnic.delivery_summary_fields({"slot": None})["ordered_at"] == ""

    async def test_mock_payload(self):
        deliveries = await picnic.get_deliveries()
        detail = await picnic.get_delivery(deliveries[0]["id"])
        fields = picnic.delivery_summary_fields(detail)
        assert fields["total_price"] == detail["total_price"]
        assert fields["delivered_at"] == detail["delivery_time"]


class TestDeliveryId:
    def test_id_wins_over_delivery_id(self):
        # The order the importer has always keyed checkpoints and orders by.
        assert picnic.delivery_id({"id": "a", "delivery_id": "b", "order_id": "c"}) == "a"

    def test_falls_back_to_delivery_id(self):
        assert picnic.delivery_id({"delivery_id": "b", "order_id": "c"}) == "b"

    def test_falls_back_to_order_id(self):
        assert picnic.delivery_id({"order_id": "c"}) == "c"

    def test_none_without_any_id(self):
        assert picnic.delivery_id({}) is None


class TestDeliveryUnusableReason:
    def test_real_payload_is_usable(self):
        # Carries "error": {} — present but empty, so not an error.
        assert picnic.delivery_unusable_reason(REAL_DELIVERY) is None

    async def test_mock_payload_is_usable(self):
        deliveries = await picnic.get_deliveries()
        detail = await picnic.get_delivery(deliveries[0]["id"])
        assert picnic.delivery_unusable_reason(detail) is None

    @pytest.mark.parametrize("detail", [
        {},
        {"error": {"code": "DELIVERY_NOT_FOUND"}},
        {**REAL_DELIVERY, "creation_time": None, "slot": None},
        {**REAL_DELIVERY, "orders": [{"items": []}]},
    ])
    def test_unusable_payloads(self, detail):
        assert picnic.delivery_unusable_reason(detail)

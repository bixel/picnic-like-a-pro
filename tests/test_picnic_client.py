"""Unit tests for picnic/client.py – the async wrappers around the Picnic API.

The autouse ``fresh_picnic_client`` fixture patches the module singleton to a
MockPicnicAPI, so no test here touches the network.
"""

from __future__ import annotations

import json

import pytest

from picnic_meal_planner.db import queries as db
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

    def test_all_required_keys_present(self):
        result = picnic._normalise_product({"id": "p1", "price": 100})
        assert {"id", "name", "unit_price", "unit_quantity", "image_id"} <= result.keys()


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

    async def test_empty_query_returns_empty(self):
        assert await picnic.search_products("") == []

    async def test_no_match_returns_empty(self):
        assert await picnic.search_products("xyzzy_none") == []


class TestGetCategoryIds:
    async def test_mock_product(self):
        l1_id, l2_id, l3_id = await picnic.get_category_ids("p_milk_whole")
        assert await picnic.get_l3_names(l2_id, l3_id) == {l3_id: "Zuivel, Eieren & Boter"}

    async def test_unknown_product_is_none(self):
        assert await picnic.get_category_ids("p_nonexistent") is None

    async def test_reads_the_ids_from_the_product_page(self, monkeypatch):
        from python_picnic_api2.models import Article

        product_page = {"children": [{"id": "category-button", "pml": {"component": {
            "onPress": {"actionType": "OPEN", "target": "app.picnic://categories/10/l2/20/l3/30"},
        }}}]}

        class Api:
            def get_article(self, article_id):
                return Article(id=article_id, raw=product_page)

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_category_ids("s1") == (10, 20, 30)

    async def test_page_without_category_button_is_none(self, monkeypatch):
        from python_picnic_api2.models import Article

        class Api:
            def get_article(self, article_id):
                return Article(id=article_id, raw={"children": []})

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_category_ids("s1") is None

    async def test_unparseable_page_is_none(self, monkeypatch):
        from python_picnic_api2 import PicnicParseError

        class Api:
            def get_article(self, article_id):
                raise PicnicParseError("no page", endpoint="product-details-page-root")

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_category_ids("s1") is None


class TestGetArticlePrice:
    async def test_mock_product_price(self):
        assert await picnic.get_article_price("p_milk_whole") == 119

    async def test_unknown_product_is_none(self):
        assert await picnic.get_article_price("p_nonexistent") is None

    async def test_bundle_is_skipped(self, monkeypatch):
        # Bundle pages expose only a per-unit price; skip rather than store it.
        class Article:
            is_bundle = True
            price = 250

        class Api:
            def get_article(self, article_id):
                return Article()

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_article_price("s1") is None

    async def test_ordinary_article_returns_price(self, monkeypatch):
        class Article:
            is_bundle = False
            price = 250

        class Api:
            def get_article(self, article_id):
                return Article()

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_article_price("s1") == 250


class TestGetL3Names:
    async def test_names_every_l3_on_the_l2_page(self, monkeypatch):
        l2_page = {"children": [
            {"id": f"vertical-article-tiles-sub-header-{l3}",
             "pml": {"component": {"accessibilityLabel": name}}}
            for l3, name in ((30, "Alkoholfrei"), (31, "Pils"))
        ]}
        paths = []

        class Api:
            def _get(self, path, add_picnic_headers=False):
                paths.append(path)
                return l2_page

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_l3_names(20, 30) == {30: "Alkoholfrei", 31: "Pils"}
        assert paths == ["/pages/L2-category-page-root?category_id=20&l3_category_id=30"]

    async def test_an_l1_page_names_the_l1_and_its_l2s(self, monkeypatch):
        l1_page = {"header": {"type": "STATIC", "title": "Cerealien"}, "children": [
            {"id": f"core-list-item-category-{l2}",
             "pml": {"component": {"accessibilityLabel": name}}}
            for l2, name in ((20, "Honig"), ("CustomCatLvl2Pos2", "Für dich"))
        ]}
        paths = []

        class Api:
            def _get(self, path, add_picnic_headers=False):
                paths.append(path)
                return l1_page

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_l1_names(10) == ("Cerealien", {20: "Honig"})
        assert paths == ["/pages/L1-category-page-root?category_id=10"]

    def test_an_unusable_l1_page_has_no_names(self):
        assert picnic.l1_names_on_page({"error": "nope"}) == (None, {})
        assert picnic.l1_names_on_page([]) == (None, {})

    def test_an_l1_title_that_is_not_text_is_ignored(self):
        page = {"header": {"title": {"text": "Cerealien"}}, "children": []}
        assert picnic.l1_names_on_page(page) == (None, {})

    async def test_a_page_without_the_l3_still_names_the_others(self, monkeypatch):
        class Api:
            def _get(self, path, add_picnic_headers=False):
                return {"children": [{"id": "vertical-article-tiles-sub-header-31",
                                      "pml": {"component": {"accessibilityLabel": "Pils"}}}]}

        monkeypatch.setattr(picnic, "_client", Api())
        assert await picnic.get_l3_names(20, 30) == {31: "Pils"}

    async def test_unknown_mock_page_is_empty(self):
        assert await picnic.get_l3_names(99, 99) == {}


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
    """Mock-mode selection; real-mode login is covered in test_picnic_auth.py."""

    def test_reset_clears_singleton(self, monkeypatch):
        monkeypatch.setattr(picnic, "_client", object())
        picnic.reset_client()
        assert picnic._client is None

    async def test_mock_env_var_selects_mock_client(self, monkeypatch):
        from picnic_meal_planner.picnic.mock_client import MockPicnicAPI, reset_mock_client

        reset_mock_client()
        picnic.reset_client()
        monkeypatch.setenv("PICNIC_MOCK", "true")
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        assert isinstance(await picnic.get_client(), MockPicnicAPI)
        reset_mock_client()

    async def test_client_is_cached_between_calls(self, monkeypatch):
        from picnic_meal_planner.picnic.mock_client import reset_mock_client

        reset_mock_client()
        picnic.reset_client()
        monkeypatch.setenv("PICNIC_MOCK", "true")
        monkeypatch.setenv("PICNIC_MOCK_PERSIST_CART", "false")
        assert await picnic.get_client() is await picnic.get_client()
        reset_mock_client()


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
        assert "unit_price" not in item["product"]

    def test_promo_decorator_wins_over_line_price(self):
        # "jetzt 1.99€": the line's regular price is 299, the paid price 199.
        line = {
            "price": 299,
            "decorators": [{"type": "PRICE", "display_price": 199}],
            "items": [
                {"id": "s1", "price": 432199,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            ],
        }
        [item] = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert item["unit_price"] == 199

    def test_mixed_promo_line_splits_paid_price(self):
        # Three different products grouped by "3 für 5.99€"; each article's
        # ``price`` is the 432199 placeholder and must be ignored.
        line = {
            "price": 747,
            "decorators": [
                {"type": "PRICE", "display_price": 599},
                {"type": "PROMO", "text": "3 für 5.99€"},
            ],
            "items": [
                {"id": "s1024698", "price": 432199,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
                {"id": "s1030505", "price": 432199,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
                {"id": "s1030462", "price": 432199,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            ],
        }
        items = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert [i["unit_price"] for i in items] == [200, 200, 199]
        assert sum(i["unit_price"] * i["quantity"] for i in items) == 599
        for item in items:
            assert item["unit_price"] != 432199
            assert "unit_price" not in item["product"]

    def test_mixed_line_without_promo_splits_line_price(self):
        line = {
            "price": 999,
            "items": [
                {"id": "a", "price": 100, "decorators": [{"type": "QUANTITY", "quantity": 1}]},
                {"id": "b", "price": 250, "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            ],
        }
        items = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert [i["unit_price"] for i in items] == [500, 499]

    def test_nested_line_without_line_price_falls_back_to_article_price(self):
        # No line-level price and no PRICE decorator: keep the article's price.
        line = {
            "items": [
                {"id": "s1", "price": 189,
                 "decorators": [{"type": "QUANTITY", "quantity": 2}]},
            ],
        }
        [item] = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert item["unit_price"] == 189

    def test_nested_line_without_line_price_rejects_placeholder(self):
        # The 432199 placeholder is never stored, even via the fallback.
        line = {
            "items": [
                {"id": "s1", "price": 432199,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            ],
        }
        [item] = picnic.delivery_line_items({"orders": [{"items": [line]}]})
        assert item["unit_price"] is None

    def test_nested_mixed_line_without_line_price_uses_each_article_price(self):
        line = {
            "items": [
                {"id": "a", "price": 100,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
                {"id": "b", "price": 250,
                 "decorators": [{"type": "QUANTITY", "quantity": 1}]},
            ],
        }
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


# ---------------------------------------------------------------------------
# Request logging (api_log)
# ---------------------------------------------------------------------------

async def _rows() -> list[dict]:
    async with db.get_db() as session:
        return await db.list_api_requests(session, provider="picnic")


class TestRequestLogging:
    async def test_successful_call_is_logged_with_its_target(self):
        deliveries = await picnic.get_deliveries()
        delivery_id = deliveries[0]["id"]
        await picnic.get_delivery(delivery_id)

        row = next(r for r in await _rows() if r["operation"] == "get_delivery")
        assert row["target"] == delivery_id
        assert row["status"] == "success"
        assert row["error_type"] is None
        assert json.loads(row["request_body"]) == {
            "args": [delivery_id],
            "kwargs": {},
        }
        assert json.loads(row["response_body"])["id"] == delivery_id

    async def test_search_query_is_not_recorded_as_a_target(self):
        await picnic.search_products("milk")

        row = next(r for r in await _rows() if r["operation"] == "search")
        assert row["target"] is None
        # The query is not an indexed target, but it is in the body.
        assert json.loads(row["request_body"]) == {"args": ["milk"], "kwargs": {}}

    async def test_failed_call_is_logged_and_reraised(self, monkeypatch):
        def boom(_query):
            raise RuntimeError("picnic down")

        monkeypatch.setattr(picnic._client, "search", boom)
        with pytest.raises(RuntimeError, match="picnic down"):
            await picnic.search_products("milk")

        [row] = await _rows()
        assert row["operation"] == "search"
        assert row["status"] == "error"
        assert row["error_type"] == "RuntimeError"
        assert row["error_message"] == "picnic down"
        assert json.loads(row["request_body"]) == {"args": ["milk"], "kwargs": {}}


class TestRateLimits:
    """Telling a rate limit or block apart from a problem with one page."""

    @staticmethod
    def _api(status: int, body: bytes, headers: dict | None = None):
        import requests
        from requests.adapters import BaseAdapter

        from picnic_meal_planner import api_log

        class _Adapter(BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.status_code = status
                response._content = body
                response.headers.update(headers or {})
                response.request = request
                response.url = request.url
                return response

            def close(self):
                pass

        session = requests.Session()
        session.mount("https://", _Adapter())

        class Api:
            # Like python_picnic_api2: the status is ignored, the body parsed.
            def _get(self, path, add_picnic_headers=False):
                return self.session.get("https://storefront.example" + path).json()

        api = Api()
        api.session = session
        api_log.install_picnic_capture(api)
        return api

    async def test_an_html_403_is_a_block(self, monkeypatch):
        monkeypatch.setattr(picnic, "_client", self._api(403, b"<html>Forbidden</html>"))
        with pytest.raises(picnic.PicnicRateLimited) as raised:
            await picnic.get_l1_names(10)
        assert (raised.value.status, raised.value.retry_after) == (403, None)

    async def test_a_429_is_a_rate_limit_even_with_a_json_body(self, monkeypatch):
        api = self._api(429, b'{"error": "slow down"}', {"Retry-After": "12"})
        monkeypatch.setattr(picnic, "_client", api)
        with pytest.raises(picnic.PicnicRateLimited) as raised:
            await picnic.get_l1_names(10)
        assert (raised.value.status, raised.value.retry_after) == (429, 12.0)

    async def test_a_missing_page_is_an_ordinary_error(self, monkeypatch):
        import requests

        monkeypatch.setattr(picnic, "_client", self._api(404, b"<html>Not found</html>"))
        with pytest.raises(requests.exceptions.JSONDecodeError):
            await picnic.get_l1_names(10)

    async def test_a_json_403_is_not_a_block(self, monkeypatch):
        monkeypatch.setattr(picnic, "_client", self._api(403, b'{"error": {"code": "X"}}'))
        assert await picnic.get_l1_names(10) == (None, {})

    def test_retry_after_as_a_date(self):
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
        assert 80 < picnic._retry_after({"retry-after": when}) <= 90
        assert picnic._retry_after({"Retry-After": "soon"}) is None
        assert picnic._retry_after(None) is None

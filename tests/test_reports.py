"""Unit tests for the pure order-history aggregation in ``reporting.py``."""

from __future__ import annotations

from datetime import date

from picnic_meal_planner import reporting


def _product(pid, l3=None, l2=None, l1=None, **names):
    return {
        "id": pid, "name": pid.title(),
        "l3_id": l3, "l3_name": names.get("l3_name"),
        "l2_id": l2, "l2_name": names.get("l2_name"),
        "l1_id": l1, "l1_name": names.get("l1_name"),
    }


PRODUCTS = [
    _product("milk", 30, 20, 10, l3_name="Milch", l2_name="Molkerei", l1_name="Frisch"),
    _product("oat", 30, 20, 10, l3_name="Milch", l2_name="Molkerei", l1_name="Frisch"),
    _product("pils", 31, 21, None, l3_name="Pils"),
    _product("bag"),
]
ORDERS = [
    {"id": 1, "date": "2026-01-05T10:00:00+01:00", "total_price": 1000},
    {"id": 2, "date": "2026-03-20T10:00:00+01:00", "total_price": 500},
    {"id": 3, "date": "", "total_price": 99},
]
ORDER_LINES = [
    {"order_id": 1, "product_id": "milk", "quantity": 2, "unit_price": 100},
    {"order_id": 1, "product_id": "pils", "quantity": 1, "unit_price": 600},
    {"order_id": 2, "product_id": "milk", "quantity": 1, "unit_price": 110},
    {"order_id": 2, "product_id": "oat", "quantity": 1, "unit_price": 200},
    {"order_id": 2, "product_id": "bag", "quantity": 3, "unit_price": None},
    {"order_id": 3, "product_id": "milk", "quantity": 9, "unit_price": 100},
]


def _lines(date_from=None, date_to=None):
    return reporting.build_lines(reporting.order_days(ORDERS, date_from, date_to), ORDER_LINES)


class TestReporting:
    def test_undated_orders_are_left_out(self):
        assert set(reporting.order_days(ORDERS)) == {1, 2}

    def test_date_range_is_inclusive(self):
        days = reporting.order_days(ORDERS, date(2026, 1, 5), date(2026, 1, 5))
        assert days == {1: date(2026, 1, 5)}
        assert reporting.order_days(ORDERS, date(2026, 1, 6), date(2026, 3, 19)) == {}

    def test_hierarchy_totals_add_up(self):
        tree = reporting.hierarchy(_lines(), PRODUCTS)
        assert (tree["level"], tree["id"], tree["name"]) == ("global", "all", None)
        assert (tree["spend"], tree["units"], tree["orders"]) == (1110, 8, 2)
        assert sum(child["spend"] for child in tree["children"]) == tree["spend"]
        # Highest spend first.
        assert [(c["level"], c["id"], c["name"]) for c in tree["children"]] == [
            ("l1", "unknown", None),
            ("l1", "10", "Frisch"),
            ("uncategorised", "none", None),
        ]

    def test_hierarchy_descends_to_products(self):
        frisch = reporting.hierarchy(_lines(), PRODUCTS)["children"][1]
        l2 = frisch["children"][0]
        l3 = l2["children"][0]
        assert (l2["level"], l2["name"], l3["level"], l3["name"]) == ("l2", "Molkerei", "l3", "Milch")
        assert [(p["level"], p["id"], p["spend"], p["orders"]) for p in l3["children"]] == [
            ("product", "milk", 310, 2),
            ("product", "oat", 200, 1),
        ]
        # A parent counts an order once, however many of its products are in it.
        assert l3["orders"] == 2

    def test_uncategorised_products_hang_below_their_own_node(self):
        node = reporting.hierarchy(_lines(), PRODUCTS)["children"][2]
        assert [c["id"] for c in node["children"]] == ["bag"]
        assert node["spend"] == 0 and node["units"] == 3

    def test_hierarchy_only_lists_what_was_ordered(self):
        tree = reporting.hierarchy(_lines(date_to=date(2026, 1, 31)), PRODUCTS)
        assert [c["id"] for c in tree["children"]] == ["unknown", "10"]

    def test_lines_of_unknown_products_are_ignored(self):
        assert reporting.hierarchy(_lines(), [])["spend"] == 0

    def test_select_node(self):
        path, ids = reporting.select_node(PRODUCTS, "l2", "20")
        assert ids == {"milk", "oat"}
        assert path == [
            {"level": "l1", "id": "10", "name": "Frisch"},
            {"level": "l2", "id": "20", "name": "Molkerei"},
        ]
        assert reporting.select_node(PRODUCTS, "global", "all") == ([], {"milk", "oat", "pils", "bag"})
        assert reporting.select_node(PRODUCTS, "uncategorised", "none")[1] == {"bag"}
        assert reporting.select_node(PRODUCTS, "product", "pils")[0][-1]["name"] == "Pils"
        assert reporting.select_node(PRODUCTS, "l3", "999") is None

    def test_monthly_series_fills_gaps(self):
        series = reporting.timeseries(_lines(), "month")
        assert [(b["start"], b["spend"], b["orders"]) for b in series] == [
            ("2026-01-01", 800, 1), ("2026-02-01", 0, 0), ("2026-03-01", 310, 1),
        ]

    def test_weekly_series_starts_on_monday(self):
        series = reporting.timeseries(_lines(), "week")
        assert series[0]["start"] == "2026-01-05" and series[-1]["start"] == "2026-03-16"
        assert len(series) == 11

    def test_month_buckets_cross_the_year_end(self):
        lines = [
            reporting.Line(date(2025, 12, 31), 1, "milk", 1, 1),
            reporting.Line(date(2026, 1, 1), 2, "milk", 1, 1),
        ]
        assert [b["start"] for b in reporting.timeseries(lines, "month")] == ["2025-12-01", "2026-01-01"]

    def test_empty_series_and_summary(self):
        assert reporting.timeseries([], "week") == []
        assert reporting.summary([]) == {
            "spend": 0, "units": 0, "orders": 0, "first_order": None, "last_order": None,
        }

    def test_summary(self):
        assert reporting.summary(_lines()) == {
            "spend": 1110, "units": 8, "orders": 2,
            "first_order": "2026-01-05", "last_order": "2026-03-20",
        }

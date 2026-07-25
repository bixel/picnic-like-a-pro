"""Standard conversation scenarios simulating recurring app usage over time.

Each scenario is a named sequence of tool calls with assertions, modelling how
a family actually uses the bot: checking what's low, searching, filling the
cart, recording orders, and inspecting statistics.

These are consumed by tests/test_conversations.py, which executes the MCP tools
directly (no Telegram, no Anthropic API) against the seeded 6-month history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Step:
    description: str
    tool: str
    kwargs: dict = field(default_factory=dict)
    assertions: list[Callable] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step]


# ---------------------------------------------------------------------------
# Scenario 1 – Saturday shop: check what's low, fill cart, pick a slot
# ---------------------------------------------------------------------------

SCENARIO_WEEKLY_SHOP = Scenario(
    name="weekly_shop",
    description=(
        "A family member opens the app on a Saturday, checks what's running low "
        "based on the 6-month history, adds the suggested staples to the cart, "
        "and reviews the available delivery slots."
    ),
    steps=[
        Step(
            description="Check what's overdue for reorder",
            tool="get_reorder_due",
            assertions=[
                lambda r: len(r) > 0,
                lambda r: any(x["product_id"] == "p_milk_whole" for x in r),
                lambda r: any(x["product_id"] == "p_bread_whole" for x in r),
                lambda r: all(x["urgency_score"] >= 1.0 for x in r),
            ],
        ),
        Step(
            description="Search for milk",
            tool="search_products",
            kwargs={"query": "melk", "limit": 5},
            assertions=[
                lambda r: len(r) > 0,
                lambda r: any(x["id"] == "p_milk_whole" for x in r),
            ],
        ),
        Step(
            description="Add milk to cart",
            tool="add_to_cart",
            kwargs={"product_id": "p_milk_whole", "quantity": 2},
            assertions=[lambda r: r.get("total_count", 0) >= 2],
        ),
        Step(
            description="Add bread to cart",
            tool="add_to_cart",
            kwargs={"product_id": "p_bread_whole", "quantity": 1},
            assertions=[lambda r: r.get("total_count", 0) >= 3],
        ),
        Step(
            description="Review the cart",
            tool="get_cart",
            assertions=[
                lambda r: len(r.get("items", [])) >= 2,
                lambda r: r.get("total_price", 0) > 0,
            ],
        ),
        Step(
            description="Check available delivery slots",
            tool="get_delivery_slots",
            assertions=[
                lambda r: len(r) >= 4,
                lambda r: all("window_start" in s for s in r),
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# Scenario 2 – Forecast-driven planning across horizons
# ---------------------------------------------------------------------------

SCENARIO_FORECAST = Scenario(
    name="forecast_and_plan",
    description=(
        "User asks for a forecast to plan the week. Weekly staples show as "
        "overdue; widening the horizon to 14 days pulls in the biweekly items."
    ),
    steps=[
        Step(
            description="7-day forecast",
            tool="forecast_order",
            kwargs={"horizon_days": 7},
            assertions=[
                lambda r: len(r) > 0,
                lambda r: any(x["product_id"] == "p_milk_whole" for x in r),
                lambda r: any(x["product_id"] == "p_bananas" for x in r),
                lambda r: all(
                    r[i]["urgency_score"] >= r[i + 1]["urgency_score"]
                    for i in range(len(r) - 1)
                ),
            ],
        ),
        Step(
            description="14-day forecast includes biweekly items",
            tool="forecast_order",
            kwargs={"horizon_days": 14},
            assertions=[
                lambda r: len(r) > 0,
                lambda r: any(x["product_id"] == "p_coffee" for x in r),
                lambda r: any(x["product_id"] == "p_butter" for x in r),
            ],
        ),
        Step(
            description="Recent order history (7 days)",
            tool="get_order_history",
            kwargs={"limit": 5, "days_back": 7},
            assertions=[lambda r: isinstance(r, list)],
        ),
        Step(
            description="Order history over the last 30 days",
            tool="get_order_history",
            kwargs={"limit": 10, "days_back": 30},
            assertions=[lambda r: len(r) >= 3],
        ),
    ],
)


# ---------------------------------------------------------------------------
# Scenario 3 – Statistics deep-dive across all ordering frequencies
# ---------------------------------------------------------------------------

SCENARIO_STATS = Scenario(
    name="product_stats",
    description=(
        "User inspects consumption patterns per product and the overall "
        "frequently-ordered ranking, covering every seeded frequency band."
    ),
    steps=[
        Step(
            description="Stats for milk (weekly)",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_milk_whole"},
            assertions=[
                lambda r: r is not None,
                lambda r: r["order_count"] == 25,
                lambda r: 6.0 <= r["avg_interval_days"] <= 8.0,
                lambda r: r["avg_quantity"] == 2.0,
            ],
        ),
        Step(
            description="Stats for coffee (biweekly)",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_coffee"},
            assertions=[
                lambda r: r is not None,
                lambda r: r["order_count"] == 13,
                lambda r: 13.0 <= r["avg_interval_days"] <= 15.0,
            ],
        ),
        Step(
            description="Stats for olive oil (monthly)",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_olive_oil"},
            assertions=[
                lambda r: r is not None,
                lambda r: r["order_count"] == 7,
                lambda r: 26.0 <= r["avg_interval_days"] <= 30.0,
            ],
        ),
        Step(
            description="Stats for tomato sauce (single order, no interval)",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_tomato_sauce"},
            assertions=[
                lambda r: r is not None,
                lambda r: r["order_count"] == 1,
                lambda r: r["avg_interval_days"] is None,
            ],
        ),
        Step(
            description="Stats for a never-ordered product",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_quark"},
            assertions=[lambda r: r is None],
        ),
        Step(
            description="Top 10 frequently ordered",
            tool="get_frequently_ordered",
            kwargs={"limit": 10},
            assertions=[
                lambda r: len(r) >= 5,
                lambda r: r[0]["order_count"] == 25,
                lambda r: all("product_id" in x and "order_count" in x for x in r),
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# Scenario 4 – Record an order and verify statistics update
# ---------------------------------------------------------------------------

SCENARIO_RECORD_AND_VERIFY = Scenario(
    name="record_and_verify",
    description=(
        "After checking out in the Picnic app, the user records the order. "
        "It must appear in history immediately and bump the product statistics."
    ),
    steps=[
        Step(
            description="Record a new order",
            tool="record_order",
            kwargs={
                "items": [
                    {"product_id": "p_milk_whole",  "quantity": 2, "unit_price": 119},
                    {"product_id": "p_bread_whole", "quantity": 1, "unit_price": 199},
                    {"product_id": "p_eggs",        "quantity": 1, "unit_price": 349},
                ],
                "picnic_order_id": "manual_test_order_001",
                "notes": "Test order – automated scenario",
            },
            assertions=[
                lambda r: "order_id" in r,
                lambda r: r["item_count"] == 3,
            ],
        ),
        Step(
            description="Order appears in recent history",
            tool="get_order_history",
            kwargs={"limit": 5},
            assertions=[
                lambda r: len(r) >= 1,
                lambda r: any(o.get("picnic_order_id") == "manual_test_order_001" for o in r),
            ],
        ),
        Step(
            description="Milk order count increased",
            tool="get_product_order_stats",
            kwargs={"product_id": "p_milk_whole"},
            assertions=[
                lambda r: r is not None,
                lambda r: r["order_count"] == 26,
            ],
        ),
    ],
)


ALL_SCENARIOS = [
    SCENARIO_WEEKLY_SHOP,
    SCENARIO_FORECAST,
    SCENARIO_STATS,
    SCENARIO_RECORD_AND_VERIFY,
]

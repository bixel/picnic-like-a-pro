"""Order-history aggregation for the reporting UI (pure Python, no DB access).

The category tree is Global → L1 → L2 → L3 → product.  A node is addressed by
``(level, id)`` with string ids; products without a category sit under one
top-level ``uncategorised`` node.

Spend is always ``quantity × unit_price`` of the order lines, so a node's
figure is the sum of its children's.  An order's own total (which includes
fees and deposits) is reported separately, for the global node only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

GLOBAL = ("global", "all")
UNCATEGORISED = ("uncategorised", "none")

NodeKey = tuple[str, str]


@dataclass(frozen=True)
class Line:
    """One order line, dated by its order."""

    day: date
    order_id: int
    product_id: str
    units: int
    spend: int  # cents


@dataclass
class _Totals:
    spend: int = 0
    units: int = 0
    orders: set[int] = field(default_factory=set)

    def add(self, line: Line) -> None:
        self.spend += line.spend
        self.units += line.units
        self.orders.add(line.order_id)

    def as_dict(self) -> dict:
        return {"spend": self.spend, "units": self.units, "orders": len(self.orders)}


def parse_day(value: str | None) -> date | None:
    """The calendar day of a stored ISO datetime, as written (no timezone shift)."""
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def order_days(
    orders: list[dict], date_from: date | None = None, date_to: date | None = None
) -> dict[int, date]:
    """``{order id: day}`` for the orders in the (inclusive) date range.

    An order whose date cannot be read is left out of every report.
    """
    days = {}
    for order in orders:
        day = parse_day(order["date"])
        if day is None or (date_from and day < date_from) or (date_to and day > date_to):
            continue
        days[order["id"]] = day
    return days


def build_lines(days: dict[int, date], order_lines: list[dict]) -> list[Line]:
    """The order lines of the orders in *days*."""
    return [
        Line(
            day=days[row["order_id"]],
            order_id=row["order_id"],
            product_id=row["product_id"],
            units=row["quantity"],
            spend=row["quantity"] * (row["unit_price"] or 0),
        )
        for row in order_lines
        if row["order_id"] in days
    ]


def product_path(product: dict) -> list[tuple[NodeKey, str | None]]:
    """``[(node key, name), ...]`` from below the global node down to the product."""
    leaf = (("product", product["id"]), product["name"])
    if product["l3_id"] is None:
        return [(UNCATEGORISED, None), leaf]
    l1_id = "unknown" if product["l1_id"] is None else str(product["l1_id"])
    return [
        (("l1", l1_id), product["l1_name"]),
        (("l2", str(product["l2_id"])), product["l2_name"]),
        (("l3", str(product["l3_id"])), product["l3_name"]),
        leaf,
    ]


def hierarchy(lines: list[Line], products: list[dict]) -> dict:
    """The category tree with totals per node; only nodes that were ordered.

    Children are sorted by spend, highest first.
    """
    paths = {p["id"]: product_path(p) for p in products}
    totals: dict[tuple[NodeKey, ...], _Totals] = defaultdict(_Totals)
    names: dict[tuple[NodeKey, ...], str | None] = {(): None}
    children: dict[tuple[NodeKey, ...], set[tuple[NodeKey, ...]]] = defaultdict(set)

    for line in lines:
        path = paths.get(line.product_id)
        if path is None:
            continue
        trail: tuple[NodeKey, ...] = ()
        totals[trail].add(line)
        for key, name in path:
            child = trail + (key,)
            children[trail].add(child)
            names[child] = name
            totals[child].add(line)
            trail = child

    def node(trail: tuple[NodeKey, ...]) -> dict:
        level, node_id = trail[-1] if trail else GLOBAL
        kids = [node(child) for child in children.get(trail, ())]
        kids.sort(key=lambda n: (-n["spend"], n["name"] or "", n["id"]))
        return {
            "level": level,
            "id": node_id,
            "name": names[trail],
            **totals[trail].as_dict(),
            "children": kids,
        }

    return node(())


def select_node(products: list[dict], level: str, node_id: str) -> tuple[list[dict], set[str]] | None:
    """The breadcrumb path to a node and the ids of the products below it.

    ``None`` when no product sits under such a node.  The path excludes the
    global node and ends with the node itself.
    """
    if level == "global":
        return [], {p["id"] for p in products}
    key = (level, node_id)
    crumbs: list[dict] | None = None
    product_ids = set()
    for product in products:
        path = product_path(product)
        keys = [k for k, _ in path]
        if key not in keys:
            continue
        product_ids.add(product["id"])
        if crumbs is None:
            crumbs = [
                {"level": k[0], "id": k[1], "name": name}
                for k, name in path[: keys.index(key) + 1]
            ]
    if crumbs is None:
        return None
    return crumbs, product_ids


def bucket_start(day: date, bucket: str) -> date:
    """The first day of the week (Monday) or month *day* falls in."""
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _next_bucket(start: date, bucket: str) -> date:
    if bucket == "week":
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def timeseries(lines: list[Line], bucket: str) -> list[dict]:
    """Totals per week or month, oldest first, with empty buckets in between."""
    if not lines:
        return []
    totals: dict[date, _Totals] = defaultdict(_Totals)
    for line in lines:
        totals[bucket_start(line.day, bucket)].add(line)
    series = []
    start, last = min(totals), max(totals)
    while start <= last:
        series.append({"start": start.isoformat(), **totals[start].as_dict()})
        start = _next_bucket(start, bucket)
    return series


def summary(lines: list[Line]) -> dict:
    """Totals over *lines* with the first and last order day."""
    totals = _Totals()
    for line in lines:
        totals.add(line)
    days = [line.day for line in lines]
    return {
        **totals.as_dict(),
        "first_order": min(days).isoformat() if days else None,
        "last_order": max(days).isoformat() if days else None,
    }

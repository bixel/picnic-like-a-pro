"""Mock implementation of the Picnic API for testing and demo environments.

When PICNIC_MOCK=true is set, the real python-picnic-api2 client is replaced
with this in-memory mock so the full application runs without real Picnic
credentials and without triggering actual orders or cart modifications.

Cart state is in-memory by default.  Set PICNIC_MOCK_PERSIST_CART=true to
persist the cart across restarts via a JSON file alongside the DB.

The delivery history (25 weekly orders) is generated relative to the current
date so that urgency scores and forecasting results are always meaningful.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Product catalog  – realistic Dutch grocery assortment (~50 SKUs)
# ---------------------------------------------------------------------------

CATALOG: list[dict] = [
    # Zuivel, Eieren & Boter
    {"id": "p_milk_whole",    "name": "Volle Melk 1L",                  "price": 119, "unit_quantity": "1 liter",    "image_id": "img_milk_whole",    "category": "Zuivel, Eieren & Boter"},
    {"id": "p_milk_semi",     "name": "Halfvolle Melk 1L",              "price": 109, "unit_quantity": "1 liter",    "image_id": "img_milk_semi",     "category": "Zuivel, Eieren & Boter"},
    {"id": "p_yogurt_nat",    "name": "Naturel Yogurt 500g",            "price": 159, "unit_quantity": "500 gram",   "image_id": "img_yogurt_nat",    "category": "Zuivel, Eieren & Boter"},
    {"id": "p_yogurt_grk",    "name": "Griekse Yogurt 450g",            "price": 229, "unit_quantity": "450 gram",   "image_id": "img_yogurt_grk",    "category": "Zuivel, Eieren & Boter"},
    {"id": "p_butter",        "name": "Roomboter 250g",                 "price": 219, "unit_quantity": "250 gram",   "image_id": "img_butter",        "category": "Zuivel, Eieren & Boter"},
    {"id": "p_cheese_gouda",  "name": "Goudse Kaas Jong Belegen 500g",  "price": 349, "unit_quantity": "500 gram",   "image_id": "img_cheese_gouda",  "category": "Zuivel, Eieren & Boter"},
    {"id": "p_cheese_brie",   "name": "Brie 200g",                      "price": 299, "unit_quantity": "200 gram",   "image_id": "img_cheese_brie",   "category": "Zuivel, Eieren & Boter"},
    {"id": "p_eggs",          "name": "Vrije Uitloop Eieren 12 stuks",  "price": 349, "unit_quantity": "12 stuks",   "image_id": "img_eggs",          "category": "Zuivel, Eieren & Boter"},
    {"id": "p_cream",         "name": "Slagroom 250ml",                 "price": 189, "unit_quantity": "250 ml",     "image_id": "img_cream",         "category": "Zuivel, Eieren & Boter"},
    {"id": "p_quark",         "name": "Kwark Naturel 500g",             "price": 199, "unit_quantity": "500 gram",   "image_id": "img_quark",         "category": "Zuivel, Eieren & Boter"},

    # Brood & Banket
    {"id": "p_bread_white",   "name": "Wit Brood 800g",                 "price": 179, "unit_quantity": "800 gram",   "image_id": "img_bread_white",   "category": "Brood & Banket"},
    {"id": "p_bread_whole",   "name": "Volkoren Brood 800g",            "price": 199, "unit_quantity": "800 gram",   "image_id": "img_bread_whole",   "category": "Brood & Banket"},
    {"id": "p_croissant",     "name": "Croissants 6 stuks",             "price": 249, "unit_quantity": "6 stuks",    "image_id": "img_croissant",     "category": "Brood & Banket"},

    # Fruit & Groenten
    {"id": "p_apples",        "name": "Elstar Appels 1kg",              "price": 199, "unit_quantity": "1 kg",       "image_id": "img_apples",        "category": "Fruit & Groenten"},
    {"id": "p_bananas",       "name": "Bananen 1kg",                    "price": 149, "unit_quantity": "1 kg",       "image_id": "img_bananas",       "category": "Fruit & Groenten"},
    {"id": "p_tomatoes",      "name": "Trostomaten 500g",               "price": 149, "unit_quantity": "500 gram",   "image_id": "img_tomatoes",      "category": "Fruit & Groenten"},
    {"id": "p_potatoes",      "name": "Aardappelen 1.5kg",              "price": 179, "unit_quantity": "1.5 kg",     "image_id": "img_potatoes",      "category": "Fruit & Groenten"},
    {"id": "p_onions",        "name": "Uien 1kg",                       "price": 99,  "unit_quantity": "1 kg",       "image_id": "img_onions",        "category": "Fruit & Groenten"},
    {"id": "p_carrots",       "name": "Wortels 1kg",                    "price": 119, "unit_quantity": "1 kg",       "image_id": "img_carrots",       "category": "Fruit & Groenten"},
    {"id": "p_broccoli",      "name": "Broccoli 500g",                  "price": 149, "unit_quantity": "500 gram",   "image_id": "img_broccoli",      "category": "Fruit & Groenten"},
    {"id": "p_spinach",       "name": "Spinazie 300g",                  "price": 189, "unit_quantity": "300 gram",   "image_id": "img_spinach",       "category": "Fruit & Groenten"},
    {"id": "p_cucumber",      "name": "Komkommer",                      "price": 89,  "unit_quantity": "1 stuk",     "image_id": "img_cucumber",      "category": "Fruit & Groenten"},
    {"id": "p_pepper_red",    "name": "Rode Paprika",                   "price": 129, "unit_quantity": "1 stuk",     "image_id": "img_pepper_red",    "category": "Fruit & Groenten"},
    {"id": "p_mushrooms",     "name": "Champignons 250g",               "price": 149, "unit_quantity": "250 gram",   "image_id": "img_mushrooms",     "category": "Fruit & Groenten"},

    # Vlees, Vis & Vegetarisch
    {"id": "p_chicken",       "name": "Kipfilet 500g",                  "price": 499, "unit_quantity": "500 gram",   "image_id": "img_chicken",       "category": "Vlees, Vis & Vegetarisch"},
    {"id": "p_ground_beef",   "name": "Rundergehakt 500g",              "price": 449, "unit_quantity": "500 gram",   "image_id": "img_ground_beef",   "category": "Vlees, Vis & Vegetarisch"},
    {"id": "p_salmon",        "name": "Zalm Filet 250g",                "price": 599, "unit_quantity": "250 gram",   "image_id": "img_salmon",        "category": "Vlees, Vis & Vegetarisch"},
    {"id": "p_sausages",      "name": "Rookworst 250g",                 "price": 279, "unit_quantity": "250 gram",   "image_id": "img_sausages",      "category": "Vlees, Vis & Vegetarisch"},

    # Dranken
    {"id": "p_oj",            "name": "Sinaasappelsap 1L",              "price": 179, "unit_quantity": "1 liter",    "image_id": "img_oj",            "category": "Dranken"},
    {"id": "p_water",         "name": "Mineraalwater 6x1.5L",           "price": 249, "unit_quantity": "6 x 1.5 L",  "image_id": "img_water",         "category": "Dranken"},
    {"id": "p_cola",          "name": "Cola 6x330ml",                   "price": 399, "unit_quantity": "6 x 330 ml", "image_id": "img_cola",          "category": "Dranken"},

    # Ontbijt & Beleg
    {"id": "p_oatmeal",       "name": "Havermout 500g",                 "price": 179, "unit_quantity": "500 gram",   "image_id": "img_oatmeal",       "category": "Ontbijt & Beleg"},
    {"id": "p_muesli",        "name": "Muesli Crunch 750g",             "price": 299, "unit_quantity": "750 gram",   "image_id": "img_muesli",        "category": "Ontbijt & Beleg"},
    {"id": "p_hagelslag",     "name": "Chocolade Hagelslag 400g",       "price": 179, "unit_quantity": "400 gram",   "image_id": "img_hagelslag",     "category": "Ontbijt & Beleg"},
    {"id": "p_jam",           "name": "Aardbeienjam 450g",              "price": 219, "unit_quantity": "450 gram",   "image_id": "img_jam",           "category": "Ontbijt & Beleg"},
    {"id": "p_coffee",        "name": "Filterkoffie Gemalen 500g",      "price": 549, "unit_quantity": "500 gram",   "image_id": "img_coffee",        "category": "Ontbijt & Beleg"},
    {"id": "p_tea",           "name": "Groene Thee 20 zakjes",          "price": 199, "unit_quantity": "20 zakjes",  "image_id": "img_tea",           "category": "Ontbijt & Beleg"},
    {"id": "p_peanut_butter", "name": "Pindakaas Naturel 350g",         "price": 249, "unit_quantity": "350 gram",   "image_id": "img_peanut_butter", "category": "Ontbijt & Beleg"},

    # Pasta, Rijst & Granen
    {"id": "p_spaghetti",     "name": "Spaghetti 500g",                 "price": 129, "unit_quantity": "500 gram",   "image_id": "img_spaghetti",     "category": "Pasta, Rijst & Granen"},
    {"id": "p_penne",         "name": "Penne 500g",                     "price": 129, "unit_quantity": "500 gram",   "image_id": "img_penne",         "category": "Pasta, Rijst & Granen"},
    {"id": "p_rice",          "name": "Zilvervliesrijst 1kg",           "price": 199, "unit_quantity": "1 kg",       "image_id": "img_rice",          "category": "Pasta, Rijst & Granen"},
    {"id": "p_couscous",      "name": "Couscous 500g",                  "price": 149, "unit_quantity": "500 gram",   "image_id": "img_couscous",      "category": "Pasta, Rijst & Granen"},

    # Sauzen, Kruiden & Olie
    {"id": "p_tomato_sauce",  "name": "Tomatensaus Basilicum 400g",     "price": 149, "unit_quantity": "400 gram",   "image_id": "img_tomato_sauce",  "category": "Sauzen, Kruiden & Olie"},
    {"id": "p_pesto",         "name": "Pesto Alla Genovese 190g",       "price": 249, "unit_quantity": "190 gram",   "image_id": "img_pesto",         "category": "Sauzen, Kruiden & Olie"},
    {"id": "p_olive_oil",     "name": "Extra Vierge Olijfolie 500ml",   "price": 499, "unit_quantity": "500 ml",     "image_id": "img_olive_oil",     "category": "Sauzen, Kruiden & Olie"},
    {"id": "p_soy_sauce",     "name": "Sojasaus 150ml",                 "price": 199, "unit_quantity": "150 ml",     "image_id": "img_soy_sauce",     "category": "Sauzen, Kruiden & Olie"},

    # Snacks & Snoep
    {"id": "p_chips",         "name": "Chips Naturel 200g",             "price": 199, "unit_quantity": "200 gram",   "image_id": "img_chips",         "category": "Snacks & Snoep"},
    {"id": "p_nuts",          "name": "Gemengde Noten 200g",            "price": 299, "unit_quantity": "200 gram",   "image_id": "img_nuts",          "category": "Snacks & Snoep"},
    {"id": "p_stroopwafel",   "name": "Stroopwafels 8 stuks",           "price": 179, "unit_quantity": "8 stuks",    "image_id": "img_stroopwafel",   "category": "Snacks & Snoep"},
]

_CATALOG_BY_ID: dict[str, dict] = {p["id"]: p for p in CATALOG}

CATEGORIES: list[dict] = [
    {"id": "cat_dairy",     "name": "Zuivel, Eieren & Boter",  "depth": 0},
    {"id": "cat_bread",     "name": "Brood & Banket",           "depth": 0},
    {"id": "cat_produce",   "name": "Fruit & Groenten",         "depth": 0},
    {"id": "cat_meat",      "name": "Vlees, Vis & Vegetarisch", "depth": 0},
    {"id": "cat_drinks",    "name": "Dranken",                  "depth": 0},
    {"id": "cat_breakfast", "name": "Ontbijt & Beleg",          "depth": 0},
    {"id": "cat_pasta",     "name": "Pasta, Rijst & Granen",    "depth": 0},
    {"id": "cat_sauces",    "name": "Sauzen, Kruiden & Olie",   "depth": 0},
    {"id": "cat_snacks",    "name": "Snacks & Snoep",           "depth": 0},
]


# ---------------------------------------------------------------------------
# Delivery history generator
# ---------------------------------------------------------------------------

# (product_id, quantity_per_order, frequency)
_ORDER_PATTERNS: list[tuple[str, int, str]] = [
    ("p_milk_whole",   2, "weekly"),
    ("p_bread_whole",  1, "weekly"),
    ("p_bananas",      1, "weekly"),
    ("p_yogurt_nat",   1, "weekly"),
    ("p_eggs",         1, "weekly"),
    ("p_coffee",       1, "biweekly"),
    ("p_butter",       1, "biweekly"),
    ("p_cheese_gouda", 1, "biweekly"),
    ("p_chicken",      1, "biweekly"),
    ("p_spaghetti",    2, "monthly"),
    ("p_rice",         1, "monthly"),
    ("p_olive_oil",    1, "monthly"),
    ("p_salmon",       1, "occasional"),
    ("p_chips",        1, "occasional"),
    ("p_tomato_sauce", 2, "one_time"),
]

# Which of the 25 orders (index 0 = oldest, 24 = newest) contain each frequency
_FREQUENCY_INDICES: dict[str, list[int]] = {
    "weekly":     list(range(25)),        # 25 orders, ~7-day interval
    "biweekly":   list(range(0, 25, 2)),  # 13 orders, ~14-day interval
    "monthly":    list(range(0, 25, 4)),  # 7 orders,  ~28-day interval
    "occasional": [0, 12, 24],            # 3 orders,  ~84-day interval
    "one_time":   [24],                   # 1 order — no interval computable
}


def _make_delivery_item(product_id: str, quantity: int) -> dict:
    p = _CATALOG_BY_ID[product_id]
    return {
        "id": product_id,
        "name": p["name"],
        "price": p["price"],
        "unit_quantity": p["unit_quantity"],
        "image_id": p["image_id"],
        "quantity": quantity,
    }


def generate_deliveries(reference_time: datetime | None = None) -> list[dict]:
    """Return 25 weekly deliveries ending 8 days before *reference_time*.

    Each entry has a ``summary`` dict (the shape returned by get_deliveries)
    and a ``details`` dict (the shape returned by get_delivery).  Dates are
    relative to *reference_time* so forecasting urgency is always meaningful.
    """
    now = reference_time or datetime.now(timezone.utc)
    deliveries: list[dict] = []

    for i in range(25):
        days_ago = 8 + (24 - i) * 7  # i=0 → 176 days ago, i=24 → 8 days ago
        order_dt = now - timedelta(days=days_ago)
        delivery_dt = order_dt + timedelta(days=1)

        items = [
            _make_delivery_item(pid, qty)
            for pid, qty, freq in _ORDER_PATTERNS
            if i in _FREQUENCY_INDICES[freq]
        ]

        delivery_id = f"mock_delivery_{i + 1:03d}"
        slot = {
            "window_start": delivery_dt.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
            "window_end":   delivery_dt.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
        }
        total_price = sum(it["price"] * it["quantity"] for it in items)

        deliveries.append({
            "summary": {
                "id": delivery_id,
                "status": "DELIVERED",
                "creation_time": order_dt.isoformat(),
                "slot": slot,
                "total_price": total_price,
            },
            "details": {
                "id": delivery_id,
                "status": "DELIVERED",
                "creation_time": order_dt.isoformat(),
                "delivery_time": delivery_dt.isoformat(),
                "total_price": total_price,
                "slot": slot,
                "orders": [{"id": f"mock_order_{i + 1:03d}", "items": items}],
            },
        })

    return deliveries


# ---------------------------------------------------------------------------
# MockPicnicAPI – drop-in replacement for PicnicAPI (synchronous interface)
# ---------------------------------------------------------------------------

class MockPicnicAPI:
    """Synchronous mock of PicnicAPI.  Methods stay sync so the existing
    thread-pool wrapper in client.py works unchanged."""

    def __init__(self, persist_cart_path: Path | None = None) -> None:
        self._persist_path = persist_cart_path
        self._cart_items: dict[str, dict] = {}
        self._deliveries = generate_deliveries()
        self._deliveries_by_id = {d["summary"]["id"]: d for d in self._deliveries}
        if persist_cart_path and persist_cart_path.exists():
            try:
                self._cart_items = json.loads(persist_cart_path.read_text())
            except (json.JSONDecodeError, ValueError):
                self._cart_items = {}

    # -- Catalog -------------------------------------------------------

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if not q:
            return []
        matches = [p for p in CATALOG if q in p["name"].lower()]
        if not matches:
            return []
        return [{"id": "search_results", "name": "Zoekresultaten", "items": matches}]

    def get_categories(self) -> list[dict]:
        return list(CATEGORIES)

    # -- Cart ----------------------------------------------------------

    def get_cart(self) -> dict:
        items = list(self._cart_items.values())
        return {
            "id": "mock_cart",
            "items": items,
            "total_price": sum(it["price"] * it["quantity"] for it in items),
            "total_count": sum(it["quantity"] for it in items),
        }

    def add_product(self, product_id: str, count: int = 1) -> dict:
        product = _CATALOG_BY_ID.get(product_id)
        if product is None:
            return {"error": f"Product {product_id!r} not found in mock catalog"}
        if product_id in self._cart_items:
            self._cart_items[product_id]["quantity"] += count
        else:
            self._cart_items[product_id] = {
                "id": product_id,
                "name": product["name"],
                "price": product["price"],
                "quantity": count,
                "image_id": product.get("image_id"),
            }
        self._save_cart()
        return self.get_cart()

    def remove_product(self, product_id: str) -> dict:
        self._cart_items.pop(product_id, None)
        self._save_cart()
        return self.get_cart()

    # -- Delivery slots ------------------------------------------------

    def get_delivery_slots(self) -> dict:
        now = datetime.now(timezone.utc)
        slots: list[dict] = []
        for day_offset in range(1, 8):
            date = now + timedelta(days=day_offset)
            for hour in (9, 12, 16, 19):
                start = date.replace(hour=hour, minute=0, second=0, microsecond=0)
                slots.append({
                    "slot_id": f"slot_{day_offset}_{hour:02d}00",
                    "window_start": start.isoformat(),
                    "window_end": (start + timedelta(hours=2)).isoformat(),
                    "cut_off_time": (start - timedelta(hours=16)).isoformat(),
                    "is_available": True,
                    "minimum_order_value": 3500,
                    "delivery_fee": 0,
                })
        return {"delivery_slots": slots}

    # -- Order history -------------------------------------------------

    def get_deliveries(self) -> list[dict]:
        return [d["summary"] for d in self._deliveries]

    def get_delivery(self, delivery_id: str) -> dict:
        entry = self._deliveries_by_id.get(delivery_id)
        return entry["details"] if entry else {}

    # -- Internal ------------------------------------------------------

    def _save_cart(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(json.dumps(self._cart_items, indent=2))


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_mock_client: MockPicnicAPI | None = None


def get_mock_client() -> MockPicnicAPI:
    """Return (lazily creating) the singleton MockPicnicAPI instance."""
    global _mock_client
    if _mock_client is None:
        cart_path: Path | None = None
        if os.getenv("PICNIC_MOCK_PERSIST_CART", "").lower() == "true":
            db_path = os.getenv("DB_PATH", "data/picnic.db")
            cart_path = Path(db_path).parent / "mock_cart.json"
        _mock_client = MockPicnicAPI(persist_cart_path=cart_path)
    return _mock_client


def reset_mock_client() -> None:
    """Discard the singleton so the next get_mock_client() call starts fresh."""
    global _mock_client
    _mock_client = None

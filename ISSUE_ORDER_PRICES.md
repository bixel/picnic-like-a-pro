# Order line prices are wrong

Status: **open** — described here, not yet fixed.

The reporting UI shows spend that is far too high for some products.  For
example, three ginger shots bought in one delivery in March 2025 show about
13,000 € of spend; they cost 5.99 € together.

There are two separate errors in how `picnic/client.py::delivery_line_items()`
reads prices from a delivery.  Both end up in `order_items.unit_price`, which
is the only input to spend (`quantity × unit_price`, see "Reporting UI" in
`CLAUDE.md`).

---

## What Picnic sends

A delivery nests `orders → ORDER_LINE → ORDER_ARTICLE`.  A line of a past
delivery looks like this (shortened, from a real payload):

```json
{
  "type": "ORDER_LINE",
  "price": 747,
  "display_price": 747,
  "decorators": [
    {"type": "PRICE", "display_price": 599},
    {"type": "PROMO", "text": "3 für 5.99€"}
  ],
  "items": [
    {"type": "ORDER_ARTICLE", "id": "s1024698", "price": 432199,
     "decorators": [{"type": "QUANTITY", "quantity": 1}]},
    {"type": "ORDER_ARTICLE", "id": "s1030505", "price": 432199,
     "decorators": [{"type": "QUANTITY", "quantity": 1}]},
    {"type": "ORDER_ARTICLE", "id": "s1030462", "price": 432199,
     "decorators": [{"type": "QUANTITY", "quantity": 1}]}
  ]
}
```

Three facts follow from it:

| Field | Meaning |
|---|---|
| `ORDER_LINE.price` | Price of the whole line **before** a promotion |
| `PRICE` decorator's `display_price` | Price actually paid for the line; present only when a promotion applied |
| `ORDER_ARTICLE.price` | Always `432199` in past deliveries — a placeholder, not a price |

A line holds several articles when a mix-and-match promotion ("3 für 5.99€")
groups different products.

---

## Error 1: the placeholder is stored as a price

`delivery_line_items()` uses the line price only when the line holds one
article:

```python
line_price = line.get("price") if nested and len(articles) == 1 else None
unit_price = (
    _price_per_unit(line_price, quantity)
    or entry.get("unit_price")
    or article.get("unit_price")
    or article.get("price")          # 432199 for a real delivery
)
```

For a line with several articles, `line_price` is `None` and the chain falls
through to `article.get("price")`.  Every article of a mixed promo line is
therefore stored at 4,321.99 €.

The same value goes into the product dict the function returns, so
`upsert_product` writes it to `products.unit_price` too.

Measured on the production database (139 orders, 5,481 order lines):

| | Count |
|---|---|
| Order lines with `unit_price = 432199` | 258 |
| Orders containing at least one | 70 |
| Products with `unit_price = 432199` | 143 |
| Spend of those 258 lines | about 1.17 million € |
| Spend of all other lines | about 15,900 € |

## Error 2: promotions are ignored

A line with a single article takes `ORDER_LINE.price`, the pre-promotion
price, and never looks at the `PRICE` decorator.  A product bought at
"jetzt 1.99€" is stored at its regular 2.99 €.

In one delivery checked in full (34 lines):

| | Amount |
|---|---|
| Sum of `ORDER_LINE.price` | 112.89 € |
| Sum of paid prices (`PRICE` decorator, else line price) | 105.03 € |
| Difference | 7.86 € |
| Picnic's `total_savings` for the delivery's orders | 7.86 € |

Across the database, the lines not hit by error 1 sum to about 15,900 €,
against 14,937 € of `orders.total_price` — roughly 6 % too high.

The paid prices of that delivery are still 3.39 € above its `total_price`.
What makes up that remainder has not been traced.

---

## What is affected

- **Reporting UI** — spend at every level of the category tree.  The global
  node's `order_total` comes from `orders.total_price` and is correct.
- **MCP tools** that return `unit_price` of products or order lines, so the
  LLM can quote a product at 4,321.99 €.
- Not affected: quantities, order dates, order counts, forecasting (it does
  not use prices).

Tests did not catch it because the fixtures in `tests/test_picnic_client.py`
give articles a realistic `price`; none uses the placeholder or a line with
several articles and a `PRICE` decorator.

---

## Proposed fix

1. **Line price paid:** the `PRICE` decorator's `display_price` when present,
   else `ORDER_LINE.price`.
2. **Never read `ORDER_ARTICLE.price`** for a nested line.  The flat shape of
   the mock client (no line level) keeps its own price fields.
3. **A line with several articles:** split the paid price across the articles
   by quantity (5.99 € / 3 = 2.00 € each).  The split is an approximation —
   Picnic does not say what each article of a mixed line cost — and rounding
   must not lose a cent of the line total.
4. **Do not write an order line's price to `products.unit_price`** when it
   comes from a promotion or a split; the catalogue price should stay the
   regular one.
5. **Repair the stored data.**  The payloads are not kept, so all 139
   deliveries have to be fetched again (one `get_delivery` each) to rewrite
   `order_items.unit_price`, plus a price lookup for the 143 products.
6. **Tests** with a fixture copied from a real line: placeholder article
   prices, several articles, `PRICE` and `PROMO` decorators.

Open question: whether to store the regular line price as well (a second
column), so reports can show savings.

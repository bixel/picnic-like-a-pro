"""Show how Picnic describes a product's category, to find the L1/L2 names.

A product page's category button links to
``app.picnic://categories/{l1}/l2/{l2}/l3/{l3}``.  The L3 name is read from
the L2 category page (``vertical-article-tiles-sub-header-{l3}``); where that
page keeps the L2 name, and whether an L1 page exists, is not known yet.  This
script prints the outline of those pages so the parser can be written against
a real payload.

Read-only: it sends GET requests for public catalogue pages and writes
nothing to the database.  The pages hold no account data, so the saved JSON
is safe to share.

Usage:
    uv run python scripts/inspect_category.py <product_id>
    uv run python scripts/inspect_category.py <product_id> --save data/category-pages
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from python_picnic_api2.models import Article, pml

from picnic_meal_planner.picnic import client as picnic

# The L1 path is a guess, modelled on the L2 one; a 404 or error page is an
# answer too.
PAGES = {
    "product": "/pages/product-details-page-root?id={product_id}&show_category_action=true",
    "l2": "/pages/L2-category-page-root?category_id={l2}&l3_category_id={l3}",
    "l1": "/pages/L1-category-page-root?category_id={l1}",
}


def outline(page) -> list[str]:
    """One line per node that has an id, a header role or a text."""
    lines = []
    for node in pml.walk(page):
        node_id = node.get("id") if isinstance(node.get("id"), str) else None
        component = node.get("pml", {}).get("component", {}) if isinstance(node.get("pml"), dict) else {}
        role = component.get("accessibilityRole")
        label = component.get("accessibilityLabel")
        text = pml.text_of(node)
        target = (node.get("onPress") or {}).get("target") if isinstance(node.get("onPress"), dict) else None
        if node_id or role == "header" or text or target:
            parts = [f"id={node_id}" if node_id else "", f"role={role}" if role else "",
                     f"label={label!r}" if label else "", f"text={text!r}" if text else "",
                     f"target={target}" if target else ""]
            lines.append("  " + "  ".join(p for p in parts if p))
    return lines


async def fetch(path: str):
    try:
        return await picnic._call("_get", path, add_picnic_headers=True)
    except Exception as exc:  # noqa: BLE001 — a diagnostic: report and go on
        return {"error": f"{type(exc).__name__}: {exc}"}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("product_id", help="a Picnic product id, e.g. s1018620")
    parser.add_argument("--save", type=Path, help="directory to write the raw pages to")
    args = parser.parse_args()

    product = await fetch(PAGES["product"].format(product_id=args.product_id))
    ids = Article.category_ids_from_page(product) if isinstance(product, dict) else None
    pages = {"product": product}
    if ids is None:
        print("No category button on the product page.")
    else:
        l1, l2, l3 = ids
        print(f"Category ids: l1={l1} l2={l2} l3={l3}")
        print("L3 names on the L2 page:")
        pages["l2"] = await fetch(PAGES["l2"].format(l2=l2, l3=l3))
        for l3_id, name in picnic.l3_names_on_page(pages["l2"]).items():
            print(f"  {l3_id}: {name}")
        pages["l1"] = await fetch(PAGES["l1"].format(l1=l1))

    for kind, page in pages.items():
        if kind == "product":
            continue
        print(f"\n== {kind} page ==")
        top = list(page) if isinstance(page, dict) else type(page).__name__
        print(f"top-level keys: {top}")
        print("\n".join(outline(page)[:200]) or "  (no nodes with ids or text)")

    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)
        for kind, page in pages.items():
            path = args.save / f"{args.product_id}_{kind}.json"
            path.write_text(json.dumps(page, indent=1, ensure_ascii=False))
            print(f"Saved {path}")


if __name__ == "__main__":
    asyncio.run(main())

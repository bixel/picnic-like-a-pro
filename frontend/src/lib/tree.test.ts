import { describe, expect, it } from "vitest";

import type { HierarchyNode, Level } from "./api";
import { nodeName } from "./format";
import { branchKeys, filterTree, share } from "./tree";

function node(level: Level, id: string, name: string | null, spend: number, children: HierarchyNode[] = []) {
  return { level, id, name, spend, units: 1, orders: 1, children } satisfies HierarchyNode;
}

const tree = node("global", "all", null, 1000, [
  node("l1", "1", "Frisch", 700, [
    node("l2", "2", "Molkerei", 700, [
      node("l3", "3", "Milch", 700, [
        node("product", "milk", "Vollmilch", 500),
        node("product", "oat", "Haferdrink", 200),
      ]),
    ]),
  ]),
  node("uncategorised", "none", null, 300, [node("product", "bag", "Tragetasche", 300)]),
]);

describe("filterTree", () => {
  it("returns the tree unchanged for an empty query", () => {
    expect(filterTree(tree, "  ")).toBe(tree);
  });

  it("keeps a matching product with its ancestors only", () => {
    const result = filterTree(tree, "hafer")!;
    expect(result.children.map((c) => c.id)).toEqual(["1"]);
    const l3 = result.children[0].children[0].children[0];
    expect(l3.children.map((c) => c.id)).toEqual(["oat"]);
  });

  it("keeps everything below a matching category", () => {
    const result = filterTree(tree, "MOLK")!;
    expect(result.children[0].children[0].children[0].children).toHaveLength(2);
  });

  it("matches the fallback names", () => {
    expect(filterTree(tree, "uncat")!.children.map((c) => c.level)).toEqual(["uncategorised"]);
  });

  it("keeps an empty root when nothing matches", () => {
    expect(filterTree(tree, "zzz")!.children).toEqual([]);
  });
});

describe("branchKeys", () => {
  it("lists every node with children", () => {
    expect([...branchKeys(tree)]).toEqual(["global:all", "l1:1", "l2:2", "l3:3", "uncategorised:none"]);
  });
});

describe("share", () => {
  it("is the fraction of the parent's spend", () => {
    expect(share(tree.children[0], tree)).toBe(0.7);
    expect(share(tree.children[0], { ...tree, spend: 0 })).toBe(0);
  });
});

describe("nodeName", () => {
  it("falls back when a name was not looked up", () => {
    expect(nodeName({ level: "l2", id: "20", name: null })).toBe("Category 20");
    expect(nodeName({ level: "l1", id: "unknown", name: null })).toBe("Unknown category");
    expect(nodeName({ level: "global", id: "all" })).toBe("All orders");
    expect(nodeName({ level: "l3", id: "3", name: "Milch" })).toBe("Milch");
  });
});

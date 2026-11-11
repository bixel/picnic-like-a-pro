import type { HierarchyNode, Level } from "./api";
import { nodeName } from "./format";

export const nodeKey = (node: { level: Level; id: string }) => `${node.level}:${node.id}`;

/**
 * The tree reduced to the nodes whose name contains *query*, with their
 * ancestors.  A matching category keeps everything below it.
 */
export function filterTree(node: HierarchyNode, query: string): HierarchyNode | null {
  const needle = query.trim().toLowerCase();
  if (!needle) return node;
  if (node.level !== "global" && nodeName(node).toLowerCase().includes(needle)) return node;
  const children = node.children
    .map((child) => filterTree(child, needle))
    .filter((child): child is HierarchyNode => child !== null);
  return children.length || node.level === "global" ? { ...node, children } : null;
}

/** Keys of every node that has children — to expand a filtered tree fully. */
export function branchKeys(node: HierarchyNode, keys = new Set<string>()): Set<string> {
  if (node.children.length) {
    keys.add(nodeKey(node));
    for (const child of node.children) branchKeys(child, keys);
  }
  return keys;
}

/** A node's share of its parent's spend, 0–1. */
export function share(node: HierarchyNode, parent: HierarchyNode): number {
  return parent.spend > 0 ? node.spend / parent.spend : 0;
}

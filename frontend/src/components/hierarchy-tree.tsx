import { ChevronRight, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Input } from "@/components/ui/input";
import type { HierarchyNode } from "@/lib/api";
import { formatMoney, nodeName } from "@/lib/format";
import { branchKeys, filterTree, nodeKey, share } from "@/lib/tree";
import { cn } from "@/lib/utils";

interface Props {
  root: HierarchyNode;
  selected: string;
  /** Keys of the selected node's ancestors, opened so the selection is visible. */
  ancestors: string[];
  onSelect: (node: HierarchyNode) => void;
}

export function HierarchyTree({ root, selected, ancestors, onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(() => new Set([nodeKey(root)]));

  const ancestorKey = ancestors.join("|");
  useEffect(() => {
    if (ancestorKey) setExpanded((open) => new Set([...open, ...ancestorKey.split("|")]));
  }, [ancestorKey]);

  const filtered = useMemo(() => filterTree(root, query) ?? root, [root, query]);
  // While searching, every match is shown — collapsing would hide results.
  const open = useMemo(() => (query.trim() ? branchKeys(filtered) : expanded), [query, filtered, expanded]);

  const toggle = (key: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="relative px-5">
        <Search className="pointer-events-none absolute top-2.5 left-8 size-4 text-muted-foreground" />
        <Input
          type="search"
          placeholder="Search categories and products"
          aria-label="Search categories and products"
          className="pl-9"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <ul role="tree" className="min-h-0 flex-1 overflow-y-auto px-2 pb-1">
        <Row node={filtered} parent={null} depth={0} open={open} selected={selected} onToggle={toggle} onSelect={onSelect} />
        {filtered.children.length === 0 && (
          <li className="px-3 py-6 text-center text-sm text-muted-foreground">
            {query.trim() ? "Nothing matches your search." : "No orders in this period."}
          </li>
        )}
      </ul>
    </div>
  );
}

interface RowProps {
  node: HierarchyNode;
  parent: HierarchyNode | null;
  depth: number;
  open: Set<string>;
  selected: string;
  onToggle: (key: string) => void;
  onSelect: (node: HierarchyNode) => void;
}

function Row({ node, parent, depth, open, selected, onToggle, onSelect }: RowProps) {
  const key = nodeKey(node);
  const hasChildren = node.children.length > 0;
  const isOpen = hasChildren && open.has(key);
  const isSelected = key === selected;
  const fraction = parent ? share(node, parent) : 1;

  return (
    <li role="treeitem" aria-expanded={hasChildren ? isOpen : undefined} aria-selected={isSelected}>
      <div
        className={cn(
          "group flex items-center rounded-md text-sm hover:bg-accent",
          isSelected && "bg-accent font-medium",
        )}
        style={{ paddingLeft: depth * 14 }}
      >
        {hasChildren ? (
          <button
            type="button"
            aria-label={isOpen ? "Collapse" : "Expand"}
            className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            onClick={() => onToggle(key)}
          >
            <ChevronRight className={cn("size-4 transition-transform", isOpen && "rotate-90")} />
          </button>
        ) : (
          <span className="size-7 shrink-0" />
        )}
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-3 rounded-md py-1.5 pr-2 text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          title={parent ? `${nodeName(node)} — ${(fraction * 100).toFixed(0)}% of ${nodeName(parent)}` : undefined}
          onClick={() => onSelect(node)}
        >
          <span className={cn("min-w-0 flex-1 truncate", !node.name && node.level !== "global" && "text-muted-foreground italic")}>
            {nodeName(node)}
          </span>
          <span className="flex shrink-0 flex-col items-end gap-1">
            <span className="tabular-nums">{formatMoney(node.spend)}</span>
            {parent && (
              <span className="block h-0.5 w-14 overflow-hidden rounded-full bg-border" aria-hidden="true">
                <span className="block h-full rounded-full bg-muted-foreground" style={{ width: `${fraction * 100}%` }} />
              </span>
            )}
          </span>
        </button>
      </div>
      {isOpen && (
        <ul role="group">
          {node.children.map((child) => (
            <Row
              key={nodeKey(child)}
              node={child}
              parent={node}
              depth={depth + 1}
              open={open}
              selected={selected}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

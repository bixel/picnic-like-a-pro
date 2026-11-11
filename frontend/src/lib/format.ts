import type { Bucket, Level } from "./api";

const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR" });
const wholeMoney = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "EUR",
  maximumFractionDigits: 0,
});
const count = new Intl.NumberFormat();

export const formatMoney = (cents: number) => money.format(cents / 100);
export const formatMoneyShort = (cents: number) => wholeMoney.format(cents / 100);
export const formatCount = (value: number) => count.format(value);

/** A stored `YYYY-MM-DD` as a local date (not shifted by the time zone). */
function parseDay(day: string): Date {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date);
}

export function formatDay(day: string | null | undefined): string {
  if (!day) return "—";
  return parseDay(day).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Axis label of a bucket: "Mar 26" for a month, "16 Mar" for a week. */
export function formatBucket(start: string, bucket: Bucket): string {
  const options: Intl.DateTimeFormatOptions =
    bucket === "month" ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" };
  return parseDay(start).toLocaleDateString(undefined, options);
}

/** Tooltip title of a bucket: "March 2026" or "Week of 16 Mar 2026". */
export function formatBucketLong(start: string, bucket: Bucket): string {
  if (bucket === "month") {
    return parseDay(start).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }
  return `Week of ${formatDay(start)}`;
}

/** What to call a node; categories have no name until the import looked it up. */
export function nodeName(node: { level: Level; id: string; name?: string | null }): string {
  if (node.name) return node.name;
  if (node.level === "global") return "All orders";
  if (node.level === "uncategorised") return "Uncategorised";
  if (node.id === "unknown") return "Unknown category";
  return `Category ${node.id}`;
}

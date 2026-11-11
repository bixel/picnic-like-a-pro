import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";

import type { Metric } from "@/components/orders-chart";
import type { Bucket, Level } from "@/lib/api";
import { LoginPage } from "@/routes/login";
import { ReportPage } from "@/routes/report";

/** The report's whole state lives in the URL, so a view can be linked to. */
export interface ReportSearch {
  level: Level;
  id: string;
  bucket: Bucket;
  metric: Metric;
  from?: string;
  to?: string;
}

const LEVELS: Level[] = ["global", "l1", "l2", "l3", "product", "uncategorised"];
const METRICS: Metric[] = ["spend", "units", "orders", "price"];
const DAY = /^\d{4}-\d{2}-\d{2}$/;

function oneOf<T extends string>(value: unknown, options: T[], fallback: T): T {
  return options.includes(value as T) ? (value as T) : fallback;
}

export function validateReportSearch(search: Record<string, unknown>): ReportSearch {
  const level = oneOf(search.level, LEVELS, "global");
  const day = (value: unknown) => (typeof value === "string" && DAY.test(value) ? value : undefined);
  return {
    level,
    // Ids are numeric for categories; the router parses those as numbers.
    id: level === "global" || search.id == null ? "all" : String(search.id),
    bucket: oneOf(search.bucket, ["week", "month"], "month"),
    metric: oneOf(search.metric, METRICS, "spend"),
    from: day(search.from),
    to: day(search.to),
  };
}

const rootRoute = createRootRoute({ component: Outlet });

export const reportRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: validateReportSearch,
  component: ReportPage,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

export const router = createRouter({ routeTree: rootRoute.addChildren([reportRoute, loginRoute]) });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

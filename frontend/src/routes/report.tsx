import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { LogOut } from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { HierarchyTree } from "@/components/hierarchy-tree";
import { KpiTiles } from "@/components/kpi-tiles";
import { METRICS, OrdersChart, OrdersTable, type Metric } from "@/components/orders-chart";
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { ApiError, api, type Bucket, type Level } from "@/lib/api";
import { nodeName } from "@/lib/format";
import { nodeKey } from "@/lib/tree";
import { reportRoute, type ReportSearch } from "@/router";

export function ReportPage() {
  const search = reportRoute.useSearch();
  const navigate = useNavigate({ from: reportRoute.fullPath });
  const queryClient = useQueryClient();
  const [view, setView] = useState<"chart" | "table">("chart");

  const { level, id, bucket } = search;
  const range = { from: search.from, to: search.to };
  // The unit price only means something for a single product.
  const metric: Metric = search.metric === "price" && level !== "product" ? "spend" : search.metric;

  const setSearch = (patch: Partial<ReportSearch>) =>
    void navigate({ search: (current) => ({ ...current, ...patch }) });

  const hierarchy = useQuery({
    queryKey: ["hierarchy", range],
    queryFn: () => api.hierarchy(range),
    placeholderData: keepPreviousData,
  });
  const series = useQuery({
    queryKey: ["timeseries", level, id, bucket, range],
    queryFn: () => api.timeseries(level, id, bucket, range),
    placeholderData: keepPreviousData,
  });

  const error = hierarchy.error ?? series.error;
  const loggedOut = error instanceof ApiError && error.status === 401;
  useEffect(() => {
    if (loggedOut) void navigate({ to: "/login" });
  }, [loggedOut, navigate]);

  const logout = async () => {
    await api.logout();
    queryClient.clear();
    void navigate({ to: "/login" });
  };

  const path = series.data?.path ?? [];
  const current = path.at(-1) ?? { level: "global" as Level, id: "all", name: null };
  const ancestors = [{ level: "global" as Level, id: "all" }, ...path.slice(0, -1)].map(nodeKey);
  const metrics = (Object.keys(METRICS) as Metric[]).filter((m) => m !== "price" || level === "product");

  return (
    <div className="mx-auto flex min-h-svh max-w-[1400px] flex-col gap-4 p-4 lg:h-svh lg:p-6">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <h1 className="mr-auto text-lg font-semibold">Picnic reports</h1>
        <div className="order-3 flex items-center gap-2 text-sm">
          <Input
            type="date"
            aria-label="From"
            className="w-auto"
            value={search.from ?? ""}
            max={search.to}
            onChange={(event) => setSearch({ from: event.target.value || undefined })}
          />
          <span className="text-muted-foreground">to</span>
          <Input
            type="date"
            aria-label="To"
            className="w-auto"
            value={search.to ?? ""}
            min={search.from}
            onChange={(event) => setSearch({ to: event.target.value || undefined })}
          />
          {(search.from || search.to) && (
            <Button variant="ghost" size="sm" onClick={() => setSearch({ from: undefined, to: undefined })}>
              All time
            </Button>
          )}
        </div>
        <Button variant="ghost" size="icon" className="order-2 sm:order-last" aria-label="Log out" title="Log out" onClick={logout}>
          <LogOut />
        </Button>
      </header>

      {error && !loggedOut && (
        <div role="alert" className="rounded-md border border-destructive/40 px-4 py-3 text-sm text-destructive">
          Could not load the report: {error.message}{" "}
          {series.error instanceof ApiError && series.error.status === 404 && (
            <Link to="/" search={{ ...search, level: "global", id: "all" }} className="underline">
              Back to all orders
            </Link>
          )}
        </div>
      )}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <Card className="max-h-[70svh] min-h-0 gap-3 pb-2 lg:max-h-none">
          <CardHeader>
            <CardTitle>Categories</CardTitle>
            <CardDescription>Spend, and share of the level above</CardDescription>
          </CardHeader>
          {hierarchy.data ? (
            <HierarchyTree
              root={hierarchy.data}
              selected={nodeKey({ level, id })}
              ancestors={ancestors}
              onSelect={(node) => setSearch({ level: node.level, id: node.id })}
            />
          ) : (
            <div className="space-y-2 px-5 pb-3">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-7" />
              ))}
            </div>
          )}
        </Card>

        <div className="flex min-w-0 flex-col gap-4 lg:overflow-y-auto">
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem>
                {path.length ? (
                  <Link to="/" search={{ ...search, level: "global", id: "all" }} className="hover:text-foreground">
                    All orders
                  </Link>
                ) : (
                  <BreadcrumbPage>All orders</BreadcrumbPage>
                )}
              </BreadcrumbItem>
              {path.map((crumb, index) => (
                <Fragment key={nodeKey(crumb)}>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    {index === path.length - 1 ? (
                      <BreadcrumbPage>{nodeName(crumb)}</BreadcrumbPage>
                    ) : (
                      <Link to="/" search={{ ...search, level: crumb.level, id: crumb.id }} className="hover:text-foreground">
                        {nodeName(crumb)}
                      </Link>
                    )}
                  </BreadcrumbItem>
                </Fragment>
              ))}
            </BreadcrumbList>
          </Breadcrumb>

          {series.data ? (
            <KpiTiles summary={series.data.summary} />
          ) : (
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
          )}

          <Card>
            <CardHeader className="flex-row flex-wrap items-start justify-between gap-3">
              <div className="flex flex-col gap-1">
                <CardTitle>
                  {METRICS[metric].label} per {bucket}
                </CardTitle>
                <CardDescription>{nodeName(current)}</CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <ToggleGroup type="single" aria-label="Metric" value={metric} onValueChange={(v) => v && setSearch({ metric: v as Metric })}>
                  {metrics.map((m) => (
                    <ToggleGroupItem key={m} value={m}>
                      {METRICS[m].label}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
                <ToggleGroup type="single" aria-label="Interval" value={bucket} onValueChange={(v) => v && setSearch({ bucket: v as Bucket })}>
                  <ToggleGroupItem value="week">Week</ToggleGroupItem>
                  <ToggleGroupItem value="month">Month</ToggleGroupItem>
                </ToggleGroup>
                <ToggleGroup type="single" aria-label="View" value={view} onValueChange={(v) => v && setView(v as typeof view)}>
                  <ToggleGroupItem value="chart">Chart</ToggleGroupItem>
                  <ToggleGroupItem value="table">Table</ToggleGroupItem>
                </ToggleGroup>
              </div>
            </CardHeader>
            <CardContent>
              {!series.data ? (
                <Skeleton className="h-80" />
              ) : series.data.buckets.length === 0 ? (
                <div className="flex h-80 items-center justify-center text-sm text-muted-foreground">
                  No orders in this period.
                </div>
              ) : view === "chart" ? (
                <OrdersChart buckets={series.data.buckets} bucket={bucket} metric={metric} />
              ) : (
                <OrdersTable buckets={series.data.buckets} bucket={bucket} />
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

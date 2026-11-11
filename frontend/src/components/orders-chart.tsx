import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { Bucket, BucketTotals } from "@/lib/api";
import { formatBucket, formatBucketLong, formatCount, formatMoney, formatMoneyShort } from "@/lib/format";

export type Metric = "spend" | "units" | "orders" | "price";

export const METRICS: Record<Metric, { label: string; money: boolean }> = {
  spend: { label: "Spend", money: true },
  units: { label: "Units", money: false },
  orders: { label: "Orders", money: false },
  price: { label: "Unit price", money: true },
};

/** A bucket's value for a metric; the unit price is undefined without units. */
export function metricValue(bucket: BucketTotals, metric: Metric): number | null {
  if (metric === "price") return bucket.units > 0 ? Math.round(bucket.spend / bucket.units) : null;
  return bucket[metric];
}

interface Props {
  buckets: BucketTotals[];
  bucket: Bucket;
  metric: Metric;
}

export function OrdersChart({ buckets, bucket, metric }: Props) {
  const { label, money } = METRICS[metric];
  const config = { value: { label, color: "var(--chart-1)" } } satisfies ChartConfig;
  const data = buckets.map((b) => ({ start: b.start, value: metricValue(b, metric) }));
  const formatValue = (value: number) => (money ? formatMoney(value) : formatCount(value));
  // Whole euros repeat on the axis ("€1, €1, €2") when the values are small.
  const max = Math.max(0, ...data.map((d) => d.value ?? 0));
  const formatTick = (value: number) =>
    money ? (max < 2000 ? formatMoney(value) : formatMoneyShort(value)) : formatCount(value);

  const axes = (
    <>
      <CartesianGrid vertical={false} />
      <XAxis
        dataKey="start"
        tickLine={false}
        axisLine={false}
        tickMargin={8}
        minTickGap={24}
        tickFormatter={(start: string) => formatBucket(start, bucket)}
      />
      <YAxis
        tickLine={false}
        axisLine={false}
        width={64}
        allowDecimals={money}
        tickFormatter={formatTick}
      />
      <ChartTooltip
        content={
          <ChartTooltipContent
            labelFormatter={(start) => formatBucketLong(start, bucket)}
            valueFormatter={formatValue}
          />
        }
      />
    </>
  );

  return (
    <ChartContainer config={config} className="aspect-auto h-80 w-full">
      {metric === "price" ? (
        // A price is a level, not an amount: a line, so the axis need not start at zero.
        <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }} accessibilityLayer>
          {axes}
          <Line
            dataKey="value"
            type="linear"
            stroke="var(--color-value)"
            strokeWidth={2}
            connectNulls
            dot={{ r: 4, fill: "var(--color-value)", stroke: "var(--card)", strokeWidth: 2 }}
            activeDot={{ r: 5 }}
            isAnimationActive={false}
          />
        </LineChart>
      ) : (
        <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }} accessibilityLayer>
          {axes}
          <Bar dataKey="value" fill="var(--color-value)" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} />
        </BarChart>
      )}
    </ChartContainer>
  );
}

export function OrdersTable({ buckets, bucket }: { buckets: BucketTotals[]; bucket: Bucket }) {
  return (
    <div className="max-h-80 overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-card text-left text-muted-foreground">
          <tr className="border-b">
            <th className="py-2 pr-3 font-medium">{bucket === "month" ? "Month" : "Week"}</th>
            <th className="px-3 py-2 text-right font-medium">Spend</th>
            <th className="px-3 py-2 text-right font-medium">Units</th>
            <th className="py-2 pl-3 text-right font-medium">Orders</th>
          </tr>
        </thead>
        <tbody>
          {[...buckets].reverse().map((b) => (
            <tr key={b.start} className="border-b last:border-0">
              <td className="py-1.5 pr-3">{formatBucketLong(b.start, bucket)}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">{formatMoney(b.spend)}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">{formatCount(b.units)}</td>
              <td className="py-1.5 pl-3 text-right tabular-nums">{formatCount(b.orders)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

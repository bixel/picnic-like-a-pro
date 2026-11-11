import * as React from "react";
import * as RechartsPrimitive from "recharts";

import { cn } from "@/lib/utils";

// shadcn/ui chart: a Recharts container that maps a config to CSS variables
// (--color-<key>) and restyles Recharts' axes, grid and cursor to the theme.

export type ChartConfig = Record<string, { label: React.ReactNode; color?: string }>;

const ChartContext = React.createContext<{ config: ChartConfig } | null>(null);

function useChart() {
  const context = React.useContext(ChartContext);
  if (!context) throw new Error("useChart must be used within a <ChartContainer />");
  return context;
}

function ChartContainer({
  config,
  className,
  children,
  style,
  ...props
}: React.ComponentProps<"div"> & {
  config: ChartConfig;
  children: React.ComponentProps<typeof RechartsPrimitive.ResponsiveContainer>["children"];
}) {
  const colors = Object.fromEntries(
    Object.entries(config)
      .filter(([, item]) => item.color)
      .map(([key, item]) => [`--color-${key}`, item.color]),
  );
  return (
    <ChartContext.Provider value={{ config }}>
      <div
        data-slot="chart"
        style={{ ...colors, ...style } as React.CSSProperties}
        className={cn(
          "flex aspect-video justify-center text-xs [&_.recharts-cartesian-axis-tick_text]:fill-muted-foreground [&_.recharts-cartesian-grid_line]:stroke-border [&_.recharts-rectangle.recharts-tooltip-cursor]:fill-muted [&_.recharts-curve.recharts-tooltip-cursor]:stroke-border [&_.recharts-surface]:outline-hidden [&_.recharts-layer]:outline-hidden",
          className,
        )}
        {...props}
      >
        <RechartsPrimitive.ResponsiveContainer>{children}</RechartsPrimitive.ResponsiveContainer>
      </div>
    </ChartContext.Provider>
  );
}

const ChartTooltip = RechartsPrimitive.Tooltip;

interface TooltipItem {
  dataKey?: string | number;
  name?: string | number;
  value?: number | string;
  color?: string;
}

function ChartTooltipContent({
  active,
  payload,
  label,
  labelFormatter,
  valueFormatter,
}: {
  active?: boolean;
  payload?: TooltipItem[];
  label?: string | number;
  labelFormatter?: (label: string) => React.ReactNode;
  valueFormatter?: (value: number, key: string) => React.ReactNode;
}) {
  const { config } = useChart();
  if (!active || !payload?.length) return null;
  return (
    <div className="grid min-w-36 gap-1.5 rounded-lg border bg-card px-2.5 py-1.5 text-xs shadow-xl">
      <div className="font-medium">{labelFormatter ? labelFormatter(String(label)) : label}</div>
      {payload.map((item) => {
        const key = String(item.dataKey ?? item.name);
        return (
          <div key={key} className="flex items-center gap-2">
            <span className="size-2.5 shrink-0 rounded-[2px]" style={{ background: `var(--color-${key})` }} />
            <span className="text-muted-foreground">{config[key]?.label ?? key}</span>
            <span className="ml-auto pl-3 font-medium tabular-nums">
              {valueFormatter ? valueFormatter(Number(item.value), key) : item.value}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export { ChartContainer, ChartTooltip, ChartTooltipContent };

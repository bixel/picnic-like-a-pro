import { Card } from "@/components/ui/card";
import type { Summary } from "@/lib/api";
import { formatCount, formatDay, formatMoney } from "@/lib/format";

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card className="gap-1 px-5 py-4">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="min-h-4 text-xs text-muted-foreground">{hint}</div>
    </Card>
  );
}

export function KpiTiles({ summary }: { summary: Summary }) {
  const perOrder = summary.orders > 0 ? formatMoney(summary.spend / summary.orders) : "—";
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <Tile
        label="Spend"
        value={formatMoney(summary.spend)}
        hint={
          summary.order_total != null
            ? `${formatMoney(summary.order_total)} incl. fees and deposits`
            : "Sum of the order lines"
        }
      />
      <Tile label="Orders" value={formatCount(summary.orders)} hint={`First ${formatDay(summary.first_order)}`} />
      <Tile label="Units" value={formatCount(summary.units)} hint={`Last ${formatDay(summary.last_order)}`} />
      <Tile label="Spend per order" value={perOrder} />
    </div>
  );
}

import { PanelShell } from "../../components/PanelShell";
import type { Order } from "../../types/models";
import { cx } from "../../utils/formatters";

const sourceTone = {
  system: "bg-sky-500/15 text-sky-200",
  bot: "bg-emerald-500/15 text-emerald-200",
  manual: "bg-amber-500/15 text-amber-200",
  external: "bg-violet-500/15 text-violet-200",
};

export function OrdersPanel({
  orders,
  selectedOrderId,
  onSelect,
}: {
  orders: Order[];
  selectedOrderId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <PanelShell title="Orders" accent="green">
      <table className="min-w-full text-xs">
        <thead className="sticky top-0 bg-chrome-850 text-slate-500">
          <tr>
            <th className="px-3 py-2 text-left">Order</th>
            <th className="px-3 py-2 text-left">Qty</th>
            <th className="px-3 py-2 text-left">Fill</th>
            <th className="px-3 py-2 text-left">Status</th>
            <th className="px-3 py-2 text-left">Source</th>
            <th className="px-3 py-2 text-left">Age</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr
              key={order.id}
              onClick={() => onSelect(order.id)}
              className={cx("cursor-pointer border-b border-white/5", order.id === selectedOrderId ? "bg-accent-blue/10" : "hover:bg-white/4")}
            >
              <td className="px-3 py-2">
                <div className="font-semibold text-slate-100">{order.symbol}</div>
                <div className="font-mono text-[11px] text-slate-500">{order.side} {order.limitPrice.toFixed(2)}</div>
              </td>
              <td className="px-3 py-2 text-slate-300">{order.qty}</td>
              <td className="px-3 py-2 text-slate-300">{order.filledQty}/{order.qty}</td>
              <td className="px-3 py-2">
                <span className={cx("rounded px-2 py-1 font-medium", order.status === "Rejected" ? "bg-accent-red/15 text-accent-red" : order.status === "PartialFill" ? "bg-accent-amber/15 text-accent-amber" : order.status === "Filled" ? "bg-accent-green/15 text-accent-green" : "bg-white/8 text-slate-200")}>
                  {order.status}
                </span>
              </td>
              <td className="px-3 py-2">
                <span className={cx("rounded px-2 py-1", sourceTone[order.source])}>{order.source}</span>
              </td>
              <td className="px-3 py-2 font-mono text-slate-500">{order.ageSec}s</td>
            </tr>
          ))}
        </tbody>
      </table>
    </PanelShell>
  );
}

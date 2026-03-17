import { PanelShell } from "../../components/PanelShell";
import type { AlertItem, Order } from "../../types/models";
import { formatTime } from "../../utils/formatters";

export function DetailsPanel({
  order,
  alert,
}: {
  order?: Order;
  alert?: AlertItem;
}) {
  return (
    <PanelShell title="Detail View" accent="blue">
      <div className="space-y-4 p-3 text-sm">
        <section className="rounded border border-white/6 bg-black/15 p-3">
          <div className="text-[11px] uppercase tracking-[0.2em] text-slate-500">Selected Order</div>
          {order ? (
            <div className="mt-3 space-y-2 text-slate-200">
              <div className="flex justify-between"><span>Order ID</span><span className="font-mono">{order.id}</span></div>
              <div className="flex justify-between"><span>Instrument</span><span>{order.symbol}</span></div>
              <div className="flex justify-between"><span>Route</span><span>{order.route}</span></div>
              <div className="flex justify-between"><span>Status</span><span>{order.status}</span></div>
              <div className="flex justify-between"><span>Fill Progress</span><span>{order.filledQty} / {order.qty}</span></div>
            </div>
          ) : (
            <div className="mt-2 text-slate-500">No order selected.</div>
          )}
        </section>
        <section className="rounded border border-white/6 bg-black/15 p-3">
          <div className="text-[11px] uppercase tracking-[0.2em] text-slate-500">Selected Alert</div>
          {alert ? (
            <div className="mt-3 space-y-2 text-slate-200">
              <div className="font-semibold">{alert.title}</div>
              <div className="text-xs text-slate-400">{formatTime(alert.timestamp)}</div>
              <div>{alert.detail}</div>
            </div>
          ) : (
            <div className="mt-2 text-slate-500">No alert selected.</div>
          )}
        </section>
      </div>
    </PanelShell>
  );
}

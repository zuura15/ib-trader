import { PanelShell } from "../../components/PanelShell";
import type { Position } from "../../types/models";
import { cx, formatCurrency, formatSigned } from "../../utils/formatters";

export function PositionsPanel({ positions }: { positions: Position[] }) {
  return (
    <PanelShell title="Positions" accent="blue">
      <table className="min-w-full text-xs">
        <thead className="sticky top-0 bg-chrome-850 text-slate-500">
          <tr>
            <th className="px-3 py-2 text-left">Symbol</th>
            <th className="px-3 py-2 text-left">Qty</th>
            <th className="px-3 py-2 text-left">Avg</th>
            <th className="px-3 py-2 text-left">Mark</th>
            <th className="px-3 py-2 text-left">U-P&L</th>
            <th className="px-3 py-2 text-left">R-P&L</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.symbol} className="border-b border-white/5">
              <td className="px-3 py-2 font-semibold text-slate-100">{position.symbol}</td>
              <td className={cx("px-3 py-2", position.qty >= 0 ? "text-slate-200" : "text-accent-amber")}>{position.qty}</td>
              <td className="px-3 py-2 text-slate-300">{formatCurrency(position.avgCost)}</td>
              <td className="px-3 py-2 text-slate-300">{formatCurrency(position.mark)}</td>
              <td className={cx("px-3 py-2 font-medium", position.unrealizedPnl >= 0 ? "text-accent-green" : "text-accent-red")}>{formatSigned(position.unrealizedPnl)}</td>
              <td className={cx("px-3 py-2 font-medium", position.realizedPnl >= 0 ? "text-accent-green" : "text-accent-red")}>{formatSigned(position.realizedPnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </PanelShell>
  );
}

import type { HeaderStatus, VariantId } from "../../types/models";
import { cx, formatSigned } from "../../utils/formatters";

const statusTone = {
  connected: "text-accent-green",
  disconnected: "text-accent-red",
  reconnecting: "text-accent-amber",
};

export function HeaderBar({
  header,
  variant,
  onVariantChange,
}: {
  header: HeaderStatus;
  variant: VariantId;
  onVariantChange: (variant: VariantId) => void;
}) {
  const chips = [
    ["IB", header.ibConnection, statusTone[header.ibConnection]],
    ["Session", header.sessionConnectivity, statusTone[header.sessionConnectivity]],
    ["Account", header.accountMode, header.accountMode === "live" ? "text-accent-amber" : "text-accent-blue"],
    ["Health", header.serviceHealth, header.serviceHealth === "healthy" ? "text-accent-green" : header.serviceHealth === "degraded" ? "text-accent-amber" : "text-accent-red"],
    ["Data", header.staleData ? "stale" : "fresh", header.staleData ? "text-accent-amber" : "text-accent-green"],
  ] as const;

  const variants: Array<{ id: VariantId; label: string }> = [
    { id: "A", label: "Classic" },
    { id: "B", label: "Modern" },
    { id: "C", label: "Command" },
    { id: "D", label: "Bots" },
  ];

  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-white/5 bg-chrome-950 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-4">
        <div>
          <div className="text-[11px] uppercase tracking-[0.24em] text-slate-500">IB Control Center</div>
          <div className="text-sm font-semibold text-white">Trading Workstation Prototype</div>
        </div>
        <div className="flex flex-wrap gap-2">
          {chips.map(([label, value, tone]) => (
            <div key={label} className="rounded border border-white/8 bg-chrome-900 px-2.5 py-1">
              <span className="mr-2 text-[10px] uppercase tracking-[0.2em] text-slate-500">{label}</span>
              <span className={cx("text-xs font-medium capitalize", tone)}>{value}</span>
            </div>
          ))}
          <div className={cx("rounded border px-2.5 py-1", header.warningState ? "border-accent-amber/40 bg-accent-amber/10" : "border-white/8 bg-chrome-900")}>
            <span className="mr-2 text-[10px] uppercase tracking-[0.2em] text-slate-500">Warnings</span>
            <span className={cx("text-xs font-medium", header.warningState ? "text-accent-amber" : "text-slate-300")}>{header.warningState ? "active" : "clear"}</span>
          </div>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-3">
        <div className="rounded border border-white/8 bg-chrome-900 px-3 py-1.5">
          <div className="text-[10px] uppercase tracking-[0.2em] text-slate-500">P&L Snapshot</div>
          <div className={cx("text-sm font-semibold", header.pnl >= 0 ? "text-accent-green" : "text-accent-red")}>{formatSigned(header.pnl)}</div>
        </div>
        <div className="rounded border border-white/8 bg-chrome-900 p-1">
          <div className="mb-1 px-2 text-[10px] uppercase tracking-[0.2em] text-slate-500">Layout Variant</div>
          <div className="flex gap-1">
            {variants.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onVariantChange(item.id)}
                className={cx(
                  "rounded px-2.5 py-1.5 text-xs font-medium",
                  variant === item.id
                    ? "bg-accent-blue text-chrome-950"
                    : "bg-chrome-850 text-slate-300 hover:bg-chrome-800",
                )}
              >
                {item.id} · {item.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </header>
  );
}

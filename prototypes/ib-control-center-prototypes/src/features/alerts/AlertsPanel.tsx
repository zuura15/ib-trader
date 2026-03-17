import { PanelShell } from "../../components/PanelShell";
import type { AlertItem } from "../../types/models";
import { cx, formatTime } from "../../utils/formatters";

export function AlertsPanel({
  alerts,
  selectedAlertId,
  onSelect,
}: {
  alerts: AlertItem[];
  selectedAlertId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <PanelShell title="Alerts" accent="red">
      <div className="space-y-2 p-3">
        {alerts.map((alert) => (
          <button
            key={alert.id}
            type="button"
            onClick={() => onSelect(alert.id)}
            className={cx(
              "w-full rounded border p-3 text-left",
              alert.id === selectedAlertId ? "border-accent-red/40 bg-accent-red/10" : "border-white/6 bg-black/15",
            )}
          >
            <div className="flex items-center justify-between">
              <div className={cx("text-sm font-semibold", alert.severity === "error" ? "text-accent-red" : alert.severity === "warning" ? "text-accent-amber" : "text-accent-blue")}>
                {alert.title}
              </div>
              <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">{alert.state}</div>
            </div>
            <div className="mt-2 text-xs text-slate-300">{alert.detail}</div>
            <div className="mt-2 text-[11px] font-mono text-slate-500">{formatTime(alert.timestamp)}</div>
          </button>
        ))}
      </div>
    </PanelShell>
  );
}

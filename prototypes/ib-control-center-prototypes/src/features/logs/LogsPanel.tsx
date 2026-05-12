import { PanelShell } from "../../components/PanelShell";
import type { LogEntry } from "../../types/models";
import { cx, formatTime } from "../../utils/formatters";

export function LogsPanel({ logs }: { logs: LogEntry[] }) {
  return (
    <PanelShell title="Log / Event Stream" accent="amber">
      <div className="divide-y divide-white/5">
        {logs.map((log) => (
          <div key={log.id} className="grid grid-cols-[84px_72px_92px_1fr] gap-3 px-3 py-2 text-xs">
            <div className="font-mono text-slate-500">{formatTime(log.timestamp)}</div>
            <div className={cx("font-semibold uppercase tracking-[0.18em]", log.severity === "error" ? "text-accent-red" : log.severity === "warning" ? "text-accent-amber" : log.severity === "success" ? "text-accent-green" : "text-accent-blue")}>
              {log.severity}
            </div>
            <div className="text-slate-400">{log.source}</div>
            <div className="text-slate-200">{log.message}</div>
          </div>
        ))}
      </div>
    </PanelShell>
  );
}

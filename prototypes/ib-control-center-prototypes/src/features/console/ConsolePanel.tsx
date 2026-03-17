import { PanelShell } from "../../components/PanelShell";
import type { CommandEntry } from "../../types/models";
import { cx, formatTime } from "../../utils/formatters";

const stateTone = {
  queued: "text-accent-amber",
  running: "text-accent-blue",
  success: "text-accent-green",
  failure: "text-accent-red",
  idle: "text-slate-400",
};

export function ConsolePanel({ commands }: { commands: CommandEntry[] }) {
  return (
    <PanelShell
      title="Command Console"
      accent="blue"
      right={<span className="font-mono text-[11px] text-slate-500">prompt ready</span>}
    >
      <div className="flex h-full flex-col">
        <div className="border-b border-white/5 bg-chrome-850 px-3 py-2 font-mono text-sm text-white">
          <span className="mr-2 text-accent-green">ops@ibcc</span>
          <span className="text-slate-500">$</span>
          <span className="ml-2 text-slate-200">orders status --watch</span>
        </div>
        <div className="flex-1 space-y-3 p-3">
          {commands.map((command) => (
            <article key={command.id} className="rounded border border-white/6 bg-black/20 p-3">
              <div className="flex items-center justify-between">
                <div className="font-mono text-sm text-slate-100">{command.command}</div>
                <div className={cx("text-xs font-semibold uppercase tracking-[0.18em]", stateTone[command.state])}>{command.state}</div>
              </div>
              <div className="mt-2 text-xs text-slate-500">{formatTime(command.startedAt)}</div>
              <div className="mt-2 rounded bg-chrome-950 px-3 py-2 font-mono text-xs text-slate-300">{command.response}</div>
            </article>
          ))}
        </div>
      </div>
    </PanelShell>
  );
}

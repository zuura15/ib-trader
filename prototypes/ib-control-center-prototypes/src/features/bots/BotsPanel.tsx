import { PanelShell } from "../../components/PanelShell";
import type { BotItem } from "../../types/models";
import { cx, formatSigned } from "../../utils/formatters";

export function BotsPanel({ bots, compact = false }: { bots: BotItem[]; compact?: boolean }) {
  return (
    <PanelShell title="Bots / Automation" accent="green">
      <div className={cx("grid gap-3 p-3", compact ? "grid-cols-1" : "grid-cols-1 xl:grid-cols-2")}>
        {bots.map((bot) => (
          <article key={bot.id} className={cx("rounded border p-3", bot.errorState ? "border-accent-red/35 bg-accent-red/10" : "border-white/6 bg-black/15")}>
            <div className="flex items-start justify-between">
              <div>
                <div className="text-sm font-semibold text-slate-100">{bot.name}</div>
                <div className="text-xs text-slate-500">{bot.strategy}</div>
              </div>
              <span className={cx("rounded px-2 py-1 text-[11px] uppercase tracking-[0.18em]", bot.mode === "active" ? "bg-accent-green/15 text-accent-green" : bot.mode === "degraded" ? "bg-accent-amber/15 text-accent-amber" : "bg-accent-red/15 text-accent-red")}>
                {bot.mode}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
              <div>
                <div className="text-slate-500">Heartbeat</div>
                <div className={cx("font-mono text-sm", bot.heartbeatSec > 20 ? "text-accent-red" : "text-slate-200")}>{bot.heartbeatSec}s ago</div>
              </div>
              <div>
                <div className="text-slate-500">Session P&L</div>
                <div className={cx("font-semibold text-sm", bot.pnl >= 0 ? "text-accent-green" : "text-accent-red")}>{formatSigned(bot.pnl)}</div>
              </div>
              <div className="col-span-2">
                <div className="text-slate-500">Last Signal</div>
                <div className="text-slate-200">{bot.lastSignal}</div>
              </div>
              <div className="col-span-2">
                <div className="text-slate-500">Last Action</div>
                <div className="text-slate-200">{bot.lastAction}</div>
              </div>
            </div>
          </article>
        ))}
      </div>
    </PanelShell>
  );
}

import type { ReactNode } from "react";

export function PanelShell({
  title,
  accent,
  children,
  right,
}: {
  title: string;
  accent?: "blue" | "green" | "amber" | "red";
  children: ReactNode;
  right?: ReactNode;
}) {
  const accentMap = {
    blue: "bg-accent-blue",
    green: "bg-accent-green",
    amber: "bg-accent-amber",
    red: "bg-accent-red",
  };

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-md border border-white/5 bg-chrome-900 shadow-panel">
      <header className="flex items-center justify-between border-b border-white/5 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${accent ? accentMap[accent] : "bg-white/30"}`} />
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-300">{title}</h2>
        </div>
        {right}
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

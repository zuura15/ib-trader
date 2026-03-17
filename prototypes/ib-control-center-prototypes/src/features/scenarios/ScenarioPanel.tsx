import { PanelShell } from "../../components/PanelShell";
import type { ScenarioDefinition } from "../../types/models";
import { cx } from "../../utils/formatters";

export function ScenarioPanel({
  scenarios,
  activeScenario,
  onApply,
}: {
  scenarios: ScenarioDefinition[];
  activeScenario: string;
  onApply: (id: string) => void;
}) {
  return (
    <PanelShell title="Scenario Controls" accent="amber">
      <div className="space-y-2 p-3">
        {scenarios.map((scenario) => (
          <button
            key={scenario.id}
            type="button"
            onClick={() => onApply(scenario.id)}
            className={cx(
              "w-full rounded border p-3 text-left transition-colors",
              activeScenario === scenario.id ? "border-accent-blue/35 bg-accent-blue/10" : "border-white/6 bg-black/15 hover:bg-white/5",
            )}
          >
            <div className="text-sm font-semibold text-slate-100">{scenario.label}</div>
            <div className="mt-1 text-xs text-slate-400">{scenario.description}</div>
          </button>
        ))}
      </div>
    </PanelShell>
  );
}

import { useEffect, useState } from "react";
import { Layout, Model, TabNode } from "flexlayout-react";
import "flexlayout-react/style/dark.css";
import { HeaderBar } from "../features/header/HeaderBar";
import { ConsolePanel } from "../features/console/ConsolePanel";
import { LogsPanel } from "../features/logs/LogsPanel";
import { OrdersPanel } from "../features/orders/OrdersPanel";
import { PositionsPanel } from "../features/positions/PositionsPanel";
import { AlertsPanel } from "../features/alerts/AlertsPanel";
import { BotsPanel } from "../features/bots/BotsPanel";
import { ScenarioPanel } from "../features/scenarios/ScenarioPanel";
import { DetailsPanel } from "../features/details/DetailsPanel";
import { layoutByVariant } from "../layout/variants";
import { scenarios } from "../mock/scenarios";
import type { VariantId } from "../types/models";
import { useWorkstationState } from "./useWorkstationState";

export function App() {
  const [variant, setVariant] = useState<VariantId>("A");
  const [model, setModel] = useState(() => Model.fromJson(layoutByVariant.A));
  const { state, selectedOrder, selectedAlert, setSelectedOrderId, setSelectedAlertId, applyScenarioById } =
    useWorkstationState();

  useEffect(() => {
    setModel(Model.fromJson(layoutByVariant[variant]));
  }, [variant]);

  const factory = (node: TabNode) => {
    const component = node.getComponent();

    switch (component) {
      case "console":
        return <ConsolePanel commands={state.commands} />;
      case "logs":
        return <LogsPanel logs={state.logs} />;
      case "orders":
        return <OrdersPanel orders={state.orders} selectedOrderId={state.selectedOrderId} onSelect={setSelectedOrderId} />;
      case "positions":
        return <PositionsPanel positions={state.positions} />;
      case "alerts":
        return <AlertsPanel alerts={state.alerts} selectedAlertId={state.selectedAlertId} onSelect={setSelectedAlertId} />;
      case "bots":
        return <BotsPanel bots={state.bots} compact={variant !== "D"} />;
      case "scenarios":
        return <ScenarioPanel scenarios={scenarios} activeScenario={state.activeScenario} onApply={applyScenarioById} />;
      case "details":
        return <DetailsPanel order={selectedOrder} alert={selectedAlert} />;
      default:
        return <div />;
    }
  };

  return (
    <div className="flex h-screen flex-col bg-chrome-950 text-slate-100">
      <HeaderBar header={state.header} variant={variant} onVariantChange={setVariant} />
      <main className="min-h-0 flex-1 p-2">
        <div className="relative h-full overflow-hidden rounded-lg border border-white/5 bg-chrome-900">
          <Layout model={model} factory={factory} realtimeResize />
        </div>
      </main>
    </div>
  );
}

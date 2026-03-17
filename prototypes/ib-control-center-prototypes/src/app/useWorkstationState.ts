import { useEffect, useMemo, useState } from "react";
import { createInitialState } from "../data/seed";
import { applyScenario } from "../mock/scenarios";
import type { CommandEntry, LogEntry, WorkstationState } from "../types/models";

const randomFrom = <T,>(items: readonly T[]) => items[Math.floor(Math.random() * items.length)];

export function useWorkstationState() {
  const [state, setState] = useState<WorkstationState>(() => createInitialState());

  useEffect(() => {
    const timer = window.setInterval(() => {
      setState((current) => {
        const logTemplates = [
          ["info", "engine", "Order health check completed without variance."],
          ["warning", "broker", "Broker pacing limit nearing threshold on market data channel."],
          ["info", "supervisor", "Bot basket rebalance evaluation completed."],
          ["info", "gateway", "Account summary delta received."],
        ] as const;
        const [severity, source, message] = randomFrom(logTemplates);

        const nextLogs: LogEntry[] = [
          {
            id: crypto.randomUUID(),
            timestamp: new Date().toISOString(),
            severity,
            source,
            message,
          },
          ...current.logs,
        ].slice(0, 24);

        const nextOrders = current.orders.map((order, index) => ({
          ...order,
          ageSec: order.ageSec + 3,
          filledQty:
            order.status === "Submitted" && index % 2 === 0 ? Math.min(order.qty, order.filledQty + 5) : order.filledQty,
        }));

        const nextBots = current.bots.map((bot, index) => ({
          ...bot,
          heartbeatSec: current.activeScenario === "bot-heartbeat-missing" && index === 0 ? bot.heartbeatSec + 3 : Math.max(1, ((bot.heartbeatSec + 1) % 12) || 1),
          pnl: Number((bot.pnl + (index === 0 ? 18 : index === 1 ? -7 : 5)).toFixed(2)),
        }));

        const nextCommands: CommandEntry[] = current.commands.map((command, index) => {
          if (command.state !== "running") {
            return command;
          }
          if (index === 0) {
            return {
              ...command,
              state: "success",
              finishedAt: new Date().toISOString(),
              response: "Command completed. Broker ack and local state are aligned.",
            };
          }
          return command;
        });

        return {
          ...current,
          logs: nextLogs,
          orders: nextOrders.map((order) =>
            order.filledQty > 0 && order.filledQty < order.qty ? { ...order, status: "PartialFill" } : order,
          ),
          bots: nextBots,
          commands: nextCommands,
          header: {
            ...current.header,
            pnl: Number(
              (
                current.positions.reduce((sum, position) => sum + position.unrealizedPnl + position.realizedPnl, 0) +
                nextBots.reduce((sum, bot) => sum + bot.pnl, 0) * 0.2
              ).toFixed(2),
            ),
          },
        };
      });
    }, 3000);

    return () => window.clearInterval(timer);
  }, []);

  const selectedOrder = useMemo(
    () => state.orders.find((order) => order.id === state.selectedOrderId),
    [state.orders, state.selectedOrderId],
  );
  const selectedAlert = useMemo(
    () => state.alerts.find((alert) => alert.id === state.selectedAlertId),
    [state.alerts, state.selectedAlertId],
  );

  return {
    state,
    selectedOrder,
    selectedAlert,
    setSelectedOrderId: (id: string) => setState((current) => ({ ...current, selectedOrderId: id })),
    setSelectedAlertId: (id: string) => setState((current) => ({ ...current, selectedAlertId: id })),
    applyScenarioById: (id: string) => setState((current) => applyScenario(current, id)),
  };
}

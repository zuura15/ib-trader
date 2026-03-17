import type { BotItem, CommandEntry, HeaderStatus, LogEntry, Order, ScenarioDefinition, WorkstationState } from "../types/models";

const withLog = (state: WorkstationState, severity: "info" | "warning" | "error", source: string, message: string) => ({
  ...state,
  logs: [
    {
      id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      severity,
      source,
      message,
    },
    ...state.logs,
  ].slice(0, 24),
});

const upsertAlert = (
  state: WorkstationState,
  id: string,
  title: string,
  detail: string,
  severity: "info" | "warning" | "error",
) => ({
  ...state,
  alerts: [
    {
      id,
      title,
      detail,
      severity,
      state: "active" as const,
      timestamp: new Date().toISOString(),
    },
    ...state.alerts.filter((alert) => alert.id !== id),
  ],
  selectedAlertId: id,
});

export const scenarios: ScenarioDefinition[] = [
  { id: "healthy", label: "Healthy State", description: "Nominal connectivity, paper mode, healthy bots, normal flow." },
  { id: "ib-disconnected", label: "IB Disconnected", description: "Loss of gateway connectivity and elevated risk messaging." },
  { id: "reconnecting", label: "Reconnecting", description: "Gateway attempting session recovery after transport loss." },
  { id: "paper-mode", label: "Paper Mode", description: "Explicit paper account posture and safer warning posture." },
  { id: "live-warning", label: "Live Account Warning", description: "Live account is enabled and warning surfaces must stand out." },
  { id: "command-running", label: "Command Running", description: "A long-running console action is in flight." },
  { id: "command-failure", label: "Command Failure", description: "A console command fails and emits remediation detail." },
  { id: "partial-fill", label: "Partial Fill", description: "Open order progress updates affect orders and logs." },
  { id: "order-rejection", label: "Order Rejection", description: "Rejected order surfaces across alerts, logs, and detail views." },
  { id: "broker-burst", label: "Broker Message Burst", description: "Rapid message stream increases noise and urgency." },
  { id: "stale-data", label: "Stale Data", description: "Market data freshness degrades and warnings activate." },
  { id: "bot-heartbeat-missing", label: "Bot Heartbeat Missing", description: "Automation supervision shows missing heartbeats." },
  { id: "recon-mismatch", label: "Reconciliation Mismatch", description: "Order and position reconciliation diverges from expected state." },
];

export const applyScenario = (state: WorkstationState, id: string): WorkstationState => {
  const base: WorkstationState = {
    ...state,
    activeScenario: id,
    header: {
      ...state.header,
      ibConnection: "connected",
      sessionConnectivity: "connected",
      accountMode: "paper",
      serviceHealth: "healthy",
      staleData: false,
      warningState: false,
    },
    commands: state.commands.map<CommandEntry>((command, index) =>
      index === 0 ? { ...command, state: "success", response: "Last command completed without warnings." } : command,
    ),
    bots: state.bots.map<BotItem>((bot, index) => ({
      ...bot,
      heartbeatSec: index === 2 ? 8 : index + 2,
      errorState: false,
      mode: index === 2 ? "degraded" : "active",
    })),
    orders: state.orders.map<Order>((order) =>
      order.id === "O-24014" ? { ...order, status: "PartialFill", filledQty: 60 } : { ...order },
    ),
  };

  switch (id) {
    case "ib-disconnected":
      return upsertAlert(
        withLog(
          {
            ...base,
            header: { ...base.header, ibConnection: "disconnected", sessionConnectivity: "reconnecting", serviceHealth: "critical", warningState: true } satisfies HeaderStatus,
          },
          "error",
          "gateway",
          "Lost broker session. Order status stream paused pending reconnect.",
        ),
        "A-ib-down",
        "IB connection lost",
        "Gateway heartbeat timed out. Order entry should be considered unavailable until session restoration.",
        "error",
      );
    case "reconnecting":
      return withLog(
          {
            ...base,
            header: { ...base.header, ibConnection: "reconnecting", sessionConnectivity: "reconnecting", serviceHealth: "degraded", warningState: true } satisfies HeaderStatus,
          },
        "warning",
        "gateway",
        "Reconnect handshake in progress. Last account snapshot age 21s.",
      );
    case "paper-mode":
      return withLog(base, "info", "risk", "Paper account mode asserted for this operator session.");
    case "live-warning":
      return upsertAlert(
        {
          ...base,
          header: { ...base.header, accountMode: "live", warningState: true, serviceHealth: "degraded" } satisfies HeaderStatus,
        },
        "A-live",
        "Live account armed",
        "Live trading mode is enabled. Confirm routing, account, and strategy limits before command execution.",
        "warning",
      );
    case "command-running":
      return withLog(
        {
          ...base,
          commands: base.commands.map<CommandEntry>((command, index) =>
            index === 0
              ? {
                  ...command,
                  command: "orders amend O-24015 --limit 417.40",
                  state: "running",
                  response: "Broker acknowledgement pending...",
                }
              : command,
          ),
        },
        "info",
        "console",
        "Awaiting broker acknowledgement for amend request O-24015.",
      );
    case "command-failure":
      return upsertAlert(
        withLog(
          {
            ...base,
            commands: base.commands.map<CommandEntry>((command, index) =>
              index === 0
                ? {
                    ...command,
                    command: "positions flatten --symbol NVDA",
                    state: "failure",
                    response: "Rejected: live account not armed for flatten permission set.",
                  }
                : command,
            ),
            header: { ...base.header, warningState: true, serviceHealth: "degraded" } satisfies HeaderStatus,
          },
          "error",
          "console",
          "Flatten command rejected by policy guard.",
        ),
        "A-cmd-fail",
        "Command execution failed",
        "Policy guard rejected the console action. Review live-routing and permission checks.",
        "error",
      );
    case "partial-fill":
      return withLog(
        {
          ...base,
          orders: base.orders.map<Order>((order) =>
            order.id === "O-24015" ? { ...order, status: "PartialFill", filledQty: 80 } : order,
          ),
          selectedOrderId: "O-24015",
        },
        "warning",
        "execution",
        "MSFT order O-24015 partially filled: 80 / 200 shares.",
      );
    case "order-rejection":
      return upsertAlert(
        withLog(
          {
            ...base,
            orders: base.orders.map<Order>((order) =>
              order.id === "O-24016" ? { ...order, status: "Rejected", filledQty: 0 } : order,
            ),
            selectedOrderId: "O-24016",
            header: { ...base.header, warningState: true, serviceHealth: "degraded" } satisfies HeaderStatus,
          },
          "error",
          "broker",
          "Order O-24016 rejected: price outside precautionary band.",
        ),
        "A-reject",
        "Order rejected",
        "AMD sell order O-24016 was rejected by the broker risk band filter.",
        "error",
      );
    case "broker-burst":
      return {
        ...base,
        logs: Array.from({ length: 7 }, (_, index): LogEntry => ({
          id: `burst-${index}`,
          timestamp: new Date(Date.now() - index * 1000).toISOString(),
          severity: index % 3 === 0 ? "warning" : "info",
          source: index % 2 === 0 ? "broker" : "gateway",
          message: index % 2 === 0 ? "Execution detail update received." : "Account value refresh received.",
        })).concat(base.logs).slice(0, 24),
      };
    case "stale-data":
      return upsertAlert(
        withLog(
          {
            ...base,
            header: { ...base.header, staleData: true, warningState: true, serviceHealth: "degraded" } satisfies HeaderStatus,
          },
          "warning",
          "mds",
          "Market data age exceeded freshness threshold for 18 seconds.",
        ),
        "A-stale",
        "Stale market data",
        "At least one pricing stream has exceeded the configured freshness budget.",
        "warning",
      );
    case "bot-heartbeat-missing":
      return upsertAlert(
        withLog(
          {
            ...base,
            header: { ...base.header, serviceHealth: "critical", warningState: true } satisfies HeaderStatus,
            bots: base.bots.map<BotItem>((bot, index) =>
              index === 0 ? { ...bot, heartbeatSec: 47, errorState: true, mode: "halted" } : bot,
            ),
          },
          "error",
          "supervisor",
          "Bot meanrev-eq heartbeat missing for 47 seconds.",
        ),
        "A-bot-miss",
        "Bot heartbeat missing",
        "Automation supervision lost heartbeat from meanrev-eq. Manual review recommended.",
        "error",
      );
    case "recon-mismatch":
      return upsertAlert(
        withLog(
          {
            ...base,
            header: { ...base.header, serviceHealth: "critical", warningState: true } satisfies HeaderStatus,
          },
          "error",
          "recon",
          "Position mismatch detected for NVDA between execution ledger and cached state.",
        ),
        "A-recon",
        "Reconciliation mismatch",
        "The engine reconciliation step found an unexpected NVDA quantity delta. Review fills and external activity.",
        "error",
      );
    default:
      return base;
  }
};

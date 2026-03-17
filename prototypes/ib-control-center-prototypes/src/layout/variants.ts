import type { IJsonModel } from "flexlayout-react";
import type { VariantId } from "../types/models";

const tab = (name: string, component: string) => ({ type: "tab", name, component });

export const layoutByVariant: Record<VariantId, IJsonModel> = {
  A: {
    global: { tabEnableRename: false, splitterSize: 6, tabSetEnableTabStrip: true },
    borders: [],
    layout: {
      type: "row",
      weight: 100,
      children: [
        {
          type: "tabset",
          weight: 22,
          children: [tab("Console", "console"), tab("Scenarios", "scenarios")],
        },
        {
          type: "column",
          weight: 56,
          children: [
            {
              type: "row",
              weight: 56,
              children: [
                { type: "tabset", weight: 36, children: [tab("Orders", "orders")] },
                { type: "tabset", weight: 34, children: [tab("Positions", "positions")] },
                { type: "tabset", weight: 30, children: [tab("Alerts", "alerts")] },
              ],
            },
            {
              type: "row",
              weight: 44,
              children: [
                { type: "tabset", weight: 58, children: [tab("Logs", "logs")] },
                { type: "tabset", weight: 42, children: [tab("Details", "details")] },
              ],
            },
          ],
        },
        {
          type: "tabset",
          weight: 22,
          children: [tab("Bots", "bots")],
        },
      ],
    },
  },
  B: {
    global: { tabEnableRename: false, splitterSize: 8, tabSetEnableTabStrip: true },
    borders: [
      {
        type: "border",
        location: "right",
        children: [tab("Scenarios", "scenarios"), tab("Details", "details")],
      },
    ],
    layout: {
      type: "row",
      weight: 100,
      children: [
        {
          type: "column",
          weight: 68,
          children: [
            { type: "tabset", weight: 58, children: [tab("Orders", "orders")] },
            { type: "tabset", weight: 42, children: [tab("Logs", "logs"), tab("Alerts", "alerts")] },
          ],
        },
        {
          type: "column",
          weight: 32,
          children: [
            { type: "tabset", weight: 46, children: [tab("Positions", "positions")] },
            { type: "tabset", weight: 54, children: [tab("Console", "console"), tab("Bots", "bots")] },
          ],
        },
      ],
    },
  },
  C: {
    global: { tabEnableRename: false, splitterSize: 8, tabSetEnableTabStrip: true },
    borders: [],
    layout: {
      type: "column",
      weight: 100,
      children: [
        {
          type: "row",
          weight: 70,
          children: [
            { type: "tabset", weight: 58, children: [tab("Command Console", "console")] },
            {
              type: "column",
              weight: 42,
              children: [
                { type: "tabset", weight: 50, children: [tab("Logs", "logs")] },
                { type: "tabset", weight: 50, children: [tab("Scenarios", "scenarios"), tab("Alerts", "alerts")] },
              ],
            },
          ],
        },
        {
          type: "row",
          weight: 30,
          children: [
            { type: "tabset", weight: 38, children: [tab("Orders", "orders")] },
            { type: "tabset", weight: 30, children: [tab("Positions", "positions")] },
            { type: "tabset", weight: 32, children: [tab("Details", "details"), tab("Bots", "bots")] },
          ],
        },
      ],
    },
  },
  D: {
    global: { tabEnableRename: false, splitterSize: 8, tabSetEnableTabStrip: true },
    borders: [],
    layout: {
      type: "row",
      weight: 100,
      children: [
        {
          type: "column",
          weight: 62,
          children: [
            { type: "tabset", weight: 62, children: [tab("Bot Supervision", "bots")] },
            { type: "tabset", weight: 38, children: [tab("Alerts", "alerts"), tab("Scenarios", "scenarios")] },
          ],
        },
        {
          type: "column",
          weight: 38,
          children: [
            { type: "tabset", weight: 34, children: [tab("Details", "details")] },
            { type: "tabset", weight: 33, children: [tab("Logs", "logs")] },
            { type: "tabset", weight: 33, children: [tab("Orders", "orders"), tab("Console", "console"), tab("Positions", "positions")] },
          ],
        },
      ],
    },
  },
};

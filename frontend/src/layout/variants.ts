import type { IJsonModel } from 'flexlayout-react';

const globalConfig = {
  tabEnableClose: false,
  tabSetEnableMaximize: true,
  tabSetEnableClose: false,
  splitterSize: 3,
  tabSetHeaderHeight: 24,
  tabSetTabStripHeight: 24,
  borderBarSize: 24,
};

// Variant A — Classic Trading Workstation (default)
// Left:   Positions/Watchlist tabbed (top) + Chart (bottom)
// Center: Console (top) + Orders/Trades/Bots tabbed (bottom)
// Right:  Bot Log/Bot Activity tabbed (top) + Quick Orders/Alerts/Logs/Errors/Bot Trades tabbed (bottom)
export const variantA: IJsonModel = {
  global: globalConfig,
  borders: [
    {
      type: 'border',
      location: 'right',
      size: 280,
      children: [
        { type: 'tab', name: 'Help', component: 'help' },
      ],
    },
  ],
  layout: {
    type: 'row',
    weight: 100,
    children: [
      // Left column — Positions/Watchlist (top) + Chart (bottom)
      {
        type: 'row',
        weight: 25,
        children: [
          {
            type: 'tabset',
            weight: 60,
            children: [
              { type: 'tab', name: 'Positions', component: 'positions' },
              { type: 'tab', name: 'Watchlist', component: 'watchlist' },
            ],
          },
          {
            type: 'tabset',
            weight: 40,
            children: [
              { type: 'tab', name: 'Chart', component: 'chart' },
            ],
          },
        ],
      },
      // Center column — Console (top) + Orders/Trades/Bots tabbed (bottom)
      {
        type: 'row',
        weight: 45,
        children: [
          {
            type: 'tabset',
            weight: 45,
            children: [
              { type: 'tab', name: 'Console', component: 'console' },
            ],
          },
          {
            type: 'tabset',
            weight: 55,
            children: [
              { type: 'tab', name: 'Orders', component: 'orders' },
              { type: 'tab', name: 'Trades', component: 'trades' },
              { type: 'tab', name: 'Bot Trades', component: 'bot-trades' },
              { type: 'tab', name: 'Bots', component: 'bots' },
            ],
          },
        ],
      },
      // Right column — Bot Log/Bot Activity (top) + everything-else tabbed (bottom)
      {
        type: 'row',
        weight: 30,
        children: [
          {
            type: 'tabset',
            weight: 40,
            children: [
              { type: 'tab', name: 'Stacked Charts', component: 'stacked-charts' },
              { type: 'tab', name: 'Bot Log', component: 'bot-log' },
              { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
            ],
          },
          {
            type: 'tabset',
            weight: 60,
            children: [
              { type: 'tab', name: 'Quick Orders', component: 'templates' },
              { type: 'tab', name: 'Alerts', component: 'alerts' },
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Errors', component: 'errors' },
            ],
          },
        ],
      },
    ],
  },
};

// Variant B — Modern Control Center
// Cleaner, fewer panes visible, contextual drawers, more hierarchy
export const variantB: IJsonModel = {
  global: globalConfig,
  borders: [
    {
      type: 'border',
      location: 'bottom',
      size: 200,
      children: [
        { type: 'tab', name: 'Logs', component: 'logs' },
        { type: 'tab', name: 'Errors', component: 'errors' },
        { type: 'tab', name: 'Bot Log', component: 'bot-log' },
        { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
        { type: 'tab', name: 'Console', component: 'console' },
        { type: 'tab', name: 'Help', component: 'help' },
      ],
    },
  ],
  layout: {
    type: 'row',
    weight: 100,
    children: [
      {
        type: 'row',
        weight: 70,
        children: [
          {
            type: 'tabset',
            weight: 60,
            children: [
              { type: 'tab', name: 'Positions', component: 'positions' },
              { type: 'tab', name: 'Watchlist', component: 'watchlist' },
              { type: 'tab', name: 'Orders', component: 'orders' },
              { type: 'tab', name: 'Trades', component: 'trades' },
              { type: 'tab', name: 'Bot Trades', component: 'bot-trades' },
            ],
          },
          {
            type: 'tabset',
            weight: 40,
            children: [
              { type: 'tab', name: 'Alerts', component: 'alerts' },
            ],
          },
        ],
      },
      {
        type: 'tabset',
        weight: 30,
        children: [
          { type: 'tab', name: 'Bots', component: 'bots', config: { large: true } },
        ],
      },
    ],
  },
};

// Variant C — Command-Centric Operator View
// Console dominant, other panes support command activity
export const variantC: IJsonModel = {
  global: globalConfig,
  borders: [
    {
      type: 'border',
      location: 'right',
      size: 260,
      children: [
        { type: 'tab', name: 'Help', component: 'help' },
        { type: 'tab', name: 'Bots', component: 'bots' },
      ],
    },
  ],
  layout: {
    type: 'row',
    weight: 100,
    children: [
      {
        type: 'row',
        weight: 60,
        children: [
          {
            type: 'tabset',
            weight: 65,
            children: [
              { type: 'tab', name: 'Console', component: 'console' },
            ],
          },
          {
            type: 'tabset',
            weight: 35,
            children: [
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Errors', component: 'errors' },
              { type: 'tab', name: 'Bot Log', component: 'bot-log' },
              { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
            ],
          },
        ],
      },
      {
        type: 'row',
        weight: 40,
        children: [
          {
            type: 'tabset',
            weight: 35,
            children: [
              { type: 'tab', name: 'Orders', component: 'orders', config: { compact: true } },
              { type: 'tab', name: 'Trades', component: 'trades', config: { compact: true } },
              { type: 'tab', name: 'Bot Trades', component: 'bot-trades', config: { compact: true } },
            ],
          },
          {
            type: 'tabset',
            weight: 35,
            children: [
              { type: 'tab', name: 'Positions', component: 'positions', config: { compact: true } },
              { type: 'tab', name: 'Watchlist', component: 'watchlist', config: { compact: true } },
            ],
          },
          {
            type: 'tabset',
            weight: 30,
            children: [
              { type: 'tab', name: 'Alerts', component: 'alerts' },
            ],
          },
        ],
      },
    ],
  },
};

// Variant T — Trader (chart-signal bots)
// 3-column layout:
//   Left column (~22%):  Console (top) · Stash tabset (bottom)
//   Middle column (~39%): Slot 1 (top, GC) · Audit Feed (bottom)
//   Right column (~39%):  Slot 3 (top, ES) · Slot 4 (bottom, NQ)
// Slots target the full-size CME/COMEX contracts (GC/ES/NQ) per
// the operator's 2026-06-07 scale-up from micros. Symbol is bound
// per slot via the chart-bot-<n>.yaml ``symbol`` field.
//
// flexlayout-react axis alternation: the root ``row`` lays out
// children horizontally; each child ``row`` stacks its tabsets
// vertically. So a "column" here is a nested ``row``.
export const variantT: IJsonModel = {
  global: globalConfig,
  // borders removed — Help is accessible via Bots panel and CLAUDE.md.
  layout: {
    type: 'row',
    weight: 100,
    children: [
      // Left column — Console on top, Stash tabset on bottom.
      {
        type: 'row',
        weight: 22,
        children: [
          {
            type: 'tabset',
            weight: 40,
            children: [
              { type: 'tab', name: 'Console', component: 'console' },
            ],
          },
          {
            type: 'tabset',
            weight: 60,
            children: [
              { type: 'tab', name: 'Orders', component: 'orders', config: { compact: true } },
              { type: 'tab', name: 'Bot Trades', component: 'bot-trades', config: { compact: true } },
              { type: 'tab', name: 'Positions', component: 'positions', config: { compact: true } },
              { type: 'tab', name: 'Watchlist', component: 'watchlist', config: { compact: true } },
              { type: 'tab', name: 'Alerts', component: 'alerts' },
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Errors', component: 'errors' },
              { type: 'tab', name: 'Bots', component: 'bots' },
            ],
          },
        ],
      },
      // Middle column — Slot 1 (top) + Slot 2 (bottom).
      {
        type: 'row',
        weight: 39,
        children: [
          {
            type: 'tabset',
            weight: 50,
            children: [
              { type: 'tab', name: 'GCV6', component: 'chart-bot', config: { slot: 1 } },
              { type: 'tab', name: 'MGCV6', component: 'chart-bot', config: { slot: 7 } },
            ],
          },
          {
            type: 'tabset',
            weight: 50,
            children: [
              { type: 'tab', name: 'Audit Feed', component: 'audit-feed' },
            ],
          },
        ],
      },
      // Right column — Slot 3 (top) + Slot 4 (bottom).
      {
        type: 'row',
        weight: 39,
        children: [
          {
            type: 'tabset',
            weight: 50,
            children: [
              { type: 'tab', name: 'MNQU6', component: 'chart-bot', config: { slot: 3 } },
              { type: 'tab', name: 'CLV6', component: 'chart-bot', config: { slot: 5 } },
            ],
          },
          {
            type: 'tabset',
            weight: 50,
            children: [
              { type: 'tab', name: 'NQU6', component: 'chart-bot', config: { slot: 4 } },
            ],
          },
        ],
      },
    ],
  },
};

// Variant D — Bot / Automation Supervision
// Bot widgets prominent, alerts visible, automation-first
export const variantD: IJsonModel = {
  global: globalConfig,
  borders: [
    {
      type: 'border',
      location: 'left',
      size: 260,
      children: [
        { type: 'tab', name: 'Help', component: 'help' },
        { type: 'tab', name: 'Console', component: 'console' },
      ],
    },
  ],
  layout: {
    type: 'row',
    weight: 100,
    children: [
      {
        type: 'row',
        weight: 55,
        children: [
          {
            type: 'tabset',
            weight: 70,
            children: [
              { type: 'tab', name: 'Bots', component: 'bots', config: { large: true } },
            ],
          },
          {
            type: 'tabset',
            weight: 30,
            children: [
              { type: 'tab', name: 'Alerts', component: 'alerts' },
            ],
          },
        ],
      },
      {
        type: 'row',
        weight: 45,
        children: [
          {
            type: 'tabset',
            weight: 35,
            children: [
              { type: 'tab', name: 'Orders', component: 'orders', config: { compact: true } },
              { type: 'tab', name: 'Positions', component: 'positions', config: { compact: true } },
              { type: 'tab', name: 'Watchlist', component: 'watchlist', config: { compact: true } },
              { type: 'tab', name: 'Trades', component: 'trades', config: { compact: true } },
              { type: 'tab', name: 'Bot Trades', component: 'bot-trades', config: { compact: true } },
            ],
          },
          {
            type: 'tabset',
            weight: 65,
            children: [
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Errors', component: 'errors' },
              { type: 'tab', name: 'Bot Log', component: 'bot-log' },
              { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
            ],
          },
        ],
      },
    ],
  },
};

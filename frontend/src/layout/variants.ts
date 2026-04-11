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

// Variant A — Classic Trading Workstation
// Left: Positions (top) + Bots (bottom)
// Center: Console (top) + Orders/Alerts tabbed (bottom)
// Right: Alerts/Errors (top) + Logs (bottom)
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
      // Left column — Positions + Bots
      {
        type: 'row',
        weight: 25,
        children: [
          {
            type: 'tabset',
            weight: 55,
            children: [
              { type: 'tab', name: 'Positions', component: 'positions' },
              { type: 'tab', name: 'Watchlist', component: 'watchlist' },
            ],
          },
          {
            type: 'tabset',
            weight: 45,
            children: [
              { type: 'tab', name: 'Bots', component: 'bots' },
            ],
          },
        ],
      },
      // Center column — Console + Orders/Trades tabbed
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
            ],
          },
        ],
      },
      // Right column — Quick Orders (top) + Alerts (mid) + Logs (bottom)
      {
        type: 'row',
        weight: 30,
        children: [
          {
            type: 'tabset',
            weight: 30,
            children: [
              { type: 'tab', name: 'Quick Orders', component: 'templates' },
            ],
          },
          {
            type: 'tabset',
            weight: 25,
            children: [
              { type: 'tab', name: 'Alerts', component: 'alerts' },
            ],
          },
          {
            type: 'tabset',
            weight: 45,
            children: [
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Bot Log', component: 'bot-log' },
              { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
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
              { type: 'tab', name: 'Watchlist', component: 'watchlist' },
              { type: 'tab', name: 'Orders', component: 'orders' },
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
            ],
          },
          {
            type: 'tabset',
            weight: 65,
            children: [
              { type: 'tab', name: 'Logs', component: 'logs' },
              { type: 'tab', name: 'Bot Log', component: 'bot-log' },
              { type: 'tab', name: 'Bot Activity', component: 'bot-activity' },
            ],
          },
        ],
      },
    ],
  },
};

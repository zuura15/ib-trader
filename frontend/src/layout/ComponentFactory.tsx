import type { TabNode } from 'flexlayout-react';
import { CommandConsole } from '../features/console/CommandConsole';
import { LogStream } from '../features/logs/LogStream';
import { OrdersPanel } from '../features/orders/OrdersPanel';
import { PositionsPanel } from '../features/positions/PositionsPanel';
import { AlertsPanel } from '../features/alerts/AlertsPanel';
import { BotsPanel } from '../features/bots/BotsPanel';
import { TradesPanel } from '../features/trades/TradesPanel';
import { OrderTemplatesPanel } from '../features/templates/OrderTemplatesPanel';
import { ScenarioPanel } from '../features/scenarios/ScenarioPanel';
import { HelpPanel } from '../features/help/HelpPanel';
import { WatchlistPanel } from '../features/watchlist/WatchlistPanel';

export function componentFactory(node: TabNode) {
  const component = node.getComponent();
  const config = node.getConfig() || {};

  switch (component) {
    case 'console':
      return <CommandConsole compact={config.compact} />;
    case 'logs':
      return <LogStream maxLines={config.maxLines} />;
    case 'orders':
      return <OrdersPanel compact={config.compact} />;
    case 'positions':
      return <PositionsPanel compact={config.compact} />;
    case 'alerts':
      return <AlertsPanel />;
    case 'trades':
      return <TradesPanel compact={config.compact} />;
    case 'templates':
      return <OrderTemplatesPanel />;
    case 'bots':
      return <BotsPanel large={config.large} />;
    case 'scenarios':
      return <ScenarioPanel />;
    case 'help':
      return <HelpPanel />;
    case 'watchlist':
      return <WatchlistPanel compact={config.compact} />;
    default:
      return <div className="p-4 text-xs" style={{ color: 'var(--text-muted)' }}>Unknown component: {component}</div>;
  }
}

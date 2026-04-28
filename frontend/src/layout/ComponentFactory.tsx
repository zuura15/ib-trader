import type { TabNode } from 'flexlayout-react';
import { CommandConsole } from '../features/console/CommandConsole';
import { LogStream, ErrorStream } from '../features/logs/LogStream';
import { OrdersPanel } from '../features/orders/OrdersPanel';
import { PositionsPanel } from '../features/positions/PositionsPanel';
import { AlertsPanel } from '../features/alerts/AlertsPanel';
import { BotsPanel } from '../features/bots/BotsPanel';
import { BotLogStream } from '../features/bots/BotLogStream';
import { BotActivity } from '../features/bots/BotActivity';
import { TradesPanel } from '../features/trades/TradesPanel';
import { BotTradesPanel } from '../features/bots/BotTradesPanel';
import { OrderTemplatesPanel } from '../features/templates/OrderTemplatesPanel';
import { ScenarioPanel } from '../features/scenarios/ScenarioPanel';
import { HelpPanel } from '../features/help/HelpPanel';
import { WatchlistPanel } from '../features/watchlist/WatchlistPanel';
import { ChartPane } from '../features/chart/ChartPane';
import { StackedChartsPane } from '../features/chart/StackedChartsPane';

export function componentFactory(node: TabNode) {
  const component = node.getComponent();
  const config = node.getConfig() || {};

  switch (component) {
    case 'console':
      return <CommandConsole compact={config.compact} />;
    case 'logs':
      return <LogStream maxLines={config.maxLines} />;
    case 'errors':
      return <ErrorStream maxLines={config.maxLines} />;
    case 'orders':
      return <OrdersPanel compact={config.compact} />;
    case 'positions':
      return <PositionsPanel compact={config.compact} />;
    case 'alerts':
      return <AlertsPanel />;
    case 'trades':
      return <TradesPanel compact={config.compact} />;
    case 'bot-trades':
      return <BotTradesPanel compact={config.compact} />;
    case 'templates':
      return <OrderTemplatesPanel />;
    case 'bots':
      return <BotsPanel large={config.large} />;
    case 'bot-log':
      return <BotLogStream maxLines={config.maxLines} />;
    case 'bot-activity':
      return <BotActivity maxLines={config.maxLines} />;
    case 'scenarios':
      return <ScenarioPanel />;
    case 'help':
      return <HelpPanel />;
    case 'watchlist':
      return <WatchlistPanel compact={config.compact} />;
    case 'chart':
      return <ChartPane />;
    case 'stacked-charts':
      return <StackedChartsPane />;
    default:
      return <div className="p-4 text-xs" style={{ color: 'var(--text-muted)' }}>Unknown component: {component}</div>;
  }
}

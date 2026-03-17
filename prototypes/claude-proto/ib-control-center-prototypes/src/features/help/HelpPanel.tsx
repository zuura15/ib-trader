import { PanelShell } from '../../components/PanelShell';

interface HelpSection {
  title: string;
  items: Array<{ cmd: string; desc: string }>;
}

const sections: HelpSection[] = [
  {
    title: 'Trading',
    items: [
      { cmd: 'buy AAPL 10 mid', desc: 'Buy 10 shares at mid price with reprice loop' },
      { cmd: 'buy AAPL 10 market', desc: 'Buy 10 shares at market price' },
      { cmd: 'buy AAPL 10 bid', desc: 'Buy 10 shares at current bid (passive)' },
      { cmd: 'buy AAPL 10 ask', desc: 'Buy 10 shares at current ask (aggressive)' },
      { cmd: 'buy AAPL 10 limit 180.50', desc: 'Buy 10 shares at limit $180.50' },
      { cmd: 'sell TSLA 5 mid', desc: 'Sell 5 shares at mid price' },
      { cmd: 'sell TSLA 5 mid --profit 500', desc: 'Sell with $500 profit taker' },
    ],
  },
  {
    title: 'Close Positions',
    items: [
      { cmd: 'close 42 mid', desc: 'Close trade #42 at mid price' },
      { cmd: 'close 42 market', desc: 'Close trade #42 at market' },
      { cmd: 'close 42 limit 250.00', desc: 'Close trade #42 at limit $250' },
    ],
  },
  {
    title: 'Options',
    items: [
      { cmd: '--profit 500', desc: 'Place profit taker after fill ($500 target)' },
      { cmd: '--stop-loss 200', desc: 'Set stop loss amount (stored, not placed)' },
      { cmd: '--dollars 5000', desc: 'Size by dollar amount instead of shares' },
      { cmd: '--broker alpaca', desc: 'Route to Alpaca instead of IB' },
    ],
  },
  {
    title: 'Info',
    items: [
      { cmd: 'status', desc: 'Show system status and P&L summary' },
      { cmd: 'orders', desc: 'List all open orders' },
      { cmd: 'help', desc: 'Show help in console output' },
    ],
  },
];

function copyCmd(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

export function HelpPanel() {
  return (
    <PanelShell title="Help" accent="blue" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>command reference</span>
    }>
      <div className="h-full overflow-auto p-2">
        {sections.map((section) => (
          <div key={section.title} className="mb-3">
            <div className="text-[10px] font-semibold mb-1 px-1 uppercase tracking-wider"
              style={{ color: 'var(--text-secondary)' }}>
              {section.title}
            </div>
            <div className="flex flex-col gap-1">
              {section.items.map((item) => (
                <div key={item.cmd}
                  className="flex items-start gap-2 px-2 py-1.5 rounded"
                  style={{ background: 'var(--bg-secondary)' }}>
                  <div className="flex-1 min-w-0">
                    <code className="font-mono text-xs" style={{ color: 'var(--accent-blue)' }}>
                      {item.cmd}
                    </code>
                    <div className="text-[11px] mt-0.5" style={{ color: 'var(--text-muted)' }}>
                      {item.desc}
                    </div>
                  </div>
                  <button
                    onClick={() => copyCmd(item.cmd)}
                    className="shrink-0 text-[10px] px-1.5 py-0.5 rounded"
                    style={{
                      background: 'var(--bg-tertiary)',
                      color: 'var(--text-muted)',
                      border: 'none',
                    }}
                    title="Copy command"
                  >
                    copy
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </PanelShell>
  );
}

import { useState, useRef, useEffect } from 'react';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import type { CommandStatus } from '../../types';

const statusIcon: Record<CommandStatus, string> = {
  queued: '◌',
  running: '●',
  success: '✓',
  failure: '✗',
};

const statusVar: Record<CommandStatus, string> = {
  queued: 'var(--text-muted)',
  running: 'var(--accent-blue)',
  success: 'var(--accent-green)',
  failure: 'var(--accent-red)',
};

const statusLabel: Record<CommandStatus, string> = {
  queued: 'queued — waiting for engine...',
  running: 'executing — waiting for broker response...',
  success: '',
  failure: '',
};

function copyToClipboard(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

export function CommandConsole({ compact = false }: { compact?: boolean }) {
  const { commands, addCommand } = useStore();
  const [input, setInput] = useState('');
  const [historyIdx, setHistoryIdx] = useState(-1);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [commands]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const cmd = input.trim();
    if (!cmd) return;
    addCommand(cmd);
    setInput('');
    setHistoryIdx(-1);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      const execd = commands.filter(c => c.status !== 'queued');
      const newIdx = Math.min(historyIdx + 1, execd.length - 1);
      setHistoryIdx(newIdx);
      if (execd[execd.length - 1 - newIdx]) {
        setInput(execd[execd.length - 1 - newIdx].command);
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      const execd = commands.filter(c => c.status !== 'queued');
      const newIdx = Math.max(historyIdx - 1, -1);
      setHistoryIdx(newIdx);
      setInput(newIdx === -1 ? '' : execd[execd.length - 1 - newIdx]?.command || '');
    }
  };

  const handleCopy = (cmd: { id: string; command: string; output?: string }) => {
    const text = `$ ${cmd.command}\n${cmd.output || ''}`;
    copyToClipboard(text);
    setCopiedId(cmd.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const activeCount = commands.filter(c => c.status === 'queued' || c.status === 'running').length;
  const isTerminal = (s: CommandStatus) => s === 'success' || s === 'failure';

  return (
    <PanelShell title="Console" accent="blue" right={
      activeCount > 0
        ? <span className="animate-pulse" style={{ fontSize: 10, color: 'var(--accent-blue)' }}>
            {activeCount} command{activeCount > 1 ? 's' : ''} running...
          </span>
        : <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>prompt ready</span>
    }>
      <div className="flex flex-col h-full" onClick={() => inputRef.current?.focus()}>
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-2 font-mono text-sm">
          {commands.map((cmd, idx) => (
            <div key={cmd.id}>
              <div className="mb-1 relative">
                {/* Command line */}
                <div className="flex items-center gap-2">
                  <span style={{ color: 'var(--text-muted)' }}>{formatTime(cmd.startedAt)}</span>
                  <span style={{ color: statusVar[cmd.status] }}>{statusIcon[cmd.status]}</span>
                  <span style={{ color: 'var(--accent-blue)' }}>$</span>
                  <span style={{ color: 'var(--text-primary)' }}>{cmd.command}</span>

                  {/* Copy button — top right of command output */}
                  {isTerminal(cmd.status) && cmd.output && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleCopy(cmd); }}
                      style={{
                        marginLeft: 'auto',
                        background: 'none',
                        border: 'none',
                        color: copiedId === cmd.id ? 'var(--accent-green)' : 'var(--text-muted)',
                        fontSize: 12,
                        padding: '2px 4px',
                      }}
                      title="Copy output"
                    >
                      {copiedId === cmd.id ? '✓ copied' : '⧉ copy'}
                    </button>
                  )}
                </div>

                {/* Live status indicator */}
                {(cmd.status === 'queued' || cmd.status === 'running') && (
                  <div className="pl-6 mt-0.5 flex items-center gap-2">
                    <span className="animate-pulse" style={{ color: 'var(--accent-blue)', fontSize: 12 }}>●</span>
                    <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                      {statusLabel[cmd.status]}
                    </span>
                  </div>
                )}

                {/* Output */}
                {(cmd.output || cmd.status === 'failure') && !compact && (
                  <div className="pl-6 mt-0.5 whitespace-pre-wrap" style={{
                    color: cmd.status === 'failure' ? 'var(--accent-red)' : 'var(--text-secondary)',
                  }}>
                    {cmd.output || (cmd.status === 'failure' ? 'Command failed — check engine logs' : '')}
                  </div>
                )}
              </div>

              {/* Separator line between completed commands */}
              {isTerminal(cmd.status) && idx < commands.length - 1 && (
                <div style={{
                  borderBottom: '1px solid var(--border-default)',
                  margin: '8px 0 10px 0',
                }} />
              )}
            </div>
          ))}
        </div>

        <form onSubmit={handleSubmit} className="flex items-center gap-2 px-2 py-1.5 border-t"
          style={{ borderColor: 'var(--border-default)', background: 'var(--bg-secondary)' }}>
          <span style={{ color: 'var(--accent-blue)' }} className="text-sm font-bold font-mono">$</span>
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Enter command..."
            className="flex-1 bg-transparent outline-none text-sm font-mono"
            style={{ color: 'var(--text-primary)' }}
            spellCheck={false}
            autoComplete="off"
          />
        </form>
      </div>
    </PanelShell>
  );
}

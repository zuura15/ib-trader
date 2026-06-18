import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import { VoiceModal, isSpeechAvailable } from './VoiceModal';
import { ConsolePnl24hBadge } from './ConsolePnl24hBadge';
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
  // Async Clipboard API requires a secure context (HTTPS or localhost).
  // The operator's prod box serves over plain HTTP on the LAN
  // (http://192.168.4.66:8000), so ``navigator.clipboard`` is
  // ``undefined`` there and the previous ``.catch(() => {})`` silently
  // swallowed the TypeError. Fall back to the legacy
  // ``document.execCommand('copy')`` path via a transient textarea —
  // deprecated by spec but supported by every browser and works in
  // insecure contexts.
  const fallback = () => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      // Keep it offscreen so it can't steal focus from inputs the
      // operator is typing into.
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      document.execCommand('copy');
      document.body.removeChild(ta);
    } catch { /* swallow — no clipboard available at all */ }
  };
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    navigator.clipboard.writeText(text).catch(fallback);
    return;
  }
  fallback();
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/**
 * Command history dropdown.
 *
 * Shows the last 10 executed commands. Tapping one fills the input.
 * On desktop, Arrow Up/Down already navigates history — this button
 * provides mobile-friendly access to the same functionality.
 */
function HistoryButton({
  onSelect,
  disabled,
}: {
  onSelect: (command: string) => void;
  disabled?: boolean;
}) {
  const commands = useStore((s) => s.commands);
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [dropdownPos, setDropdownPos] = useState({ bottom: 0, right: 0 });

  // Calculate dropdown position from the button's screen coordinates.
  // Rendered via portal on document.body to escape overflow:hidden ancestors.
  useEffect(() => {
    if (!open || !buttonRef.current) return;
    const rect = buttonRef.current.getBoundingClientRect();
    setDropdownPos({
      bottom: window.innerHeight - rect.top + 4,
      right: window.innerWidth - rect.right,
    });
  }, [open]);

  // Close on outside click/tap
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node;
      if (
        buttonRef.current?.contains(target) ||
        dropdownRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    document.addEventListener('touchstart', handler);
    return () => {
      document.removeEventListener('mousedown', handler);
      document.removeEventListener('touchstart', handler);
    };
  }, [open]);

  // Close when disabled externally (e.g. mic starts listening)
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const executed = commands
    .filter((c) => c.status !== 'queued')
    .slice(-10)
    .reverse();

  const dropdown = open && (
    <div
      ref={dropdownRef}
      role="menu"
      aria-label="Recent commands"
      style={{
        position: 'fixed',
        bottom: dropdownPos.bottom,
        left: 8,
        right: 8,
        maxWidth: 400,
        maxHeight: 300,
        overflowY: 'auto',
        background: 'var(--bg-secondary)',
        border: '1px solid var(--border-default)',
        borderRadius: 6,
        boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
        zIndex: 9999,
      }}
    >
      {executed.length === 0 ? (
        <div style={{ padding: '12px 16px', fontSize: 12, color: 'var(--text-muted)' }}>
          No command history yet
        </div>
      ) : (
        executed.map((cmd) => (
          <button
            key={cmd.id}
            role="menuitem"
            onClick={() => {
              onSelect(cmd.command);
              setOpen(false);
            }}
            className="font-mono"
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              background: 'none',
              border: 'none',
              borderBottom: '1px solid var(--border-default)',
              padding: '10px 16px',
              fontSize: 12,
              color: 'var(--text-primary)',
              cursor: 'pointer',
              minHeight: 44,
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'var(--bg-primary)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'none';
            }}
          >
            <span style={{ color: 'var(--accent-blue)', marginRight: 6 }}>$</span>
            {cmd.command}
          </button>
        ))
      )}
    </div>
  );

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((prev) => !prev)}
        aria-label="Command history"
        aria-expanded={open}
        aria-haspopup="menu"
        title="Command history"
        style={{
          background: 'none',
          border: 'none',
          color: disabled ? 'var(--text-muted)' : open ? 'var(--accent-blue)' : 'var(--text-muted)',
          opacity: disabled ? 0.4 : 1,
          fontSize: 16,
          padding: 8,
          minWidth: 44,
          minHeight: 44,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: disabled ? 'default' : 'pointer',
        }}
      >
        &#x21BB;
      </button>
      {dropdown && createPortal(dropdown, document.body)}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * Collapse the engine's verbose multi-line order output. The command is
 * already the entry's first line, so:
 *   - drop the redundant ``Order #N — …`` header,
 *   - drop the debug ``fill notional=…`` line (derivable from qty×price),
 *   - keep every ``[HH:MM:SS] …`` placed/walk step verbatim,
 *   - merge the trailing fill / P&L / commission detail into one summary
 *     line: ``✓ #N · filled Q @ price · P&L ±$X · comm $Y``.
 * Tolerant: anything unrecognized is kept verbatim, and if the fill line
 * can't be parsed the raw detail lines are joined instead — so no
 * information is ever lost, only re-flowed. Applied in both layouts.
 */
function collapseConsoleOutput(output: string): string {
  const kept: string[] = [];
  const detail: string[] = [];
  let serial = '';
  for (const raw of output.split('\n')) {
    const t = raw.trim();
    if (!t) continue;
    const hdr = t.match(/^Order #(\d+)\s*[—-]/);
    if (hdr) { serial = hdr[1]; continue; }            // repeated command — drop
    if (/^fill notional=/i.test(t)) continue;          // debug noise — drop
    if (t.startsWith('[')) { kept.push(t); continue; }  // [HH:MM:SS] step — keep
    if (/^(?:[✓⚠✗❌●]\s*)?(?:FILLED|PARTIAL):|^Commission:|^Serial:|^P&L\b/i.test(t)) {
      detail.push(t);
      continue;
    }
    kept.push(t);                                        // QUEUED / LIVE / errors — keep
  }
  if (detail.length) {
    const joined = detail.join('  ');
    const fill = joined.match(/(?:FILLED|PARTIAL):\s*([\d.,]+)[^@]*@\s*(?:avg\s*)?\$?\s*([\d.,]+)/i);
    const comm = joined.match(/Commission:\s*\$?([\d.,-]+)/i);
    const pnl = joined.match(/P&L[^:]*:\s*([+-])\$?([\d.,]+)/i);
    const ser = serial || (joined.match(/Serial:\s*#?(\d+)/i)?.[1] ?? '');
    const parts: string[] = [];
    if (ser) parts.push(`#${ser}`);
    if (fill) parts.push(`filled ${fill[1]} @ ${fill[2]}`);
    if (pnl) parts.push(`P&L ${pnl[1]}$${pnl[2]}`);
    if (comm) parts.push(`comm $${comm[1].replace(/^\$/, '')}`);
    kept.push(parts.length > (ser ? 1 : 0) ? `✓ ${parts.join(' · ')}` : detail.join(' · '));
  }
  return kept.join('\n');
}

export function CommandConsole({ compact = false }: { compact?: boolean }) {
  const { commands, addCommand } = useStore();
  const [input, setInput] = useState('');
  const [historyIdx, setHistoryIdx] = useState(-1);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Defer to the next frame so the new command's output has been laid
    // out before we measure scrollHeight — otherwise we scroll to the
    // pre-render (shorter) height and stop short of the latest entry.
    const el = scrollRef.current;
    if (!el) return;
    const id = requestAnimationFrame(() => el.scrollTo(0, el.scrollHeight));
    return () => cancelAnimationFrame(id);
  }, [commands]);

  // Clean up the copy-indicator timer on unmount
  useEffect(() => {
    return () => clearTimeout(copiedTimerRef.current);
  }, []);

  // Global 'c' hotkey to focus the command input
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'c' || e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
          (e.target as HTMLElement).isContentEditable) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

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
    clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = setTimeout(() => setCopiedId(null), 2000);
  };

  /** Called by MicButton or HistoryButton to pre-fill the input. */
  const prefillInput = useCallback((text: string) => {
    setInput(text);
    setHistoryIdx(-1);
    // Defer focus so the value is set before the cursor moves to end
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const [voiceOpen, setVoiceOpen] = useState(false);
  const speechAvailable = isSpeechAvailable();

  const activeCount = commands.filter(c => c.status === 'queued' || c.status === 'running').length;
  const isTerminal = (s: CommandStatus) => s === 'success' || s === 'failure';

  return (
    <PanelShell title="Console" titleExtra={<ConsolePnl24hBadge />} accent="blue" right={
      activeCount > 0
        ? <span className="animate-pulse" style={{ fontSize: 10, color: 'var(--accent-blue)' }}>
            {activeCount} command{activeCount > 1 ? 's' : ''} running...
          </span>
        : <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>prompt ready</span>
    }>
      <div className="flex flex-col h-full" onClick={() => inputRef.current?.focus()}>
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-2 font-mono text-sm">
          {commands.map((cmd) => (
            <div
              key={cmd.id}
              data-testid="console-command"
              data-status={cmd.status}
              data-command={cmd.command}
            >
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

                {/* Output — shown in both layouts. Was previously hidden on
                    success in compact (mobile) mode, which made completed
                    orders look like nothing had happened: only the green
                    checkmark changed, no fill price or confirmation text. */}
                {(cmd.output || cmd.status === 'failure') && (
                  <div
                    className="pl-6 mt-0.5 whitespace-pre-wrap"
                    style={{
                      color: cmd.status === 'failure' ? 'var(--accent-red)' : 'var(--text-secondary)',
                    }}
                    data-testid="console-output"
                  >
                    {(() => {
                      // Collapse the verbose order block in both layouts,
                      // then render line-by-line so the ``P&L ±$X`` token
                      // can be tinted green (gain) / red (loss).
                      const out = cmd.output
                        ? collapseConsoleOutput(cmd.output)
                        : (cmd.status === 'failure' ? 'Command failed — check engine logs' : '');
                      return out.split('\n').map((line, i) => {
                        const m = line.match(/P&L\s([+-])\$[\d.,]+/);
                        if (!m) return <div key={i}>{line}</div>;
                        const start = m.index ?? 0;
                        const color = m[1] === '-'
                          ? 'var(--accent-red)' : 'var(--accent-green)';
                        return (
                          <div key={i}>
                            {line.slice(0, start)}
                            <span style={{ color, fontWeight: 600 }}>{m[0]}</span>
                            {line.slice(start + m[0].length)}
                          </div>
                        );
                      });
                    })()}
                  </div>
                )}
              </div>

              {/* EOF marker — rendered after every completed command so
                  the user has a visible closing bracket for the output
                  block (not a header for the next command). Previous
                  behaviour skipped the last command; now the newest
                  completed command also gets its marker. */}
              {isTerminal(cmd.status) && (
                <div style={{
                  borderBottom: '1px solid var(--border-default)',
                  margin: compact ? '3px 0 4px 0' : '8px 0 10px 0',
                }} />
              )}
            </div>
          ))}
        </div>

        <form
          onSubmit={handleSubmit}
          className="flex items-center gap-1 px-2 py-1 border-t"
          style={{ borderColor: 'var(--border-default)', background: 'var(--bg-secondary)' }}
          onClick={(e) => e.stopPropagation()}
          data-testid="console-form"
        >
          <span style={{ color: 'var(--accent-blue)' }} className="text-sm font-bold font-mono">$</span>
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Enter command..."
            className="flex-1 bg-transparent outline-none text-sm font-mono"
            style={{ color: 'var(--text-primary)', minHeight: 36 }}
            spellCheck={false}
            autoComplete="off"
            data-testid="console-input"
          />
          <HistoryButton onSelect={prefillInput} disabled={voiceOpen} />
          {speechAvailable && (
            <button
              type="button"
              onClick={() => setVoiceOpen(true)}
              aria-label="Voice input"
              title="Voice input (Chrome/Android)"
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--text-muted)',
                fontSize: 18,
                padding: 8,
                minWidth: 44,
                minHeight: 44,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
              }}
            >
              &#x1F3A4;
            </button>
          )}
          <VoiceModal
            open={voiceOpen}
            onClose={() => setVoiceOpen(false)}
            onSubmit={(text) => {
              setVoiceOpen(false);
              prefillInput(text);
            }}
          />
        </form>
      </div>
    </PanelShell>
  );
}

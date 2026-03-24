import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import { VoiceModal, isSpeechAvailable } from './VoiceModal';
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

export function CommandConsole({ compact = false }: { compact?: boolean }) {
  const { commands, addCommand } = useStore();
  const [input, setInput] = useState('');
  const [historyIdx, setHistoryIdx] = useState(-1);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
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
                {(cmd.output || cmd.status === 'failure') && (!compact || cmd.status === 'failure') && (
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

        <form
          onSubmit={handleSubmit}
          className="flex items-center gap-1 px-2 py-1 border-t"
          style={{ borderColor: 'var(--border-default)', background: 'var(--bg-secondary)' }}
          onClick={(e) => e.stopPropagation()}
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

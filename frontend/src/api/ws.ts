/**
 * WebSocket manager for real-time data updates.
 *
 * Handles connection, reconnection with exponential backoff,
 * subscription, snapshot delivery, and diff dispatching.
 */

export type Channel =
  | 'trades' | 'orders' | 'alerts' | 'commands' | 'heartbeats'
  | 'bots' | 'status';

export interface WSDiff {
  type: 'diff';
  channel: Channel;
  added: Record<string, unknown>[];
  updated: Record<string, unknown>[];
  removed: Record<string, unknown>[];
}

export interface WSSnapshot {
  type: 'snapshot';
  data: Record<Channel, Record<string, unknown>[]>;
}

export interface WSCommandOutput {
  type: 'command_output';
  cmd_id: string;
  data: {
    type?: 'line' | 'done';
    message?: string;
    severity?: string;
    status?: string;
    error?: string;
  };
}

export type WSMessage =
  | WSDiff
  | WSSnapshot
  | WSCommandOutput
  | { type: 'pong' };

type DiffHandler = (channel: Channel, diff: WSDiff) => void;
type SnapshotHandler = (data: WSSnapshot['data']) => void;
type StatusHandler = (connected: boolean) => void;
type CommandOutputHandler = (msg: WSCommandOutput) => void;

// Use wss:// when the page is served over HTTPS (e.g. LAN access via basic-ssl).
// Browsers block mixed-content ws:// from an https:// page.
const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_BASE = import.meta.env.VITE_WS_URL || `${wsProto}//${window.location.host}/ws`;
const WS_TOKEN = import.meta.env.VITE_API_TOKEN || '';
const WS_URL = WS_TOKEN ? `${WS_BASE}?token=${WS_TOKEN}` : WS_BASE;
const CHANNELS: Channel[] = [
  'trades', 'orders', 'alerts', 'commands', 'heartbeats', 'bots', 'status',
];

const MIN_RECONNECT_MS = 1000;
const MAX_RECONNECT_MS = 30000;

export class WSManager {
  private ws: WebSocket | null = null;
  private reconnectMs = MIN_RECONNECT_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private onDiff: DiffHandler | null = null;
  private onSnapshot: SnapshotHandler | null = null;
  private onStatus: StatusHandler | null = null;
  private destroyed = false;
  private cmdOutputHandlers = new Map<string, CommandOutputHandler>();
  // Commands subscribed before the WS opened — flushed on connect.
  private pendingCmdSubscriptions = new Set<string>();

  /**
   * Subscribe to live output for a single in-flight command.
   *
   * The server XREADs the ``cmd:{cmdId}:output`` Redis stream from the
   * beginning and pushes each line plus a final ``done`` marker. Handler
   * is auto-unregistered on the ``done`` message.
   */
  subscribeCommandOutput(cmdId: string, handler: CommandOutputHandler): () => void {
    this.cmdOutputHandlers.set(cmdId, handler);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'subscribe_command_output', cmd_id: cmdId }));
    } else {
      this.pendingCmdSubscriptions.add(cmdId);
    }
    return () => {
      this.cmdOutputHandlers.delete(cmdId);
      this.pendingCmdSubscriptions.delete(cmdId);
    };
  }

  /**
   * Register handlers and connect.
   */
  start(handlers: {
    onDiff: DiffHandler;
    onSnapshot: SnapshotHandler;
    onStatus?: StatusHandler;
  }): void {
    this.onDiff = handlers.onDiff;
    this.onSnapshot = handlers.onSnapshot;
    this.onStatus = handlers.onStatus || null;
    this.connect();
  }

  /**
   * Disconnect and stop reconnecting.
   */
  stop(): void {
    this.destroyed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.ws) {
      this.ws.onclose = null; // Prevent reconnect
      this.ws.close();
      this.ws = null;
    }
  }

  private connect(): void {
    if (this.destroyed) return;

    try {
      this.ws = new WebSocket(WS_URL);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.reconnectMs = MIN_RECONNECT_MS;
      this.onStatus?.(true);

      // Subscribe to all channels (guard against race)
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          type: 'subscribe',
          channels: CHANNELS,
        }));
        // Flush any command-output subscriptions queued before connect.
        for (const cmdId of this.pendingCmdSubscriptions) {
          this.ws.send(JSON.stringify({ type: 'subscribe_command_output', cmd_id: cmdId }));
        }
        this.pendingCmdSubscriptions.clear();
      }

      // Start ping keepalive every 25s
      this.pingTimer = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, 25000);
    };

    this.ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);
        if (msg.type === 'snapshot') {
          this.onSnapshot?.(msg.data);
        } else if (msg.type === 'diff') {
          this.onDiff?.(msg.channel, msg as WSDiff);
        } else if (msg.type === 'command_output') {
          const handler = this.cmdOutputHandlers.get(msg.cmd_id);
          handler?.(msg);
          if (msg.data?.type === 'done') {
            this.cmdOutputHandlers.delete(msg.cmd_id);
          }
        }
        // pong is silently ignored
      } catch {
        // Malformed message — ignore
      }
    };

    this.ws.onclose = () => {
      this.onStatus?.(false);
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will fire after onerror
    };
  }

  private scheduleReconnect(): void {
    if (this.destroyed) return;
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.reconnectMs);
    this.reconnectMs = Math.min(this.reconnectMs * 2, MAX_RECONNECT_MS);
  }
}

/** Singleton instance */
export const wsManager = new WSManager();

// ── Dashboard Types ──────────────────────────────────────────────────

export type SystemMode = 'LIVE PAPER' | 'BACKTEST' | 'DRY RUN';

export type Severity = 'critical' | 'error' | 'warning' | 'info';

export type HeartbeatStatus = 'green' | 'amber' | 'red';

export type StrategyState = 'active' | 'probation' | 'frozen' | 'retired';

export interface NavTab {
  id: string;
  label: string;
  alertCount: number;
}

export interface Alert {
  id: string;
  severity: Severity;
  message: string;
  timestamp: number;
}

export interface Heartbeat {
  status: HeartbeatStatus;
  label: string;
  latency_ms?: number;
  latency_s?: number;
  subscribed?: string;
}

export interface RateLimitBudget {
  available: number;
  total: number;
  label: string;
}

export interface SystemStatus {
  mode: string;
  portfolio_epoch: number;
  active_strategies: string[];
  alpha_whales: number;
  websocket_connected: boolean;
  tracked_markets_book: number;
  tracked_markets_trades: number;
  heartbeats: Record<string, Heartbeat>;
  degradation_metrics: {
    mode: string;
    can_trade: boolean;
    component_health: Record<string, boolean>;
  };
}

export interface StrategyRanking {
  name: string;
  sortino: number;
  state: StrategyState;
  alloc_pct: number;
  trades: number;
  win_rate: number;
  sharpe: number;
}

export interface Allocation {
  active: number;
  frozen: number;
  retired: number;
  total_equity: number;
  pnl_24h: number;
  pnl_24h_pct: number;
  max_drawdown: number;
  max_drawdown_pct?: number;
}

export interface AlphaWhale {
  wallet: string;
  score: number;
  total_pnl: number;
  win_rate: number;
  trades_per_week: number;
  last_active_s: number;
}

export interface WhaleFlow {
  markets: Array<{
    market: string;
    flow_usd: number;
    direction: 'buy' | 'sell';
  }>;
  avg_conviction_multiplier: number;
}

export interface Position {
  id: number;
  market: string;
  strategy: string;
  side: 'YES' | 'NO';
  size: number;
  entry: number;
  mark: number;
  pnl: number;
  pnl_pct: number;
  tau_pct: number;
  toxicity: number;
  liquidation_zone: boolean;
}

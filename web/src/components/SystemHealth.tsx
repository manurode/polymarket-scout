import { useSystemStatus } from '../hooks/useSystemStatus';
import { useRateLimits } from '../hooks/useRateLimits';
import type { SystemStatus } from '../types';

export function SystemHealth() {
  const status = useSystemStatus(2000);
  const { budgets: rateLimits } = useRateLimits(5000);

  return (
    <div className="p-4 space-y-4">
      {/* ── Section Header ────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          SYSTEM HEALTH
        </h2>
        <span className="text-[10px] text-text-tertiary font-mono">
          last snapshot: 2.1s ago
        </span>
      </div>

      {/* ── Heartbeats + Rate Limits ──────────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <HeartbeatsPanel heartbeats={status.heartbeats} />
        <RateLimitsPanel budgets={rateLimits} />
      </div>

      {/* ── Reconciliation Matrix ──────────────────────────────── */}
      <ReconciliationPanel status={status} />

      {/* ── Degradation + Latency Budget ───────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <DegradationPanel status={status} />
        <LatencyBudgetPanel />
      </div>

      {/* ── Wallet Monitor ─────────────────────────────────────── */}
      <WalletPanel />
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function HeartbeatsPanel({ heartbeats }: { heartbeats: SystemStatus['heartbeats'] }) {
  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">HEARTBEATS</h3>
      <div className="space-y-2">
        {Object.entries(heartbeats).map(([key, beat]) => (
          <div key={key} className="flex items-center justify-between text-[11px] font-mono">
            <div className="flex items-center gap-2">
              <span className={`
                w-2 h-2 rounded-full flex-shrink-0
                ${beat.status === 'green' ? 'bg-profit' : beat.status === 'amber' ? 'bg-warning' : 'bg-loss animate-pulse-red'}
              `} />
              <span className="text-text-secondary">{beat.label}</span>
            </div>
            <span className="text-text-primary text-right">
              {beat.latency_ms != null ? `${beat.latency_ms}ms` : ''}
              {beat.latency_s != null ? `${beat.latency_s}s` : ''}
              {beat.subscribed ? <span className="text-text-tertiary"> · {beat.subscribed}</span> : ''}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RateLimitsPanel({ budgets }: { budgets: Record<string, { available: number; total: number; label: string }> }) {
  const entries = Object.entries(budgets);
  if (entries.length === 0) return null;

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">RATE LIMIT BUDGET</h3>
      <div className="space-y-3">
        {entries.map(([key, b]) => {
          const pct = Math.round((b.available / b.total) * 100);
          const color = key === 'reconciliation' ? 'bg-info' : key === 'onboarding' ? 'bg-profit' : 'bg-text-tertiary';
          return (
            <div key={key}>
              <div className="flex justify-between text-[10px] font-mono mb-0.5">
                <span className="text-text-secondary">{b.label}</span>
                <span className={pct < 10 ? 'text-warning animate-pulse-red' : 'text-text-primary'}>
                  {pct}%
                </span>
              </div>
              <div className="bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
                <div className={`${color} h-full rounded-full transition-all duration-500`} style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-2 text-[10px] text-text-tertiary font-mono">
        Tokens: {(
          Object.values(budgets).reduce((sum, b) => sum + b.available, 0) /
          Object.values(budgets).reduce((sum, b) => sum + b.total, 0) * 100
        ).toFixed(0)}% available
      </div>
    </div>
  );
}

function ReconciliationPanel({ status }: { status: SystemStatus }) {
  const cleanCount = status.tracked_markets_book;
  const totalCount = 50;
  const reconcilingCount = totalCount - cleanCount;
  const cleanPct = Math.round((cleanCount / totalCount) * 100);

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
        RECONCILIATION MATRIX (Top {totalCount})
      </h3>
      <div className="flex items-center gap-2 mb-1.5">
        <div className="flex-1 bg-bg-tertiary rounded-full h-2.5 overflow-hidden">
          <div className="bg-profit h-full rounded-full transition-all duration-700" style={{ width: `${cleanPct}%` }} />
        </div>
        <span className="text-[11px] font-mono">
          <span className="text-profit">{cleanCount}</span>
          <span className="text-text-secondary">/{totalCount} CLEAN</span>
        </span>
      </div>
      {reconcilingCount > 0 ? (
        <div className="text-[10px] text-warning animate-pulse-red mt-1">
          ⚠ {reconcilingCount} market{reconcilingCount > 1 ? 's' : ''} in RECONCILING state
        </div>
      ) : (
        <div className="text-[10px] text-profit mt-1">
          All markets CLEAN — no reconciliation needed
        </div>
      )}
    </div>
  );
}

function DegradationPanel({ status }: { status: SystemStatus }) {
  const mode = status.degradation_metrics.mode;
  const canTrade = status.degradation_metrics.can_trade;
  const color = mode === 'full' ? 'border-profit text-profit' :
    mode === 'minimal' ? 'border-warning text-warning' :
    'border-loss text-loss animate-pulse-red';

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">DEGRADATION MODE</h3>
      <div className="flex items-center gap-3">
        <div className={`w-16 h-16 rounded-full border-4 flex items-center justify-center ${color}`}>
          <span className="text-xs font-bold uppercase">{mode}</span>
        </div>
        <div className="text-[11px] text-text-secondary space-y-0.5">
          <div>Active strategies: {status.active_strategies.length}/7</div>
          <div>Trading: {canTrade ? '✓ ENABLED' : '✗ DISABLED'}</div>
          <div>WebSocket: {status.websocket_connected ? '✓ Connected' : '✗ Down'}</div>
        </div>
      </div>
    </div>
  );
}

function LatencyBudgetPanel() {
  const stages = [
    { label: 'WS→Book', actual: 1.2, budget: 5 },
    { label: 'OBI+TFI→Spoof', actual: 2.8, budget: 5 },
    { label: 'Signal→Decision', actual: 8.1, budget: 10 },
    { label: 'Kelly→Position', actual: 1.9, budget: 5 },
    { label: 'Risk→Trade', actual: 0.8, budget: 3 },
  ];

  const totalActual = stages.reduce((s, st) => s + st.actual, 0);
  const totalBudget = 25;

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
        LATENCY BUDGET (Critical Path)
      </h3>
      <div className="space-y-1.5">
        {stages.map(st => {
          const pct = (st.actual / st.budget) * 100;
          const color = pct > 80 ? 'bg-loss' : pct > 50 ? 'bg-warning' : 'bg-profit';
          return (
            <div key={st.label} className="flex items-center gap-2 text-[10px] font-mono">
              <span className="text-text-tertiary w-28">{st.label}</span>
              <div className="flex-1 bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
                <div className={`${color} h-full rounded-full`} style={{ width: `${Math.min(pct, 100)}%` }} />
              </div>
              <span className="text-text-primary w-24 text-right">{st.actual}ms / {st.budget}ms</span>
            </div>
          );
        })}
        <div className="border-t border-bg-hover mt-2 pt-1.5 flex justify-between text-[10px] font-mono">
          <span className="text-text-secondary">TOTAL</span>
          <span className="text-text-primary">
            {totalActual}ms / {totalBudget}ms ({Math.round((totalActual / totalBudget) * 100)}%)
          </span>
        </div>
      </div>
    </div>
  );
}

function WalletPanel() {
  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
        WALLET MONITOR (On-Chain · Polygon)
      </h3>
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">USDC Balance</div>
          <div className="text-sm font-mono font-bold text-text-primary">$1,247.50</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div className="flex-1 bg-bg-primary rounded-full h-1.5 overflow-hidden">
              <div className="bg-profit h-full rounded-full" style={{ width: '58%' }} />
            </div>
            <span className="text-[9px] text-text-tertiary font-mono">58% free</span>
          </div>
          <div className="text-[10px] text-text-tertiary mt-0.5">$892.30 in collateral · Total: $2,139.80</div>
        </div>
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">POL (Gas Token)</div>
          <div className="text-sm font-mono font-bold text-profit">4.2 POL</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div className="flex-1 bg-bg-primary rounded-full h-1.5 overflow-hidden relative">
              <div className="bg-profit h-full rounded-full" style={{ width: '42%' }} />
              {/* MIN line */}
              <div className="absolute top-0 bottom-0 w-0.5 bg-loss" style={{ left: '20%' }} />
            </div>
            <span className="text-[9px] text-text-tertiary font-mono">$3.44 USD</span>
          </div>
          <div className="text-[10px] text-warning mt-0.5">MIN OPERATIVE: 2.0 POL</div>
        </div>
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">CTF Allowance</div>
          <div className="text-sm font-mono font-bold text-profit">✓ APPROVED</div>
          <div className="text-[10px] text-text-tertiary mt-0.5">Unlimited · CTFExchange</div>
          <div className="text-[9px] text-bg-active mt-0.5 font-mono">0x4D97DC...d697</div>
        </div>
      </div>
    </div>
  );
}

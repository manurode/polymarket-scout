import { useSystemStatus } from '../hooks/useSystemStatus';
import { useRateLimits } from '../hooks/useRateLimits';
import { useReconciliation } from '../hooks/useReconciliation';
import { useWallet } from '../hooks/useWallet';
import { useLatency } from '../hooks/useLatency';
import type { SystemStatus } from '../types';

export function SystemHealth() {
  const status = useSystemStatus(2000);
  const { budgets: rateLimits } = useRateLimits(5000);
  const { data: recon } = useReconciliation(5000);
  const { wallet } = useWallet(5000);

  return (
    <div className="p-4 space-y-4">
      {/* ── Section Header ───────────────────────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          SYSTEM HEALTH
        </h2>
        <div className="flex items-center gap-3">
          <span className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-profit/20 text-profit border border-profit/30">
            ● LIVE CONNECTIONS
          </span>
          <span className="text-[10px] text-text-tertiary font-mono">
            WS: {status.websocket_connected ? 'connected' : 'down'} · {status.tracked_markets_book} markets
          </span>
        </div>
      </div>

      {/* ── Heartbeats + Rate Limits ──────────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <HeartbeatsPanel heartbeats={status.heartbeats} />
        <RateLimitsPanel budgets={rateLimits} />
      </div>

      {/* ── Reconciliation Matrix ──────────────────────────────── */}
      <ReconciliationPanel data={recon} />

      {/* ── Degradation + Latency Budget ───────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <DegradationPanel status={status} />
        <LatencyBudgetPanel />
      </div>

      {/* ── Wallet Monitor ─────────────────────────────────────── */}
      <WalletPanel wallet={wallet} />
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function HeartbeatsPanel({ heartbeats }: { heartbeats: SystemStatus['heartbeats'] }) {
  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">HEARTBEATS</h3>
      <div className="space-y-2">
        {Object.entries(heartbeats || {}).map(([key, beat]) => (
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

function ReconciliationPanel({ data }: { data: { total: number; clean: number; reconciling: number; markets: Array<Record<string, unknown>> } }) {
  const totalCount = data.total || 50;
  const cleanCount = data.clean || 0;
  const reconcilingCount = data.reconciling || 0;
  const cleanPct = totalCount > 0 ? Math.round((cleanCount / totalCount) * 100) : 0;

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
      {data.markets && data.markets.length > 0 && (
        <div className="mt-2 space-y-1">
          {data.markets.slice(0, 3).map((m: any, i: number) => (
            <div key={i} className="flex justify-between text-[10px] font-mono text-text-secondary">
              <span>{m.token_id}</span>
              <span className="text-warning">seq={m.seq_num} gaps={m.gap_count}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DegradationPanel({ status }: { status: SystemStatus }) {
  const mode = status.degradation_metrics?.mode || 'full';
  const canTrade = status.degradation_metrics?.can_trade ?? true;
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
          <div>Active strategies: {(status.active_strategies || []).length}/7</div>
          <div>Trading: {canTrade ? '✓ ENABLED' : '✗ DISABLED'}</div>
          <div>WebSocket: {status.websocket_connected ? '✓ Connected' : '✗ Down'}</div>
        </div>
      </div>
    </div>
  );
}

function LatencyBudgetPanel() {
  const { stages, totalActual, totalBudget, source } = useLatency(5000);
  const isLive = source === 'live' || source === 'estimated';
  const filterStages = stages.filter(s => s.id !== 'radar_scan');
  const calcActual = filterStages.reduce((s, st) => s + st.actual_ms, 0);
  const calcBudget = filterStages.reduce((s, st) => s + st.budget_ms, 0);

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-[10px] text-text-tertiary tracking-wider">
          LATENCY BUDGET (Critical Path)
        </h3>
        <span className={`text-[9px] px-1.5 py-0.5 rounded font-mono ${
          isLive ? 'bg-profit/20 text-profit' : 'bg-bg-tertiary text-text-tertiary'
        }`}>
          {isLive ? '● LIVE' : '○ ESTIMATED'}
        </span>
      </div>
      <div className="space-y-1.5">
        {filterStages.map(st => {
          const pct = st.actual_ms > 0 ? (st.actual_ms / st.budget_ms) * 100 : 0;
          const color = pct > 80 ? 'bg-loss' : pct > 50 ? 'bg-warning' : 'bg-profit';
          return (
            <div key={st.id} className="flex items-center gap-2 text-[10px] font-mono">
              <span className="text-text-tertiary w-28">{st.label}</span>
              <div className="flex-1 bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
                <div className={`${color} h-full rounded-full`} style={{ width: `${Math.min(pct, 100)}%` }} />
              </div>
              <span className="text-text-primary w-24 text-right">
                {st.actual_ms > 0 ? `${st.actual_ms}ms` : '—'} / {st.budget_ms}ms
              </span>
            </div>
          );
        })}
        <div className="border-t border-bg-hover mt-2 pt-1.5 flex justify-between text-[10px] font-mono">
          <span className="text-text-secondary">TOTAL</span>
          <span className="text-text-primary">
            {calcActual > 0 ? `${calcActual}ms` : '—'} / {calcBudget}ms
            {calcActual > 0 && calcBudget > 0 ? ` (${Math.round((calcActual / calcBudget) * 100)}%)` : ''}
          </span>
        </div>
      </div>
    </div>
  );
}

function WalletPanel({ wallet }: { wallet: { usdc_free: number; usdc_collateral: number; usdc_total: number; pol_balance: number; pol_usd_value: number; ctf_allowance: boolean; ctf_contract: string } }) {
  const usdcFreePct = wallet.usdc_total > 0 ? (wallet.usdc_free / wallet.usdc_total) * 100 : 0;
  const polPct = 100; // Simplified

  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-3">
      <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
        WALLET MONITOR (Paper Trading · Virtual)
      </h3>
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">USDC Balance</div>
          <div className="text-sm font-mono font-bold text-text-primary">${wallet.usdc_free.toLocaleString(undefined, {maximumFractionDigits: 2})}</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div className="flex-1 bg-bg-primary rounded-full h-1.5 overflow-hidden">
              <div className="bg-profit h-full rounded-full" style={{ width: `${usdcFreePct}%` }} />
            </div>
            <span className="text-[9px] text-text-tertiary font-mono">{usdcFreePct.toFixed(0)}% free</span>
          </div>
          <div className="text-[10px] text-text-tertiary mt-0.5">${wallet.usdc_collateral.toLocaleString(undefined, {maximumFractionDigits: 2})} in collateral · Total: ${wallet.usdc_total.toLocaleString(undefined, {maximumFractionDigits: 2})}</div>
        </div>
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">POL (Gas Token)</div>
          <div className="text-sm font-mono font-bold text-profit">{wallet.pol_balance.toFixed(1)} POL</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div className="flex-1 bg-bg-primary rounded-full h-1.5 overflow-hidden relative">
              <div className="bg-profit h-full rounded-full" style={{ width: `${polPct}%` }} />
              {/* MIN line */}
              <div className="absolute top-0 bottom-0 w-0.5 bg-loss" style={{ left: '20%' }} />
            </div>
            <span className="text-[9px] text-text-tertiary font-mono">${wallet.pol_usd_value.toFixed(2)} USD</span>
          </div>
          <div className={`text-[10px] mt-0.5 ${wallet.pol_balance < 2 ? 'text-loss animate-pulse-red' : 'text-warning'}`}>
            MIN OPERATIVE: 2.0 POL
          </div>
        </div>
        <div className="bg-bg-tertiary rounded p-2.5">
          <div className="text-[10px] text-text-tertiary mb-1">CTF Allowance</div>
          <div className={`text-sm font-mono font-bold ${wallet.ctf_allowance ? 'text-profit' : 'text-loss'}`}>
            {wallet.ctf_allowance ? '✓ APPROVED' : '✗ NOT APPROVED'}
          </div>
          <div className="text-[10px] text-text-tertiary mt-0.5">Unlimited · CTFExchange</div>
          <div className="text-[9px] text-bg-active mt-0.5 font-mono">{wallet.ctf_contract.slice(0, 8)}...{wallet.ctf_contract.slice(-6)}</div>
        </div>
      </div>
    </div>
  );
}

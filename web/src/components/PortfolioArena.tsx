import { usePortfolio } from '../hooks/usePortfolio';
import type { StrategyRanking, StrategyState } from '../types';

const STATE_ICONS: Record<StrategyState, string> = {
  active: '●', probation: '◐', frozen: '⊘', retired: '⊗',
};

const STATE_CLASSES: Record<StrategyState, string> = {
  active: 'text-profit',
  probation: 'text-info',
  frozen: 'text-text-tertiary',
  retired: 'text-bg-active line-through',
};

export function PortfolioArena() {
  const { strategies, allocation, loading, error } = usePortfolio(10000);

  if (loading && strategies.length === 0) {
    return (
      <div className="p-4 space-y-4 animate-pulse">
        <div className="h-6 w-48 bg-bg-tertiary rounded" />
        <div className="grid grid-cols-3 gap-4">
          <div className="col-span-2 h-64 bg-bg-tertiary rounded" />
          <div className="h-64 bg-bg-tertiary rounded" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 space-y-4">
        <h2 className="text-sm font-bold tracking-wider text-loss">PORTFOLIO ARENA</h2>
        <div className="bg-bg-secondary border border-loss/30 rounded p-4 text-center text-text-secondary">
          ⚠ Failed to load portfolio data: {error}
        </div>
      </div>
    );
  }

  const epochNum = 3; // Would come from system status
  const hasData = strategies.length > 0;

  return (
    <div className="p-4 space-y-4">
      {/* ── Header ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          PORTFOLIO ARENA
        </h2>
        <span className="text-[11px] font-mono text-text-secondary">
          Epoch {epochNum}/6h █░░
        </span>
      </div>

      {/* ── Empty state ────────────────────────────────────────── */}
      {!hasData && (
        <div className="bg-bg-secondary border border-bg-hover rounded p-8 text-center text-text-tertiary">
          No strategy data available — backend may be using mock data
        </div>
      )}

      <div className="grid grid-cols-3 gap-4">
        {/* ── Strategy Rankings ────────────────────────────────── */}
        <div className="col-span-2 bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            TOURNAMENT RANKING (by Sortino Ratio)
          </h3>
          {strategies.length > 0 ? (
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-text-tertiary text-[10px] border-b border-bg-hover">
                  <th className="text-left py-1 pr-2">#</th>
                  <th className="text-left py-1 pr-2">Strategy</th>
                  <th className="text-right py-1 pr-2">Sortino</th>
                  <th className="text-center py-1 pr-2">State</th>
                  <th className="text-right py-1 pr-2">Alloc%</th>
                  <th className="text-right py-1 pr-2">Trades</th>
                  <th className="text-right py-1">WR%</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s, i) => {
                  const sortinoColor = s.sortino >= 2 ? 'text-profit' :
                    s.sortino >= 0 ? 'text-text-primary' : 'text-loss';
                  const rowClass = s.state === 'frozen'
                    ? 'opacity-60'
                    : s.state === 'retired'
                    ? 'opacity-40 line-through'
                    : '';
                  return (
                    <tr key={s.name} className={`border-b border-bg-hover/50 ${rowClass}`}>
                      <td className="py-1 pr-2 text-text-tertiary">{i + 1}</td>
                      <td className="py-1 pr-2 text-text-primary truncate max-w-[120px]">{s.name}</td>
                      <td className={`py-1 pr-2 text-right ${sortinoColor}`}>
                        {s.sortino.toFixed(2)}
                      </td>
                      <td className={`py-1 pr-2 text-center ${STATE_CLASSES[s.state]}`}>
                        {STATE_ICONS[s.state]}
                      </td>
                      <td className="py-1 pr-2 text-right">{s.alloc_pct}%</td>
                      <td className="py-1 pr-2 text-right text-text-secondary">{s.trades}</td>
                      <td className="py-1 text-right text-text-secondary">
                        {Number(s.win_rate * 100).toFixed(0)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="text-center py-8 text-text-tertiary">No strategies</div>
          )}
        </div>

        {/* ── Capital Allocation ───────────────────────────────── */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            CAPITAL ALLOCATION
          </h3>
          {allocation.total_equity > 0 ? (
            <>
              {/* Active capital bar */}
              <div className="mb-2">
                <div className="flex justify-between text-[10px] font-mono mb-0.5">
                  <span className="text-text-secondary">Active</span>
                  <span className="text-profit">
                    ${allocation.active.toLocaleString()} ({Math.round((allocation.active / allocation.total_equity) * 100)}%)
                  </span>
                </div>
                <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
                  <div
                    className="bg-profit h-full rounded-full transition-all duration-700"
                    style={{ width: `${(allocation.active / allocation.total_equity) * 100}%` }}
                  />
                </div>
              </div>

              {/* Frozen capital bar */}
              {allocation.frozen > 0 && (
                <div className="mb-2">
                  <div className="flex justify-between text-[10px] font-mono mb-0.5">
                    <span className="text-text-secondary">Frozen</span>
                    <span className="text-text-tertiary">
                      ${allocation.frozen.toLocaleString()} ({Math.round((allocation.frozen / allocation.total_equity) * 100)}%)
                    </span>
                  </div>
                  <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
                    <div
                      className="bg-text-tertiary h-full rounded-full"
                      style={{ width: `${(allocation.frozen / allocation.total_equity) * 100}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Retired capital bar */}
              {allocation.retired > 0 && (
                <div className="mb-2">
                  <div className="flex justify-between text-[10px] font-mono mb-0.5">
                    <span className="text-text-secondary">Retired</span>
                    <span className="text-bg-active">
                      ${allocation.retired.toLocaleString()}
                    </span>
                  </div>
                  <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
                    <div
                      className="bg-bg-active h-full rounded-full"
                      style={{ width: `${(allocation.retired / allocation.total_equity) * 100}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Equity summary */}
              <div className="mt-4 space-y-1.5">
                <MetricRow label="Total Equity" value={`$${allocation.total_equity.toLocaleString()}`} />
                <MetricRow
                  label="P&L (24h)"
                  value={`${allocation.pnl_24h >= 0 ? '+' : ''}$${allocation.pnl_24h.toLocaleString()} (${allocation.pnl_24h_pct >= 0 ? '+' : ''}${allocation.pnl_24h_pct}%)`}
                  color={allocation.pnl_24h >= 0 ? 'text-profit' : 'text-loss'}
                />
                <MetricRow
                  label="Max Drawdown"
                  value={`-$${Math.abs(allocation.max_drawdown).toLocaleString()} (${allocation.max_drawdown_pct || 0}%)`}
                  color="text-loss"
                />
              </div>
            </>
          ) : (
            <div className="text-center py-8 text-text-tertiary">No equity data</div>
          )}
        </div>
      </div>

      {/* ── Kelly Position Sizing Pipeline ─────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          LAST POSITION SIZING PIPELINE
        </h3>
        <div className="flex items-center gap-2 text-[10px] font-mono flex-wrap">
          <span className="text-text-secondary">Signal: momentum → YES "Trump wins 2028?" @ $0.62</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-text-primary">f_kelly=0.18</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-text-primary">k_dyn=0.14</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-text-primary">f_frac=0.025</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit">$101.25</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit">Ruin:✓</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-text-primary">Corr:-15%</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit font-bold">$86.06 EXECUTED ✓</span>
        </div>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function MetricRow({ label, value, color = 'text-text-primary' }: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="flex justify-between text-[11px] font-mono">
      <span className="text-text-secondary">{label}</span>
      <span className={`font-bold ${color}`}>{value}</span>
    </div>
  );
}

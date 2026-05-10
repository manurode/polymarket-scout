import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Filler,
} from 'chart.js';
import { Line } from 'react-chartjs-2';

import { usePortfolio } from '../hooks/usePortfolio';
import { useSystemStatus } from '../hooks/useSystemStatus';
import { useTradeHistory } from '../hooks/useTradeHistory';
import { useEquityHistory } from '../hooks/useEquityHistory';
import type { StrategyState, StrategyRanking } from '../types';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Filler);

const STATE_ICONS: Record<StrategyState, string> = {
  active: '●', probation: '◐', frozen: '⊘', retired: '⊗',
};

const STATE_CLASSES: Record<StrategyState, string> = {
  active: 'text-profit',
  probation: 'text-info',
  frozen: 'text-text-tertiary',
  retired: 'text-bg-active line-through',
};

const STATE_LABELS: Record<StrategyState, string> = {
  active: 'Active',
  probation: 'Probation',
  frozen: 'Frozen',
  retired: 'Retired',
};

const REASON_LABELS: Record<string, { label: string; color: string }> = {
  tp:      { label: 'TP',      color: 'text-profit' },
  sl:      { label: 'SL',      color: 'text-loss' },
  tau:     { label: 'τ-LIQD', color: 'text-warning' },
  expired: { label: 'EXP',    color: 'text-text-tertiary' },
  manual:  { label: 'MANU',   color: 'text-info' },
};

export function PortfolioArena() {
  const { strategies, allocation, loading, error } = usePortfolio(10000);
  const status = useSystemStatus(10000);
  const { trades, loading: tradesLoading, totalPnl: closedPnl, winRate } = useTradeHistory(15000);
  const {
    points: equityPoints,
    currentEquity,
    totalPnl: equityPnl,
    totalPnlPct,
    maxDrawdown,
    loading: equityLoading,
  } = useEquityHistory(30000);

  if (loading && strategies.length === 0) {
    return (
      <div className="p-4 space-y-4 animate-pulse">
        <div className="h-6 w-48 bg-bg-tertiary rounded" />
        <div className="h-32 bg-bg-tertiary rounded" />
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

  const epochNum = status.portfolio_epoch || 0;
  const hasData = strategies.length > 0;

  const activeCount = strategies.filter(s => s.state === 'active').length;
  const frozenCount = strategies.filter(s => s.state === 'frozen').length;
  const retiredCount = strategies.filter(s => s.state === 'retired').length;

  // ── Equity Chart data ──────────────────────────────────────────────
  const chartLabels = equityPoints.map(p => {
    const d = new Date(p.timestamp * 1000);
    return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
  });
  const chartValues = equityPoints.map(p => p.equity);

  const pnlPositive = equityPnl >= 0;
  const chartData = {
    labels: chartLabels,
    datasets: [
      {
        label: 'Equity ($)',
        data: chartValues,
        borderColor: pnlPositive ? '#22c55e' : '#ef4444',
        backgroundColor: pnlPositive ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        pointHoverRadius: 3,
        fill: true,
        tension: 0.3,
      },
    ],
  };

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx: { parsed: { y: number } }) => `$${ctx.parsed.y.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
        },
        backgroundColor: 'rgba(15,20,30,0.95)',
        borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1,
        titleColor: '#a0aec0',
        bodyColor: '#e2e8f0',
        padding: 8,
      },
    },
    scales: {
      x: {
        ticks: {
          color: '#4a5568',
          font: { size: 9, family: 'ui-monospace, monospace' },
          maxTicksLimit: 8,
          maxRotation: 0,
        },
        grid: { color: 'rgba(255,255,255,0.04)' },
      },
      y: {
        ticks: {
          color: '#4a5568',
          font: { size: 9, family: 'ui-monospace, monospace' },
          callback: (val: string | number) =>
            `$${Number(val).toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
        },
        grid: { color: 'rgba(255,255,255,0.04)' },
      },
    },
  };

  return (
    <div className="p-4 space-y-4">
      {/* ── Header ─────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          PORTFOLIO ARENA
        </h2>
        <div className="flex items-center gap-3">
          <span className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-warning/20 text-warning border border-warning/30">
            📦 PAPER TRADING
          </span>
          <span className="text-[11px] font-mono text-text-secondary">
            Epoch {epochNum}/6h
          </span>
          {hasData && (
            <span className="text-[10px] font-mono text-text-tertiary">
              {activeCount}● {frozenCount > 0 ? `${frozenCount}⊘ ` : ''}{retiredCount > 0 ? `${retiredCount}⊗` : ''}
            </span>
          )}
        </div>
      </div>

      {/* ── Equity Curve ───────────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-[10px] text-text-tertiary tracking-wider">EQUITY CURVE</h3>
          <div className="flex items-center gap-4 text-[10px] font-mono">
            <span className="text-text-secondary">
              Now: <span className="text-text-primary font-bold">
                ${currentEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </span>
            </span>
            <span className={pnlPositive ? 'text-profit' : 'text-loss'}>
              {pnlPositive ? '+' : ''}${equityPnl.toFixed(2)} ({pnlPositive ? '+' : ''}{totalPnlPct.toFixed(2)}%)
            </span>
            {maxDrawdown > 0 && (
              <span className="text-loss">
                DD: -${maxDrawdown.toFixed(2)}
              </span>
            )}
            {equityLoading && (
              <span className="text-text-tertiary animate-pulse">Loading…</span>
            )}
            {equityPoints.length > 0 && (
              <span className="text-text-tertiary">
                {equityPoints.length} pts
              </span>
            )}
          </div>
        </div>
        <div style={{ height: '120px' }}>
          {equityPoints.length >= 2 ? (
            <Line data={chartData} options={chartOptions} />
          ) : (
            <div className="h-full flex items-center justify-center text-text-tertiary text-[11px]">
              {equityLoading
                ? 'Loading equity data…'
                : equityPoints.length === 1
                ? 'Waiting for next snapshot (every 5 min)…'
                : 'No equity data yet — equity logger running…'}
            </div>
          )}
        </div>
      </div>

      {/* ── Empty state ──────────────────────────────────────────── */}
      {!hasData && (
        <div className="bg-bg-secondary border border-bg-hover rounded p-8 text-center text-text-tertiary">
          No strategy data available — backend may be using mock data
        </div>
      )}

      <div className="grid grid-cols-3 gap-4">
        {/* ── Strategy Rankings ───────────────────────────────── */}
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
                  <th className="text-right py-1 pr-2">Sharpe</th>
                  <th className="text-center py-1 pr-2">State</th>
                  <th className="text-right py-1 pr-2">Alloc%</th>
                  <th className="text-right py-1 pr-2">Trades</th>
                  <th className="text-right py-1 pr-2">WR%</th>
                  <th className="text-right py-1">Cum P&L</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s, i) => {
                  const sortinoColor =
                    s.sortino >= 2
                      ? 'text-profit'
                      : s.sortino >= 0
                      ? 'text-text-primary'
                      : 'text-loss';
                  const rowClass =
                    s.state === 'frozen'
                      ? 'opacity-60'
                      : s.state === 'retired'
                      ? 'opacity-40'
                      : '';
                  const cumulPnl = s.cumulative_pnl ?? null;
                  return (
                    <tr
                      key={s.name}
                      className={`border-b border-bg-hover/50 hover:bg-bg-hover/20 transition-colors ${rowClass}`}
                      title={`State: ${STATE_LABELS[s.state]}`}
                    >
                      <td className="py-1 pr-2 text-text-tertiary">{i + 1}</td>
                      <td className={`py-1 pr-2 truncate max-w-[120px] ${s.state === 'retired' ? 'line-through text-text-tertiary' : 'text-text-primary'}`}>
                        {s.name}
                      </td>
                      <td className={`py-1 pr-2 text-right ${sortinoColor}`}>
                        {s.sortino.toFixed(2)}
                      </td>
                      <td className={`py-1 pr-2 text-right ${s.sharpe >= 1 ? 'text-text-primary' : 'text-text-tertiary'}`}>
                        {s.sharpe.toFixed(2)}
                      </td>
                      <td className={`py-1 pr-2 text-center ${STATE_CLASSES[s.state]}`}>
                        {STATE_ICONS[s.state]}
                      </td>
                      <td className="py-1 pr-2 text-right">{s.alloc_pct}%</td>
                      <td className="py-1 pr-2 text-right text-text-secondary">{s.trades}</td>
                      <td className="py-1 pr-2 text-right text-text-secondary">
                        {(s.win_rate * 100).toFixed(0)}%
                      </td>
                      <td className={`py-1 text-right text-[10px] ${
                        cumulPnl == null
                          ? 'text-text-tertiary'
                          : cumulPnl >= 0
                          ? 'text-profit'
                          : 'text-loss'
                      }`}>
                        {cumulPnl != null
                          ? `${cumulPnl >= 0 ? '+' : ''}$${cumulPnl.toFixed(1)}`
                          : '—'}
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

        {/* ── Capital Allocation ────────────────────────────────── */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            CAPITAL ALLOCATION
          </h3>
          {allocation.total_equity > 0 ? (
            <>
              {/* Active capital bar */}
              <AllocationBar
                label="Active"
                value={allocation.active}
                total={allocation.total_equity}
                barClass="bg-profit"
                valueClass="text-profit"
              />

              {/* Frozen capital bar */}
              {allocation.frozen > 0 && (
                <AllocationBar
                  label="Frozen"
                  value={allocation.frozen}
                  total={allocation.total_equity}
                  barClass="bg-text-tertiary"
                  valueClass="text-text-tertiary"
                />
              )}

              {/* Retired capital bar */}
              {allocation.retired > 0 && (
                <AllocationBar
                  label="Retired"
                  value={allocation.retired}
                  total={allocation.total_equity}
                  barClass="bg-bg-active"
                  valueClass="text-bg-active"
                />
              )}

              {/* Equity summary */}
              <div className="mt-4 space-y-1.5">
                <MetricRow
                  label="Total Equity"
                  value={`$${allocation.total_equity.toLocaleString()}`}
                />
                <MetricRow
                  label="P&L (24h)"
                  value={`${allocation.pnl_24h >= 0 ? '+' : ''}$${allocation.pnl_24h.toLocaleString()} (${allocation.pnl_24h_pct >= 0 ? '+' : ''}${allocation.pnl_24h_pct}%)`}
                  color={allocation.pnl_24h >= 0 ? 'text-profit' : 'text-loss'}
                />
                <MetricRow
                  label="Max Drawdown"
                  value={`-$${Math.abs(allocation.max_drawdown).toLocaleString()} (${allocation.max_drawdown_pct ?? 0}%)`}
                  color="text-loss"
                />
              </div>
            </>
          ) : (
            <div className="text-center py-8 text-text-tertiary">No equity data</div>
          )}
        </div>
      </div>

      {/* ── Closed Trades Ledger ─────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-[10px] text-text-tertiary tracking-wider">
            CLOSED TRADES — TRADE LEDGER
          </h3>
          <div className="flex items-center gap-3 text-[10px] font-mono">
            {trades.length > 0 && (
              <>
                <span className="text-text-secondary">
                  {trades.length} trades
                </span>
                <span className={closedPnl >= 0 ? 'text-profit' : 'text-loss'}>
                  Net: {closedPnl >= 0 ? '+' : ''}${closedPnl.toFixed(2)}
                </span>
                <span className="text-text-secondary">
                  WR: {(winRate * 100).toFixed(0)}%
                </span>
              </>
            )}
            {tradesLoading && <span className="text-text-tertiary animate-pulse">Loading…</span>}
          </div>
        </div>

        {trades.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="text-text-tertiary text-[9px] border-b border-bg-hover">
                  <th className="text-left py-1 pr-2">#</th>
                  <th className="text-left py-1 pr-2">Market</th>
                  <th className="text-left py-1 pr-2">Strategy</th>
                  <th className="text-center py-1 pr-2">Side</th>
                  <th className="text-right py-1 pr-2">Size</th>
                  <th className="text-right py-1 pr-2">Entry</th>
                  <th className="text-right py-1 pr-2">Exit</th>
                  <th className="text-right py-1 pr-2">P&L $</th>
                  <th className="text-right py-1 pr-2">P&L %</th>
                  <th className="text-right py-1 pr-2">Slip$</th>
                  <th className="text-right py-1 pr-2">Fee$</th>
                  <th className="text-center py-1 pr-2">Reason</th>
                  <th className="text-right py-1">Closed</th>
                </tr>
              </thead>
              <tbody>
                {trades.map(t => {
                  const pnlColor = t.pnl >= 0 ? 'text-profit' : 'text-loss';
                  const reasonMeta = REASON_LABELS[t.reason] ?? { label: t.reason.toUpperCase(), color: 'text-text-secondary' };
                  const closedDate = new Date(t.closed_at * 1000);
                  const closedStr = `${closedDate.getMonth()+1}/${closedDate.getDate()} ${closedDate.getHours().toString().padStart(2,'0')}:${closedDate.getMinutes().toString().padStart(2,'0')}`;
                  return (
                    <tr
                      key={`${t.id}-${t.closed_at}`}
                      className="border-b border-bg-hover/30 hover:bg-bg-hover/20 transition-colors"
                    >
                      <td className="py-0.5 pr-2 text-text-tertiary">{t.id}</td>
                      <td className="py-0.5 pr-2 text-text-secondary truncate max-w-[140px]" title={t.market}>
                        {t.market.replace(/^\[(MM|MOM|SIG)\]\s*/, '').slice(0, 28)}
                      </td>
                      <td className="py-0.5 pr-2 text-info">{t.strategy.replace(/_/g,' ').slice(0,12)}</td>
                      <td className={`py-0.5 pr-2 text-center font-bold ${t.side === 'YES' ? 'text-profit' : 'text-loss'}`}>
                        {t.side}
                      </td>
                      <td className="py-0.5 pr-2 text-right text-text-secondary">${t.size.toFixed(0)}</td>
                      <td className="py-0.5 pr-2 text-right text-text-secondary">{t.entry.toFixed(3)}</td>
                      <td className="py-0.5 pr-2 text-right text-text-secondary">{t.exit.toFixed(3)}</td>
                      <td className={`py-0.5 pr-2 text-right font-bold ${pnlColor}`}>
                        {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}
                      </td>
                      <td className={`py-0.5 pr-2 text-right ${pnlColor}`}>
                        {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(1)}%
                      </td>
                      <td className="py-0.5 pr-2 text-right text-text-tertiary">${t.slippage_usd.toFixed(3)}</td>
                      <td className="py-0.5 pr-2 text-right text-text-tertiary">${t.commission_usd.toFixed(3)}</td>
                      <td className={`py-0.5 pr-2 text-center font-bold ${reasonMeta.color}`}>
                        {reasonMeta.label}
                      </td>
                      <td className="py-0.5 text-right text-text-tertiary">{closedStr}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-6 text-text-tertiary text-[11px]">
            {tradesLoading
              ? 'Loading trade history…'
              : 'No closed trades yet — leave the bot running overnight!'}
          </div>
        )}
      </div>

      {/* ── Kelly Position Sizing Pipeline ────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-[10px] text-text-tertiary tracking-wider">
            LAST POSITION SIZING PIPELINE
          </h3>
          <span className="text-[8px] px-1.5 py-0.5 rounded font-mono bg-bg-tertiary text-text-tertiary">
            ○ STATIC — no live endpoint yet
          </span>
        </div>
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

function AllocationBar({
  label,
  value,
  total,
  barClass,
  valueClass,
}: {
  label: string;
  value: number;
  total: number;
  barClass: string;
  valueClass: string;
}) {
  const pct = total > 0 ? (value / total) * 100 : 0;
  return (
    <div className="mb-2">
      <div className="flex justify-between text-[10px] font-mono mb-0.5">
        <span className="text-text-secondary">{label}</span>
        <span className={valueClass}>
          ${value.toLocaleString()} ({Math.round(pct)}%)
        </span>
      </div>
      <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
        <div
          className={`${barClass} h-full rounded-full transition-all duration-700`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function MetricRow({
  label,
  value,
  color = 'text-text-primary',
}: {
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

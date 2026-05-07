import { usePositions } from '../hooks/usePositions';

export function RiskMonitor() {
  const { positions, totalPnl, totalValue, liqCount } = usePositions();

  return (
    <div className="p-4 space-y-4">
      <h2 className="text-sm font-bold tracking-wider text-text-primary">
        EXECUTION & RISK MONITOR
      </h2>

      {/* ── Open Positions Table ───────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          OPEN POSITIONS ({positions.length})
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] font-mono">
            <thead>
              <tr className="text-text-tertiary text-[10px] border-b border-bg-hover">
                <th className="text-left py-1 pr-2">#</th>
                <th className="text-left py-1 pr-2">Market</th>
                <th className="text-left py-1 pr-2">Strat</th>
                <th className="text-center py-1 pr-2">Side</th>
                <th className="text-right py-1 pr-2">Size</th>
                <th className="text-right py-1 pr-2">Entry</th>
                <th className="text-right py-1 pr-2">Mark</th>
                <th className="text-right py-1 pr-2">PnL</th>
                <th className="text-right py-1 pr-2">τ%</th>
                <th className="text-right py-1 pr-2">Tox</th>
                <th className="text-center py-1">⚡</th>
              </tr>
            </thead>
            <tbody>
              {[...positions]
                .sort((a, b) => b.tau_pct - a.tau_pct)
                .map(p => {
                  const tauColor = p.tau_pct > 95
                    ? 'text-loss animate-pulse-red'
                    : p.tau_pct > 85 ? 'text-warning font-bold'
                    : p.tau_pct > 70 ? 'text-warning-bright'
                    : 'text-text-secondary';
                  const rowBg = p.tau_pct > 85 ? 'bg-loss/5' : p.tau_pct > 70 ? 'bg-warning/5' : '';
                  return (
                    <tr
                      key={p.id}
                      className={`border-b border-bg-hover/30 ${rowBg} hover:bg-bg-hover/20 transition-colors group cursor-pointer`}
                    >
                      <td className="py-1 pr-2 text-text-tertiary">{p.id}</td>
                      <td className="py-1 pr-2 text-text-primary max-w-[140px] truncate">{p.market}</td>
                      <td className="py-1 pr-2 text-text-tertiary">{p.strategy}</td>
                      <td className={`py-1 pr-2 text-center font-bold ${p.side === 'YES' ? 'text-profit' : 'text-loss'}`}>
                        {p.side}
                      </td>
                      <td className="py-1 pr-2 text-right">${p.size.toFixed(0)}</td>
                      <td className="py-1 pr-2 text-right text-text-secondary">${p.entry.toFixed(2)}</td>
                      <td className="py-1 pr-2 text-right text-text-primary">${p.mark.toFixed(2)}</td>
                      <td className={`py-1 pr-2 text-right font-bold ${p.pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                        {p.pnl >= 0 ? '+' : ''}{p.pnl.toFixed(2)}
                        <span className="text-[9px] ml-0.5">({p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct.toFixed(1)}%)</span>
                      </td>
                      <td className={`py-1 pr-2 text-right ${tauColor}`}>
                        <span className="flex items-center justify-end gap-1">
                          {p.tau_pct}%
                          <span className={`w-1.5 h-1.5 rounded-full ${
                            p.tau_pct > 85 ? 'bg-loss' : p.tau_pct > 70 ? 'bg-warning' : 'bg-profit'
                          }`} />
                        </span>
                      </td>
                      <td className={`py-1 pr-2 text-right ${
                        p.toxicity > 0.7 ? 'text-loss font-bold' : p.toxicity > 0.3 ? 'text-warning' : 'text-text-secondary'
                      }`}>
                        {p.toxicity.toFixed(2)}
                      </td>
                      <td className="py-1 text-center">
                        <button className={`
                          text-[10px] px-1.5 py-0.5 rounded border transition-all
                          ${p.tau_pct > 85 || p.toxicity > 0.7
                            ? 'border-loss/70 text-loss animate-pulse-red opacity-100'
                            : 'border-loss/30 text-loss opacity-0 group-hover:opacity-100'
                          }
                          hover:bg-loss/10
                        `}>
                          {p.tau_pct > 95 ? '¡YA!' : 'FC'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Summary Cards ──────────────────────────────────────── */}
      <div className="grid grid-cols-4 gap-3 text-center">
        <SummaryCard label="Total P&L" value={`${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`} color={totalPnl >= 0 ? 'text-profit' : 'text-loss'} />
        <SummaryCard label="Positions" value={String(positions.length)} />
        <SummaryCard label="Total Value" value={`$${totalValue}`} />
        <SummaryCard label="Liq. Zone" value={`${liqCount} pos`} color={liqCount > 0 ? 'text-warning animate-pulse-red' : 'text-text-secondary'} />
      </div>

      {/* ── Time-Decay Calendar ────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          TIME-DECAY CALENDAR (sorted by τ)
        </h3>
        <div className="space-y-2">
          {[...positions]
            .sort((a, b) => b.tau_pct - a.tau_pct)
            .map(p => (
              <div key={p.id} className="flex items-center gap-3 text-[10px] font-mono">
                <span className="text-text-secondary w-36 truncate">{p.market}</span>
                <span className={`w-8 text-right ${p.tau_pct > 85 ? 'text-loss' : 'text-text-primary'}`}>
                  {p.tau_pct}%
                </span>
                <div className="flex-1 bg-bg-tertiary rounded-full h-2 overflow-hidden relative">
                  <div
                    className={`h-full rounded-full ${
                      p.tau_pct > 95 ? 'bg-loss animate-pulse-red' :
                      p.tau_pct > 85 ? 'bg-loss' :
                      p.tau_pct > 70 ? 'bg-warning' : 'bg-profit'
                    }`}
                    style={{ width: `${p.tau_pct}%` }}
                  />
                  {/* 70% marker */}
                  <div className="absolute top-0 bottom-0 w-px bg-warning/40" style={{ left: '70%' }} />
                  {/* 85% marker */}
                  <div className="absolute top-0 bottom-0 w-px bg-loss/40" style={{ left: '85%' }} />
                </div>
                <div className="w-16 text-right">
                  {p.tau_pct > 85 && <span className="text-loss text-[9px]">⚠ LIQ</span>}
                  {p.tau_pct > 70 && p.tau_pct <= 85 && <span className="text-warning text-[9px]">reduce</span>}
                </div>
              </div>
            ))}
        </div>
      </div>

      {/* ── Correlation Matrix ─────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          CORRELATION MATRIX (open positions)
        </h3>
        <CorrelationMiniMatrix positions={positions} />
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function SummaryCard({ label, value, color = 'text-text-primary' }: {
  label: string; value: string; color?: string;
}) {
  return (
    <div className="bg-bg-secondary border border-bg-hover rounded p-2.5">
      <div className="text-[10px] text-text-tertiary mb-1">{label}</div>
      <div className={`text-sm font-mono font-bold ${color}`}>{value}</div>
    </div>
  );
}

function CorrelationMiniMatrix({ positions }: { positions: { market: string }[] }) {
  const n = Math.min(positions.length, 6);
  if (n === 0) return <div className="text-center py-4 text-text-tertiary">No positions to correlate</div>;

  const labels = positions.slice(0, n).map(p => p.market.slice(0, 12));

  // Simulated correlation data
  const corrData = [
    [1.00, 0.45, 0.12, -0.08, 0.32, 0.55],
    [0.45, 1.00, 0.28, -0.15, 0.20, 0.35],
    [0.12, 0.28, 1.00, 0.05, -0.10, 0.08],
    [-0.08, -0.15, 0.05, 1.00, 0.40, -0.22],
    [0.32, 0.20, -0.10, 0.40, 1.00, 0.15],
    [0.55, 0.35, 0.08, -0.22, 0.15, 1.00],
  ];

  return (
    <div className="overflow-x-auto">
      <table className="text-[9px] font-mono">
        <thead>
          <tr>
            <th className="pr-1"></th>
            {labels.map((l, i) => (
              <th key={i} className="px-1 text-text-tertiary font-normal">{l}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {labels.slice(0, n).map((l, i) => (
            <tr key={i}>
              <td className="pr-1 text-text-tertiary">{l}</td>
              {corrData[i].slice(0, n).map((v, j) => {
                const bg = v === 1 ? 'bg-bg-active' :
                  v > 0.5 ? 'bg-loss/30' :
                  v > 0 ? 'bg-loss/15' :
                  v < -0.3 ? 'bg-info/30' : 'bg-bg-tertiary';
                return (
                  <td key={j} className={`px-1 py-0.5 text-center ${bg} ${j <= i ? '' : 'hidden'}`}>
                    <span className={v > 0.5 ? 'text-loss' : v < -0.3 ? 'text-info' : 'text-text-tertiary'}>
                      {v.toFixed(2)}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      {/* Portfolio avg correlation */}
      <div className="mt-2 pt-1.5 border-t border-bg-hover flex justify-between text-[10px] font-mono">
        <span className="text-text-tertiary">Portfolio Avg Correlation</span>
        <span className="text-warning">0.22</span>
      </div>
    </div>
  );
}

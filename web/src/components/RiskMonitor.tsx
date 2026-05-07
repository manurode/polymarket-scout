export function RiskMonitor() {
  const positions = [
    { id: 1, market: 'Trump wins 2028?', strat: 'MOM', side: 'YES' as const, size: 86, entry: 0.62, mark: 0.67, pnl: 7.12, pnlPct: 8.3, tau: 62, tox: 0.25 },
    { id: 2, market: 'BTC > $100K Dec?', strat: 'CORR', side: 'NO' as const, size: 120, entry: 0.45, mark: 0.42, pnl: -4.80, pnlPct: -4.0, tau: 85, tox: 0.65 },
    { id: 3, market: 'Fed cuts rates?', strat: 'WHL', side: 'YES' as const, size: 45, entry: 0.55, mark: 0.58, pnl: 2.45, pnlPct: 5.4, tau: 40, tox: 0.18 },
    { id: 4, market: 'Crypto bull market?', strat: 'MM', side: 'NO' as const, size: 62, entry: 0.70, mark: 0.68, pnl: -1.24, pnlPct: -2.0, tau: 25, tox: 0.12 },
    { id: 5, market: 'S&P 500 ATH Q3?', strat: 'MOM', side: 'YES' as const, size: 38, entry: 0.35, mark: 0.39, pnl: 4.56, pnlPct: 12.0, tau: 15, tox: 0.05 },
    { id: 6, market: 'Oil price > $80?', strat: 'CNTR', side: 'NO' as const, size: 28, entry: 0.80, mark: 0.85, pnl: -7.50, pnlPct: -26.8, tau: 91, tox: 1.20 },
  ];

  return (
    <div className="p-4 space-y-4">
      <h2 className="text-sm font-bold tracking-wider text-text-primary">
        EXECUTION & RISK MONITOR
      </h2>

      {/* Open Positions Table */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          OPEN POSITIONS
        </h3>
        <table className="w-full text-[11px] font-mono">
          <thead>
            <tr className="text-text-tertiary text-[10px] border-b border-bg-hover">
              <th className="text-left py-1 pr-2">#</th>
              <th className="text-left py-1 pr-2">Market</th>
              <th className="text-center py-1 pr-2">Side</th>
              <th className="text-right py-1 pr-2">Size</th>
              <th className="text-right py-1 pr-2">PnL</th>
              <th className="text-right py-1 pr-2">τ%</th>
              <th className="text-right py-1 pr-2">Tox</th>
              <th className="text-center py-1">⚡</th>
            </tr>
          </thead>
          <tbody>
            {positions.map(p => {
              const tauColor = p.tau > 95 ? 'text-loss animate-pulse-red' : p.tau > 85 ? 'text-warning' : p.tau > 70 ? 'text-warning-bright' : 'text-text-secondary';
              return (
                <tr
                  key={p.id}
                  className={`
                    border-b border-bg-hover/50
                    ${p.tau > 85 ? 'bg-loss/5' : ''}
                    ${p.tau > 70 ? 'bg-warning/5' : ''}
                  `}
                >
                  <td className="py-1 pr-2 text-text-tertiary">{p.id}</td>
                  <td className="py-1 pr-2 text-text-primary max-w-40 truncate">{p.market}</td>
                  <td className={`py-1 pr-2 text-center ${p.side === 'YES' ? 'text-profit' : 'text-loss'}`}>
                    {p.side}
                  </td>
                  <td className="py-1 pr-2 text-right">${p.size}</td>
                  <td className={`py-1 pr-2 text-right ${p.pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                    {p.pnl >= 0 ? '+' : ''}{p.pnl.toFixed(2)}
                  </td>
                  <td className={`py-1 pr-2 text-right ${tauColor}`}>{p.tau}%</td>
                  <td className={`py-1 pr-2 text-right ${p.tox > 0.7 ? 'text-loss' : p.tox > 0.3 ? 'text-warning' : 'text-text-secondary'}`}>
                    {p.tox.toFixed(2)}
                  </td>
                  <td className="py-1 text-center">
                    <button className="text-[10px] px-1.5 py-0.5 rounded border border-loss/30 text-loss hover:bg-loss/10 opacity-0 group-hover:opacity-100 transition-opacity">
                      FC
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Time-Decay Calendar */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          TIME-DECAY CALENDAR (next 48h)
        </h3>
        <div className="space-y-2">
          {positions
            .sort((a, b) => b.tau - a.tau)
            .map(p => (
              <div key={p.id} className="flex items-center gap-3 text-[10px] font-mono">
                <span className="text-text-secondary w-32 truncate">{p.market}</span>
                <span className="text-text-tertiary w-8">{p.tau}%</span>
                <div className="flex-1 bg-bg-tertiary rounded-full h-2 overflow-hidden">
                  <div
                    className={`h-full rounded-full ${p.tau > 85 ? 'bg-loss' : p.tau > 70 ? 'bg-warning' : 'bg-profit'}`}
                    style={{ width: `${p.tau}%` }}
                  />
                </div>
                {p.tau > 85 && <span className="text-loss text-[9px]">⚠ LIQ</span>}
              </div>
            ))}
        </div>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-4 gap-2 text-center">
        <div className="bg-bg-secondary rounded p-2">
          <div className="text-[10px] text-text-tertiary">Total P&L</div>
          <div className="text-sm font-mono text-profit">+$1.23</div>
        </div>
        <div className="bg-bg-secondary rounded p-2">
          <div className="text-[10px] text-text-tertiary">Positions</div>
          <div className="text-sm font-mono text-text-primary">6</div>
        </div>
        <div className="bg-bg-secondary rounded p-2">
          <div className="text-[10px] text-text-tertiary">Value</div>
          <div className="text-sm font-mono text-text-primary">$379</div>
        </div>
        <div className="bg-bg-secondary rounded p-2">
          <div className="text-[10px] text-text-tertiary">Liq. Zone</div>
          <div className="text-sm font-mono text-warning">2 pos</div>
        </div>
      </div>
    </div>
  );
}

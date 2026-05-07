export function PortfolioArena() {
  const strategies = [
    { name: 'corr_arb', sortino: 3.21, state: 'active', alloc: 34, trades: 45, wr: 68 },
    { name: 'whale_follow', sortino: 2.45, state: 'active', alloc: 22, trades: 32, wr: 72 },
    { name: 'market_making', sortino: 1.87, state: 'active', alloc: 17, trades: 128, wr: 62 },
    { name: 'momentum_follow', sortino: 0.92, state: 'active', alloc: 11, trades: 24, wr: 55 },
    { name: 'consensus_break', sortino: 0.45, state: 'probation', alloc: 8, trades: 18, wr: 50 },
    { name: 'contrarian', sortino: -0.21, state: 'frozen', alloc: 5, trades: 15, wr: 40 },
    { name: 'volume_breakout', sortino: -0.85, state: 'retired', alloc: 3, trades: 8, wr: 25 },
  ];

  const stateIcons: Record<string, string> = {
    active: '●', probation: '◐', frozen: '⊘', retired: '⊗',
  };

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          PORTFOLIO ARENA
        </h2>
        <span className="text-[11px] font-mono text-text-secondary">
          Epoch 3/6h █░░
        </span>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* Strategy Rankings */}
        <div className="col-span-2 bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            TOURNAMENT RANKING (by Sortino)
          </h3>
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
              {strategies.map((s, i) => (
                <tr
                  key={s.name}
                  className={`
                    border-b border-bg-hover/50
                    ${s.state === 'frozen' ? 'text-text-tertiary' : ''}
                    ${s.state === 'retired' ? 'text-bg-active line-through' : ''}
                  `}
                >
                  <td className="py-1 pr-2 text-text-tertiary">{i + 1}</td>
                  <td className="py-1 pr-2 text-text-primary">{s.name}</td>
                  <td className={`py-1 pr-2 text-right ${s.sortino >= 2 ? 'text-profit' : s.sortino >= 0 ? 'text-text-primary' : 'text-loss'}`}>
                    {s.sortino.toFixed(2)}
                  </td>
                  <td className="py-1 pr-2 text-center">{stateIcons[s.state]}</td>
                  <td className="py-1 pr-2 text-right">{s.alloc}%</td>
                  <td className="py-1 pr-2 text-right text-text-secondary">{s.trades}</td>
                  <td className="py-1 text-right text-text-secondary">{s.wr}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Capital Allocation */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            CAPITAL ALLOCATION
          </h3>
          <div className="space-y-3">
            <div>
              <div className="flex justify-between text-[10px] font-mono mb-0.5">
                <span>Active</span><span className="text-profit">$2,847 (70%)</span>
              </div>
              <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
                <div className="bg-profit h-full rounded-full" style={{ width: '70%' }} />
              </div>
            </div>
            <div>
              <div className="flex justify-between text-[10px] font-mono mb-0.5">
                <span>Frozen</span><span className="text-text-tertiary">$1,203 (30%)</span>
              </div>
              <div className="bg-bg-tertiary rounded-full h-3 overflow-hidden">
                <div className="bg-text-tertiary h-full rounded-full" style={{ width: '30%' }} />
              </div>
            </div>
          </div>

          <div className="mt-4 space-y-1.5">
            <div className="flex justify-between text-[11px] font-mono">
              <span className="text-text-secondary">Total Equity</span>
              <span className="text-text-primary font-bold">$4,050</span>
            </div>
            <div className="flex justify-between text-[11px] font-mono">
              <span className="text-text-secondary">P&L (24h)</span>
              <span className="text-profit">+$127 (+3.2%)</span>
            </div>
            <div className="flex justify-between text-[11px] font-mono">
              <span className="text-text-secondary">Max Drawdown</span>
              <span className="text-loss">-$89 (-2.2%)</span>
            </div>
          </div>
        </div>
      </div>

      {/* Kelly Pipeline */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          LAST POSITION SIZING PIPELINE
        </h3>
        <div className="flex items-center gap-2 text-[10px] font-mono flex-wrap">
          <span className="text-text-secondary">Signal: momentum → YES "Trump wins 2028?" @ $0.62</span>
          <span className="text-bg-active mx-1">→</span>
          <span>f_kelly=0.18</span>
          <span className="text-bg-active mx-1">→</span>
          <span>k_dyn=0.14</span>
          <span className="text-bg-active mx-1">→</span>
          <span>f_frac=0.025</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit">$101.25</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit">Ruin:✓</span>
          <span className="text-bg-active mx-1">→</span>
          <span>Corr:-15%</span>
          <span className="text-bg-active mx-1">→</span>
          <span className="text-profit font-bold">$86.06 EXECUTED ✓</span>
        </div>
      </div>
    </div>
  );
}

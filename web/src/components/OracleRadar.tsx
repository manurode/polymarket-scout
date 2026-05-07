import { useWhales } from '../hooks/useWhales';
import { useSystemStatus } from '../hooks/useSystemStatus';

export function OracleRadar() {
  const { alphaWhales, whaleFlow, loading, error } = useWhales();
  const status = useSystemStatus(5000);

  return (
    <div className="p-4 space-y-4">
      <h2 className="text-sm font-bold tracking-wider text-text-primary">
        ORACLE RADAR
      </h2>

      {/* ── Spoofing Heatmap ──────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            SPOOFING HEATMAP (Markets × Time)
          </h3>
          <div className="space-y-1.5">
            <SpoofRow market="Trump wins 2028?" spoofScore={0.62} classification="PROBABLE" />
            <SpoofRow market="BTC > $100K Dec?" spoofScore={0.35} classification="SUSPICIOUS" />
            <SpoofRow market="Fed cuts rates?" spoofScore={0.12} classification="NORMAL" />
            <SpoofRow market="Crypto bull market?" spoofScore={0.48} classification="SUSPICIOUS" />
            <SpoofRow market="S&P 500 ATH Q3?" spoofScore={0.08} classification="NORMAL" />
            <SpoofRow market="Oil price > $80?" spoofScore={0.71} classification="CONFIRMED" />
          </div>
        </div>

        {/* ── Spoof Detail ────────────────────────────────────── */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            SPOOF DETAIL — "Trump wins 2028?"
          </h3>
          <div className="space-y-2 text-[11px] font-mono">
            <div className="flex justify-between">
              <span className="text-text-tertiary">OBI</span>
              <span className="text-profit">+0.62</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">TFI</span>
              <span className="text-text-primary">+0.15</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Divergence</span>
              <span className="text-warning font-bold text-base">0.47</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Spoof Score</span>
              <span className="text-warning">0.62 ◉ PROBABLE</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Cancel Rate</span>
              <span className="text-loss">3.2× avg ⚠</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Direction</span>
              <span className="text-profit">▲ BUY (TFI)</span>
            </div>

            {/* Action buttons */}
            <div className="flex gap-2 pt-2 border-t border-bg-hover mt-2">
              <button className="flex-1 px-3 py-1.5 rounded border border-warning/50 text-warning text-[10px] font-medium hover:bg-warning/10 transition-colors">
                ⏸ HALT TRADING
              </button>
              <button className="flex-1 px-3 py-1.5 rounded border border-loss/50 text-loss text-[10px] font-medium hover:bg-loss/10 transition-colors">
                ⚡ FORCE CLOSE
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── DOM Chart ──────────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          DEPTH OF MARKET (DOM Chart)
        </h3>
        <DOMChart />
      </div>

      {/* ── Whale Tracker ──────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          WHALE TRACKER
        </h3>
        {loading ? (
          <div className="text-center py-6 text-text-tertiary animate-pulse">Loading whale data...</div>
        ) : error ? (
          <div className="text-center py-6 text-loss">⚠ {error}</div>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            {/* Alpha Whales Table */}
            <div>
              <h4 className="text-[10px] text-whale mb-1.5 font-mono">
                ALPHA WHALES · {alphaWhales.length}α
              </h4>
              {alphaWhales.length > 0 ? (
                <table className="w-full text-[10px] font-mono">
                  <thead>
                    <tr className="text-text-tertiary border-b border-bg-hover">
                      <th className="text-left py-1 pr-1">Wallet</th>
                      <th className="text-right py-1 pr-1">Score</th>
                      <th className="text-right py-1 pr-1">P&L</th>
                      <th className="text-right py-1 pr-1">WR%</th>
                      <th className="text-right py-1">Tr/w</th>
                    </tr>
                  </thead>
                  <tbody>
                    {alphaWhales.map((w, i) => (
                      <tr key={i} className="border-b border-bg-hover/30 hover:bg-bg-hover/30 transition-colors cursor-pointer">
                        <td className="py-1 pr-1 text-text-primary">{w.wallet}</td>
                        <td className="py-1 pr-1 text-right">
                          <span className={w.score >= 0.9 ? 'text-whale font-bold' : 'text-text-secondary'}>
                            {w.score.toFixed(2)}
                          </span>
                        </td>
                        <td className={`py-1 pr-1 text-right ${w.total_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                          ${(w.total_pnl / 1000).toFixed(0)}K
                        </td>
                        <td className="py-1 pr-1 text-right text-text-secondary">
                          {Number(w.win_rate * 100).toFixed(0)}%
                        </td>
                        <td className="py-1 text-right text-text-secondary">
                          {w.trades_per_week.toFixed(1)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="text-center py-4 text-text-tertiary">No alpha whales tracked</div>
              )}
            </div>

            {/* Whale Flow by Market */}
            <div>
              <h4 className="text-[10px] text-whale mb-1.5 font-mono">
                WHALE FLOW (1h)
              </h4>
              {whaleFlow.markets.length > 0 ? (
                <div className="space-y-1.5">
                  {whaleFlow.markets.map((m, i) => (
                    <div key={i} className="flex items-center gap-2 text-[10px] font-mono">
                      <span className="flex-1 text-text-secondary truncate">{m.market}</span>
                      <div className="w-24 bg-bg-tertiary rounded-full h-2 overflow-hidden">
                        <div
                          className={`h-full rounded-full ${m.direction === 'buy' ? 'bg-profit' : 'bg-loss'}`}
                          style={{ width: `${Math.min(Math.abs(m.flow_usd) / 150, 100)}%` }}
                        />
                      </div>
                      <span className={`w-14 text-right ${m.flow_usd >= 0 ? 'text-profit' : 'text-loss'}`}>
                        {m.flow_usd >= 0 ? '+' : ''}${Math.abs(m.flow_usd / 1000).toFixed(0)}K
                      </span>
                    </div>
                  ))}
                  <div className="flex justify-between mt-2 pt-1.5 border-t border-bg-hover text-[10px] font-mono">
                    <span className="text-text-tertiary">CM (avg)</span>
                    <span className={`font-bold ${whaleFlow.avg_conviction_multiplier >= 1.0 ? 'text-profit' : 'text-loss'}`}>
                      {whaleFlow.avg_conviction_multiplier.toFixed(2)}
                      {whaleFlow.avg_conviction_multiplier >= 1.0 ? ' ▲ bullish' : ' ▼ bearish'}
                    </span>
                  </div>
                </div>
              ) : (
                <div className="text-center py-4 text-text-tertiary">No whale flow detected</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ── Wallet Clustering ──────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          WALLET CLUSTERING (Anti-Sybil)
        </h3>
        <div className="text-[11px] text-text-tertiary font-mono text-center py-4">
          Clustering analysis — updated every 24h
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function SpoofRow({ market, spoofScore, classification }: {
  market: string;
  spoofScore: number;
  classification: string;
}) {
  const color = spoofScore >= 0.7 ? 'text-loss animate-pulse-red' :
    spoofScore >= 0.5 ? 'text-warning' :
    spoofScore >= 0.3 ? 'text-warning-bright' : 'text-text-secondary';

  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className="w-36 truncate text-text-secondary">{market}</span>
      <div className="flex-1 bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
        <div
          className={`h-full rounded-full ${
            spoofScore >= 0.7 ? 'bg-loss' : spoofScore >= 0.5 ? 'bg-warning' : spoofScore >= 0.3 ? 'bg-warning-bright' : 'bg-bg-active'
          }`}
          style={{ width: `${Math.min(spoofScore * 100, 100)}%` }}
        />
      </div>
      <span className={`w-16 text-right ${color}`}>
        {spoofScore.toFixed(2)}
      </span>
      <span className={`w-20 text-right text-[9px] ${color}`}>
        {classification}
      </span>
    </div>
  );
}

function DOMChart() {
  return (
    <div className="relative h-48 bg-bg-tertiary rounded overflow-hidden">
      {/* Mid-price line */}
      <div className="absolute top-0 bottom-0 left-1/2 w-px bg-bg-active border-dashed" />

      {/* Simulated DOM bars */}
      <div className="absolute inset-0 flex items-end">
        {/* Bids (left side, green) */}
        <div className="w-1/2 flex items-end justify-end gap-px px-4 pb-2">
          {[85, 70, 60, 45, 38, 25, 18, 12, 8, 5].map((h, i) => (
            <div
              key={`bid-${i}`}
              className="w-[8%] bg-profit/60 rounded-t-sm"
              style={{ height: `${h}%` }}
            />
          ))}
        </div>
        {/* Asks (right side, red) */}
        <div className="w-1/2 flex items-end gap-px px-4 pb-2">
          {[5, 10, 15, 22, 30, 35, 42, 55, 65, 80].map((h, i) => (
            <div
              key={`ask-${i}`}
              className="w-[8%] bg-loss/60 rounded-t-sm"
              style={{ height: `${h}%` }}
            />
          ))}
        </div>
      </div>

      {/* OBI/TFI labels */}
      <div className="absolute top-2 left-4 text-[10px] font-mono text-text-tertiary">
        OBI: <span className="text-profit">-0.62</span>
      </div>
      <div className="absolute top-2 right-4 text-[10px] font-mono text-text-tertiary">
        TFI: <span className="text-profit">-0.15</span>
      </div>
      <div className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[11px] font-mono font-bold text-warning">
        DIV: 0.47 ⚠
      </div>

      {/* Label */}
      <div className="absolute bottom-2 left-4 text-[9px] text-text-tertiary font-mono">BIDS</div>
      <div className="absolute bottom-2 right-4 text-[9px] text-text-tertiary font-mono">ASKS</div>
    </div>
  );
}

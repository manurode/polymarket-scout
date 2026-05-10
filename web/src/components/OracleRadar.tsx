import { useWhales } from '../hooks/useWhales';
import { useSpoofing } from '../hooks/useSpoofing';
import { useRadarMarkets } from '../hooks/useRadarMarkets';
import { useBookSnapshot } from '../hooks/useBookSnapshot';

export function OracleRadar() {
  const { alphaWhales, whaleFlow, loading, error } = useWhales();
  const { data: spoofData } = useSpoofing(10000);
  const radarData = useRadarMarkets(30000);

  // Pick the highest-score market for the Spoof Detail panel
  const detailMarket =
    [...spoofData.markets].sort((a, b) => b.spoof_score - a.spoof_score)[0] ||
    { token_id: 'N/A', spoof_score: 0, classification: 'NORMAL', requires_pause: false };

  const isLiveData = radarData.source === 'gamma_api';

  // Summary stats
  const probableCount = spoofData.markets.filter(m => m.spoof_score >= 0.7).length;
  const suspiciousCount = spoofData.markets.filter(
    m => m.spoof_score >= 0.5 && m.spoof_score < 0.7,
  ).length;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">ORACLE RADAR</h2>
        <div className="flex items-center gap-2">
          <span
            className={`text-[10px] px-2 py-0.5 rounded-full font-mono ${
              isLiveData ? 'bg-profit/20 text-profit' : 'bg-warning/20 text-warning'
            }`}
          >
            {isLiveData ? '● LIVE DATA' : `○ ${radarData.source.toUpperCase()}`}
          </span>
        </div>
      </div>

      {/* ── Live Markets from Radar ─────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-[10px] text-text-tertiary tracking-wider">
            UPCOMING HEATMAP (Markets from Gamma API)
          </h3>
          <span className="text-[9px] text-text-tertiary font-mono">
            {radarData.count} markets
          </span>
        </div>
        {radarData.count > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-[10px] font-mono">
              <thead>
                <tr className="text-text-tertiary border-b border-bg-hover">
                  <th className="text-left py-1 pr-2">Market</th>
                  <th className="text-right py-1 pr-2">Price</th>
                  <th className="text-right py-1 pr-2">Spread</th>
                  <th className="text-right py-1 pr-2">Volume 24h</th>
                  <th className="text-right py-1">Liquidity</th>
                </tr>
              </thead>
              <tbody>
                {radarData.markets.slice(0, 8).map((m, i) => (
                  <tr
                    key={i}
                    className="border-b border-bg-hover/30 hover:bg-bg-hover/30 transition-colors"
                  >
                    <td
                      className="py-1.5 pr-2 text-text-primary truncate max-w-[200px]"
                      title={m.question}
                    >
                      {m.question}
                    </td>
                    <td className="py-1.5 pr-2 text-right text-text-secondary">
                      {m.mid_price != null ? `$${m.mid_price.toFixed(3)}` : 'N/A'}
                    </td>
                    <td className="py-1.5 pr-2 text-right">
                      {m.spread != null ? (
                        <span
                          className={
                            m.spread < 0.02
                              ? 'text-profit'
                              : m.spread > 0.05
                              ? 'text-warning'
                              : 'text-text-secondary'
                          }
                        >
                          {(m.spread * 100).toFixed(2)}%
                        </span>
                      ) : (
                        'N/A'
                      )}
                    </td>
                    <td className="py-1.5 pr-2 text-right text-text-secondary">
                      ${(m.volume_24h / 1000).toFixed(1)}K
                    </td>
                    <td className="py-1.5 text-right text-text-secondary">
                      ${m.liquidity.toFixed(0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-6 text-text-tertiary text-[11px]">
            {radarData.error ? (
              <span className="text-loss">⚠ {radarData.error}</span>
            ) : (
              'No markets available — scanner not connected'
            )}
          </div>
        )}
      </div>

      {/* ── Spoofing Heatmap + Detail ───────────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[10px] text-text-tertiary tracking-wider">
              SPOOFING HEATMAP
            </h3>
            {/* Summary badges */}
            <div className="flex gap-1.5">
              {probableCount > 0 && (
                <span className="text-[8px] px-1.5 py-0.5 rounded font-mono bg-loss/20 text-loss">
                  {probableCount} PROBABLE
                </span>
              )}
              {suspiciousCount > 0 && (
                <span className="text-[8px] px-1.5 py-0.5 rounded font-mono bg-warning/20 text-warning">
                  {suspiciousCount} SUSPICIOUS
                </span>
              )}
            </div>
          </div>
          <div className="space-y-1.5">
            {spoofData.markets.length > 0 ? (
              // BUG FIX: Sort by score descending so highest-risk markets appear first
              [...spoofData.markets]
                .sort((a, b) => b.spoof_score - a.spoof_score)
                .map((m, i) => (
                  <SpoofRow
                    key={i}
                    market={m.token_id}
                    spoofScore={m.spoof_score}
                    classification={m.classification}
                    requiresPause={m.requires_pause}
                  />
                ))
            ) : (
              <div className="text-center py-4 text-text-tertiary text-[11px]">
                No spoofing data
              </div>
            )}
          </div>
        </div>

        {/* ── Spoof Detail ─────────────────────────────────────── */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            SPOOF DETAIL — highest risk market
          </h3>
          <div className="space-y-2 text-[11px] font-mono">
            {/* Market ID */}
            <div className="flex justify-between">
              <span className="text-text-tertiary">Market ID</span>
              <span className="text-text-primary truncate max-w-[180px]" title={detailMarket.token_id}>
                {detailMarket.token_id}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Spoof Score</span>
              <span
                className={
                  detailMarket.spoof_score >= 0.7
                    ? 'text-loss font-bold'
                    : detailMarket.spoof_score >= 0.5
                    ? 'text-warning font-bold'
                    : 'text-profit'
                }
              >
                {detailMarket.spoof_score.toFixed(2)}{' '}
                <span className="text-[10px]">{detailMarket.classification}</span>
              </span>
            </div>
            {/* Spoof score gauge */}
            <div className="bg-bg-tertiary rounded-full h-2 overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${
                  detailMarket.spoof_score >= 0.7
                    ? 'bg-loss'
                    : detailMarket.spoof_score >= 0.5
                    ? 'bg-warning'
                    : 'bg-profit'
                }`}
                style={{ width: `${Math.min(detailMarket.spoof_score * 100, 100)}%` }}
              />
            </div>
            <div className="flex justify-between">
              <span className="text-text-tertiary">Requires Pause</span>
              <span className={detailMarket.requires_pause ? 'text-loss' : 'text-profit'}>
                {detailMarket.requires_pause ? 'YES ⚠' : 'NO'}
              </span>
            </div>
            {/* Thresholds reference */}
            <div className="text-[9px] text-text-tertiary pt-1 border-t border-bg-hover">
              NORMAL &lt; 0.3 · SUSPICIOUS 0.3–0.5 · PROBABLE 0.5–0.7 · HIGH ≥ 0.7
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

      {/* ── DOM Chart ───────────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          DEPTH OF MARKET (DOM Chart)
        </h3>
        <DOMChart />
      </div>

      {/* ── Whale Tracker ────────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">WHALE TRACKER</h3>
        {loading ? (
          <div className="text-center py-6 text-text-tertiary animate-pulse">
            Loading whale data...
          </div>
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
                      <tr
                        key={i}
                        className="border-b border-bg-hover/30 hover:bg-bg-hover/30 transition-colors cursor-pointer"
                      >
                        <td className="py-1 pr-1 text-text-primary">{w.wallet}</td>
                        <td className="py-1 pr-1 text-right">
                          <span
                            className={
                              w.score >= 0.9 ? 'text-whale font-bold' : 'text-text-secondary'
                            }
                          >
                            {w.score.toFixed(2)}
                          </span>
                        </td>
                        <td
                          className={`py-1 pr-1 text-right ${
                            w.total_pnl >= 0 ? 'text-profit' : 'text-loss'
                          }`}
                        >
                          ${(w.total_pnl / 1000).toFixed(0)}K
                        </td>
                        <td className="py-1 pr-1 text-right text-text-secondary">
                          {(w.win_rate * 100).toFixed(0)}%
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
              <h4 className="text-[10px] text-whale mb-1.5 font-mono">WHALE FLOW (1h)</h4>
              {whaleFlow.markets.length > 0 ? (
                <div className="space-y-1.5">
                  {whaleFlow.markets.map((m, i) => (
                    <div key={i} className="flex items-center gap-2 text-[10px] font-mono">
                      <span className="flex-1 text-text-secondary truncate">{m.market}</span>
                      <div className="w-24 bg-bg-tertiary rounded-full h-2 overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            m.direction === 'buy' ? 'bg-profit' : 'bg-loss'
                          }`}
                          style={{
                            width: `${Math.min(Math.abs(m.flow_usd) / 150, 100)}%`,
                          }}
                        />
                      </div>
                      <span
                        className={`w-14 text-right ${
                          m.flow_usd >= 0 ? 'text-profit' : 'text-loss'
                        }`}
                      >
                        {m.flow_usd >= 0 ? '+' : ''}$
                        {Math.abs(m.flow_usd / 1000).toFixed(0)}K
                      </span>
                    </div>
                  ))}
                  <div className="flex justify-between mt-2 pt-1.5 border-t border-bg-hover text-[10px] font-mono">
                    <span className="text-text-tertiary">CM (avg)</span>
                    <span
                      className={`font-bold ${
                        whaleFlow.avg_conviction_multiplier >= 1.0 ? 'text-profit' : 'text-loss'
                      }`}
                    >
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
      {/* NOTE: Wallet Clustering panel removed — was a static placeholder with no data.
               Re-add when /api/oracles/wallet-clustering endpoint is available. */}
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────

function SpoofRow({
  market,
  spoofScore,
  classification,
  requiresPause,
}: {
  market: string;
  spoofScore: number;
  classification: string;
  requiresPause: boolean;
}) {
  const color =
    spoofScore >= 0.7
      ? 'text-loss animate-pulse-red'
      : spoofScore >= 0.5
      ? 'text-warning'
      : spoofScore >= 0.3
      ? 'text-warning-bright'
      : 'text-text-secondary';

  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className="w-36 truncate text-text-secondary" title={market}>
        {market}
      </span>
      <div className="flex-1 bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
        <div
          className={`h-full rounded-full ${
            spoofScore >= 0.7
              ? 'bg-loss'
              : spoofScore >= 0.5
              ? 'bg-warning'
              : spoofScore >= 0.3
              ? 'bg-warning-bright'
              : 'bg-bg-active'
          }`}
          style={{ width: `${Math.min(spoofScore * 100, 100)}%` }}
        />
      </div>
      <span className={`w-10 text-right ${color}`}>{spoofScore.toFixed(2)}</span>
      <span className={`w-20 text-right text-[9px] ${color}`}>{classification}</span>
      {requiresPause && (
        <span className="text-loss text-[9px]">⏸</span>
      )}
    </div>
  );
}

function DOMChart() {
  const { book, source } = useBookSnapshot(10000);
  const isLive = source === 'clob_ws';

  const bids = (book?.bids || []).slice(0, 10);
  const asks = (book?.asks || []).slice(0, 10);

  const maxBidSize = bids.length > 0 ? Math.max(...bids.map(b => b.size)) : 1;
  const maxAskSize = asks.length > 0 ? Math.max(...asks.map(a => a.size)) : 1;
  const maxSize = Math.max(maxBidSize, maxAskSize, 1);

  const obi = book?.obi ?? null;
  const tfi = book?.tfi ?? null;

  return (
    <div className="relative h-48 bg-bg-tertiary rounded overflow-hidden">
      {/* Mid-price divider */}
      <div className="absolute top-0 bottom-0 left-1/2 w-px bg-bg-active border-dashed" />

      <div className="absolute inset-0 flex items-end">
        {/* Bids (left side, green) */}
        <div className="w-1/2 flex items-end justify-end gap-px px-4 pb-2">
          {bids.length > 0
            ? bids.map((b, i) => (
                <div
                  key={`bid-${i}`}
                  className="bg-profit/60 rounded-t-sm min-w-[6px]"
                  style={{ height: `${Math.max(3, (b.size / maxSize) * 100)}%`, width: '7%' }}
                />
              ))
            : Array.from({ length: 10 }).map((_, i) => (
                <div
                  key={`bid-e-${i}`}
                  className="w-[7%] bg-bg-active/20 rounded-t-sm"
                  style={{ height: `${Math.max(3, (1 - i * 0.1) * 60)}%` }}
                />
              ))}
        </div>
        {/* Asks (right side, red) */}
        <div className="w-1/2 flex items-end gap-px px-4 pb-2">
          {asks.length > 0
            ? asks.map((a, i) => (
                <div
                  key={`ask-${i}`}
                  className="bg-loss/60 rounded-t-sm min-w-[6px]"
                  style={{ height: `${Math.max(3, (a.size / maxSize) * 100)}%`, width: '7%' }}
                />
              ))
            : Array.from({ length: 10 }).map((_, i) => (
                <div
                  key={`ask-e-${i}`}
                  className="w-[7%] bg-bg-active/20 rounded-t-sm"
                  style={{ height: `${Math.max(3, (i * 0.1) * 60)}%` }}
                />
              ))}
        </div>
      </div>

      {/* OBI/TFI labels */}
      <div className="absolute top-2 left-4 text-[10px] font-mono text-text-tertiary">
        OBI:{' '}
        {obi != null ? <span className="text-profit">{obi.toFixed(2)}</span> : '—'}
      </div>
      <div className="absolute top-2 right-4 text-[10px] font-mono text-text-tertiary">
        TFI:{' '}
        {tfi != null ? <span className="text-profit">{tfi.toFixed(2)}</span> : '—'}
      </div>
      <div className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[11px] font-mono font-bold">
        {isLive ? (
          <span className="text-profit">
            {book?.mid_price != null ? `$${book.mid_price.toFixed(3)}` : '● LIVE'}
          </span>
        ) : (
          <span className="text-text-tertiary">○ NO BOOK DATA</span>
        )}
      </div>

      {/* Source badge */}
      <div className="absolute top-2 left-1/2 -translate-x-1/2">
        <span
          className={`text-[8px] px-1.5 py-0.5 rounded font-mono ${
            isLive ? 'bg-profit/20 text-profit' : 'bg-bg-tertiary text-text-tertiary'
          }`}
        >
          {isLive ? '● LIVE BOOK' : '○ SIMULATED'}
        </span>
      </div>

      <div className="absolute bottom-2 left-4 text-[9px] text-text-tertiary font-mono">BIDS</div>
      <div className="absolute bottom-2 right-4 text-[9px] text-text-tertiary font-mono">ASKS</div>
    </div>
  );
}

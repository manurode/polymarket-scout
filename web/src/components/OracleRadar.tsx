export function OracleRadar() {
  return (
    <div className="p-4 space-y-4">
      <h2 className="text-sm font-bold tracking-wider text-text-primary">
        ORACLE RADAR
      </h2>

      {/* Spoofing Heatmap placeholder */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          SPOOFING HEATMAP (Markets × Time Windows)
        </h3>
        <div className="text-[11px] text-text-tertiary font-mono text-center py-8">
          Spoofing heatmap — WebSocket data required
        </div>
      </div>

      {/* DOM Chart placeholder */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          DEPTH OF MARKET (DOM Chart)
        </h3>
        <div className="text-[11px] text-text-tertiary font-mono text-center py-12">
          DOM Chart — Canvas rendering with real-time L2 data
        </div>
      </div>

      {/* Whale Tracker */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          WHALE TRACKER
        </h3>
        <div className="text-[11px] text-text-tertiary font-mono">
          12 Alpha Whales online · CM (avg): 1.18 ▲ bullish
        </div>
      </div>

      {/* Wallet Clustering */}
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

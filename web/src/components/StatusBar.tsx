import type { SystemStatus } from '../types';
import { useWallet } from '../hooks/useWallet';

interface StatusBarProps {
  status: SystemStatus;
}

export function StatusBar({ status }: StatusBarProps) {
  const { wallet } = useWallet(5000);
  const wsHealth = status.websocket_connected ? 'CLEAN' : 'DOWN';
  const wsColor = status.websocket_connected ? 'text-profit' : 'text-loss animate-pulse-red';
  const epochLabel = `MAB ${status.portfolio_epoch}/6h`;
  const whaleLabel = `${status.alpha_whales}α`;
  const pt = status.paper_trading;

  // Show WS latency from heartbeats (it's the CLOB book latency, not a rate limit)
  const wsLatencyMs = status.heartbeats?.clob_ws?.latency_ms;

  return (
    <footer className="flex-shrink-0 bg-bg-secondary border-t border-bg-hover">
      <div className="flex items-center gap-6 px-4 py-1 text-[10.5px] font-mono text-text-secondary">
        {/* WebSocket connection */}
        <span>
          📡 WS: <span className={wsColor}>{wsHealth}</span>
        </span>

        {/* Book latency — BUG FIX: was labelled "RL" (rate-limit), it's actually WS book latency */}
        <span>
          📶 Lat:{' '}
          {wsLatencyMs != null ? (
            <span className={wsLatencyMs > 50 ? 'text-warning' : 'text-text-primary'}>
              {wsLatencyMs}ms
            </span>
          ) : (
            <span className="text-text-tertiary">N/A</span>
          )}
        </span>

        {/* MAB epoch */}
        <span>
          🧠 <span className="text-text-primary">{epochLabel}</span>
        </span>

        {/* Alpha whales */}
        <span>
          🐋 <span className="text-whale">{whaleLabel}</span>
        </span>

        {/* USDC balance */}
        <span>
          💰{' '}
          <span className="text-profit">
            ${wallet.usdc_total.toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </span>
        </span>

        {/* POL gas balance */}
        <span>
          ⛽{' '}
          <span className={wallet.pol_balance < 2 ? 'text-loss animate-pulse-red' : 'text-warning'}>
            {wallet.pol_balance.toFixed(1)} POL
          </span>
        </span>

        {/* Paper trading summary (only when available) */}
        {pt && (
          <span className="text-text-tertiary">
            Open: {pt.open_positions} · P&L:{' '}
            <span className={pt.unrealized_pnl >= 0 ? 'text-profit' : 'text-loss'}>
              {pt.unrealized_pnl >= 0 ? '+' : ''}${pt.unrealized_pnl.toFixed(0)}
            </span>
          </span>
        )}

        {/* Market tracking counts */}
        <span className="ml-auto text-text-tertiary">
          {status.tracked_markets_book} books · {status.tracked_markets_trades} trades
        </span>
      </div>
    </footer>
  );
}

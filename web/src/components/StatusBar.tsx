import type { SystemStatus } from '../types';
import { useWallet } from '../hooks/useWallet';

interface StatusBarProps {
  status: SystemStatus;
}

export function StatusBar({ status }: StatusBarProps) {
  const { wallet } = useWallet(5000);
  const wsHealth = status.websocket_connected ? 'CLEAN' : 'DOWN';
  const wsColor = status.websocket_connected ? 'text-profit' : 'text-loss';
  const epochLabel = `MAB ${status.portfolio_epoch}/6h`;
  const whaleLabel = `${status.alpha_whales}α`;
  const pt = status.paper_trading;

  return (
    <footer className="flex-shrink-0 bg-bg-secondary border-t border-bg-hover">
      <div className="flex items-center gap-6 px-4 py-1 text-[10.5px] font-mono text-text-secondary">
        <span>📡 WS: <span className={wsColor}>{wsHealth}</span></span>
        <span>⚡ RL: recon {status.heartbeats?.clob_ws?.latency_ms != null ? `${status.heartbeats.clob_ws.latency_ms}ms` : 'N/A'}</span>
        <span>🧠 <span className="text-text-primary">{epochLabel}</span></span>
        <span>🐋 <span className="text-whale">{whaleLabel}</span></span>
        <span>💰 <span className="text-profit">${wallet.usdc_total.toLocaleString(undefined, {maximumFractionDigits: 0})}</span></span>
        <span>⛽ <span className={wallet.pol_balance < 2 ? 'text-loss' : 'text-warning'}>{wallet.pol_balance.toFixed(1)} POL</span></span>
        {pt && (
          <span className="text-text-tertiary">
            Open: {pt.open_positions} · P&L: {pt.unrealized_pnl >= 0 ? '+' : ''}${pt.unrealized_pnl.toFixed(0)}
          </span>
        )}
        <span className="ml-auto text-text-tertiary">
          {status.tracked_markets_book} books · {status.tracked_markets_trades} trades
        </span>
      </div>
    </footer>
  );
}

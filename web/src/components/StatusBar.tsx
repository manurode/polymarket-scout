import type { SystemStatus } from '../types';

interface StatusBarProps {
  status: SystemStatus;
}

export function StatusBar({ status }: StatusBarProps) {
  const wsHealth = status.websocket_connected ? 'CLEAN' : 'DOWN';
  const wsColor = status.websocket_connected ? 'text-profit' : 'text-loss';
  const epochLabel = `MAB ${status.portfolio_epoch}/6h`;
  const whaleLabel = `${status.alpha_whales}α`;

  return (
    <footer className="flex-shrink-0 bg-bg-secondary border-t border-bg-hover">
      <div className="flex items-center gap-6 px-4 py-1 text-[10.5px] font-mono text-text-secondary">
        <span>📡 WS: <span className={wsColor}>{wsHealth}</span></span>
        <span>⚡ RL: recon 70%</span>
        <span>🧠 <span className="text-text-primary">{epochLabel}</span></span>
        <span>🐋 <span className="text-whale">{whaleLabel}</span></span>
        <span>💰 <span className="text-profit">$1,247</span></span>
        <span>⛽ <span className="text-warning">4.2 POL</span></span>
        <span className="ml-auto text-text-tertiary">
          {status.tracked_markets_book} books · {status.tracked_markets_trades} trades
        </span>
      </div>
    </footer>
  );
}

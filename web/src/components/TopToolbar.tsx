import type { SystemMode, NavTab } from '../types';
import { useHealth } from '../hooks/useHealth';

interface TopToolbarProps {
  mode: SystemMode;
  onModeChange: (mode: SystemMode) => void;
  activeTab: string;
  onTabChange: (tab: string) => void;
  tabs: NavTab[];
}

export function TopToolbar({ mode, activeTab, onTabChange, tabs }: TopToolbarProps) {
  const { uptime } = useHealth(5000);

  return (
    <header className="flex-shrink-0 bg-bg-secondary border-b border-bg-hover">
      {/* Top row: brand, mode, uptime, kill switch */}
      <div className="flex items-center justify-between px-4 py-1.5">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-bold tracking-wider text-text-primary">
            SCOUT LAB v2.0
          </span>
          {/* Mode indicator - now more explicit about PAPER TRADING */}
          <span className={`
            text-[11px] font-mono px-2 py-0.5 rounded-full
            ${mode === 'LIVE PAPER'
              ? 'bg-warning/20 text-warning border border-warning/30'
              : mode === 'BACKTEST'
                ? 'bg-info/20 text-info border border-info/30'
                : 'bg-bg-tertiary text-text-tertiary border border-bg-hover'
            }
          `}>
            {mode === 'LIVE PAPER' ? '📦 PAPER TRADING' : mode}
          </span>
          {/* Tooltip hint */}
          <span className="text-[9px] text-text-tertiary hidden sm:inline">
            (Virtual money - no real funds at risk)
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] font-mono text-text-secondary">
          <span>UPTIME: {uptime}</span>
          <button
            className="px-2 py-0.5 rounded border border-loss/30 text-loss hover:bg-loss/10 transition-colors"
            title="Kill Switch — Pause all trading"
          >
            KILL ▼
          </button>
        </div>
      </div>

      {/* Bottom row: navigation tabs */}
      <nav className="flex px-4">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`
              relative px-4 py-1.5 text-[11px] font-medium tracking-wider
              transition-colors border-b-2
              ${activeTab === tab.id
                ? 'text-text-primary border-info'
                : 'text-text-tertiary border-transparent hover:text-text-secondary'
              }
            `}
          >
            {tab.label}
            {tab.alertCount > 0 && (
              <span className="ml-1.5 w-2 h-2 rounded-full bg-loss inline-block animate-pulse-red" />
            )}
          </button>
        ))}
      </nav>
    </header>
  );
}

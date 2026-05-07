import type { SystemMode, NavTab } from '../types';

interface TopToolbarProps {
  mode: SystemMode;
  onModeChange: (mode: SystemMode) => void;
  activeTab: string;
  onTabChange: (tab: string) => void;
  tabs: NavTab[];
}

export function TopToolbar({ mode, activeTab, onTabChange, tabs }: TopToolbarProps) {
  const modeColors: Record<SystemMode, string> = {
    'LIVE PAPER': 'text-profit',
    'BACKTEST': 'text-info',
    'DRY RUN': 'text-text-tertiary',
  };

  return (
    <header className="flex-shrink-0 bg-bg-secondary border-b border-bg-hover">
      {/* Top row: brand, mode, uptime, kill switch */}
      <div className="flex items-center justify-between px-4 py-1.5">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-bold tracking-wider text-text-primary">
            SCOUT LAB v2.0
          </span>
          <span className={`text-[11px] font-mono ${modeColors[mode]}`}>
            ● {mode}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] font-mono text-text-secondary">
          <span>UPTIME: 38h 12m</span>
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

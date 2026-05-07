import type { Alert } from '../types';

interface AlertStripProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
}

const severityStyles: Record<string, { bg: string; text: string; icon: string }> = {
  critical: { bg: 'bg-loss/15 border-loss/50', text: 'text-loss-bright', icon: '🔴' },
  error: { bg: 'bg-orange-500/10 border-orange-500/50', text: 'text-orange-400', icon: '🟠' },
  warning: { bg: 'bg-warning/10 border-warning/50', text: 'text-warning', icon: '🟡' },
  info: { bg: 'bg-info/10 border-info/50', text: 'text-info', icon: '🔵' },
};

export function AlertStrip({ alerts, onDismiss }: AlertStripProps) {
  if (alerts.length === 0) return null;

  return (
    <div className="flex-shrink-0 bg-bg-tertiary border-b border-bg-hover">
      <div className="flex flex-wrap gap-1.5 px-4 py-1.5">
        {alerts.map(alert => {
          const style = severityStyles[alert.severity] || severityStyles.info;
          const timeAgo = Date.now() - alert.timestamp;
          const timeStr = timeAgo < 60000
            ? `${Math.round(timeAgo / 1000)}s ago`
            : `${Math.round(timeAgo / 60000)}m ago`;

          return (
            <div
              key={alert.id}
              className={`
                flex items-center gap-2 px-3 py-1 rounded border text-[11px]
                ${style.bg} ${style.text}
              `}
            >
              <span className={`text-[10px] ${alert.severity === 'critical' ? 'animate-pulse-red' : ''}`}>
                {style.icon}
              </span>
              <span className="max-w-xl truncate">
                [{alert.severity.toUpperCase()}] {alert.message}
              </span>
              <span className="text-text-tertiary whitespace-nowrap ml-1">
                ⏱ {timeStr}
              </span>
              <button
                onClick={() => onDismiss(alert.id)}
                className="text-text-tertiary hover:text-text-primary ml-1"
              >
                ✕
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

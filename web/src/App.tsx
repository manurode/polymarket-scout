import { useState, useEffect, useCallback } from 'react';
import { TopToolbar } from './components/TopToolbar';
import { AlertStrip } from './components/AlertStrip';
import { StatusBar } from './components/StatusBar';
import { SystemHealth } from './components/SystemHealth';
import { PortfolioArena } from './components/PortfolioArena';
import { OracleRadar } from './components/OracleRadar';
import { RiskMonitor } from './components/RiskMonitor';
import { ToastProvider, useToasts } from './components/ToastProvider';
import { useSystemStatus } from './hooks/useSystemStatus';
import type { Alert, NavTab, SystemMode } from './types';

const NAV_TABS: NavTab[] = [
  { id: 'system', label: 'SYSTEM', alertCount: 2 },
  { id: 'portfolio', label: 'PORTFOLIO', alertCount: 0 },
  { id: 'oracles', label: 'ORACLES', alertCount: 1 },
  { id: 'risk', label: 'RISK', alertCount: 1 },
];

function AppContent() {
  const [activeTab, setActiveTab] = useState<string>('system');
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [mode, setMode] = useState<SystemMode>('LIVE PAPER');

  const systemStatus = useSystemStatus(1000);
  const { addToast } = useToasts();

  // ── Simulate alerts + demo toasts on startup ──────────────────
  useEffect(() => {
    const demoAlerts: Alert[] = [
      {
        id: '1',
        severity: 'critical',
        message: 'RECONCILING: "Trump wins 2028?" seq gap=3',
        timestamp: Date.now() - 2000,
      },
      {
        id: '2',
        severity: 'warning',
        message: 'FROZEN: momentum_follow (Sortino -1.2)',
        timestamp: Date.now() - 300000,
      },
    ];
    setAlerts(demoAlerts);

    // Demo toasts to showcase the notification system
    const timer = setTimeout(() => {
      addToast({
        severity: 'critical',
        title: 'RECONCILING',
        message: '"Trump wins 2028?" — seq gap detected. Trading paused.',
        duration: 15000,
        sound: 'siren',
      });
    }, 1000);

    const timer2 = setTimeout(() => {
      addToast({
        severity: 'warning',
        title: 'TIME DECAY',
        message: 'Oil price > $80? — τ = 91%. Liquidation zone.',
        duration: 10000,
        sound: 'alarm',
      });
    }, 3000);

    const timer3 = setTimeout(() => {
      addToast({
        severity: 'info',
        title: 'FILL',
        message: 'Market Making: +$2.15 spread captured on BTC > $100K',
        duration: 5000,
        sound: 'click',
      });
    }, 5000);

    return () => {
      clearTimeout(timer);
      clearTimeout(timer2);
      clearTimeout(timer3);
    };
  }, []);

  const dismissAlert = useCallback((id: string) => {
    setAlerts(prev => prev.filter(a => a.id !== id));
  }, []);

  const renderPanel = () => {
    switch (activeTab) {
      case 'system': return <SystemHealth />;
      case 'portfolio': return <PortfolioArena />;
      case 'oracles': return <OracleRadar />;
      case 'risk': return <RiskMonitor />;
      default: return <SystemHealth />;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-bg-primary text-text-primary overflow-hidden">
      <TopToolbar
        mode={mode}
        onModeChange={setMode}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        tabs={NAV_TABS}
      />

      <AlertStrip alerts={alerts} onDismiss={dismissAlert} />

      <main className="flex-1 overflow-y-auto overflow-x-hidden">
        {renderPanel()}
      </main>

      <StatusBar status={systemStatus} />
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <AppContent />
    </ToastProvider>
  );
}

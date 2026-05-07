import { useState, useEffect, useCallback } from 'react';
import { TopToolbar } from './components/TopToolbar';
import { AlertStrip } from './components/AlertStrip';
import { StatusBar } from './components/StatusBar';
import { SystemHealth } from './components/SystemHealth';
import { PortfolioArena } from './components/PortfolioArena';
import { OracleRadar } from './components/OracleRadar';
import { RiskMonitor } from './components/RiskMonitor';
import { useSystemStatus } from './hooks/useSystemStatus';
import type { Alert, NavTab, SystemMode } from './types';

const NAV_TABS: NavTab[] = [
  { id: 'system', label: 'SYSTEM', alertCount: 0 },
  { id: 'portfolio', label: 'PORTFOLIO', alertCount: 0 },
  { id: 'oracles', label: 'ORACLES', alertCount: 0 },
  { id: 'risk', label: 'RISK', alertCount: 0 },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<string>('system');
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [mode, setMode] = useState<SystemMode>('LIVE PAPER');

  const systemStatus = useSystemStatus(1000);

  // ── Simulate alerts for development ──────────────────────────────
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
  }, []);

  const dismissAlert = useCallback((id: string) => {
    setAlerts(prev => prev.filter(a => a.id !== id));
  }, []);

  // ── Render active panel ──────────────────────────────────────────
  const renderPanel = () => {
    switch (activeTab) {
      case 'system': return <SystemHealth status={systemStatus} />;
      case 'portfolio': return <PortfolioArena />;
      case 'oracles': return <OracleRadar />;
      case 'risk': return <RiskMonitor />;
      default: return <SystemHealth status={systemStatus} />;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-bg-primary text-text-primary overflow-hidden">
      {/* ── Top Toolbar (sticky) ────────────────────────────────── */}
      <TopToolbar
        mode={mode}
        onModeChange={setMode}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        tabs={NAV_TABS}
      />

      {/* ── Alert Strip (sticky) ─────────────────────────────────── */}
      <AlertStrip alerts={alerts} onDismiss={dismissAlert} />

      {/* ── Main Content (scrollable) ────────────────────────────── */}
      <main className="flex-1 overflow-y-auto overflow-x-hidden">
        {renderPanel()}
      </main>

      {/* ── Status Bar (sticky bottom) ───────────────────────────── */}
      <StatusBar status={systemStatus} />
    </div>
  );
}

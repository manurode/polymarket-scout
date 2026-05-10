import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { TopToolbar } from './components/TopToolbar';
import { AlertStrip } from './components/AlertStrip';
import { StatusBar } from './components/StatusBar';
import { SystemHealth } from './components/SystemHealth';
import { PortfolioArena } from './components/PortfolioArena';
import { OracleRadar } from './components/OracleRadar';
import { RiskMonitor } from './components/RiskMonitor';
import { ToastProvider, useToasts } from './components/ToastProvider';
import { useSystemStatus } from './hooks/useSystemStatus';
import { useReconciliation } from './hooks/useReconciliation';
import { usePositions } from './hooks/usePositions';
import type { Alert, NavTab, SystemMode } from './types';

const BASE_TABS: Omit<NavTab, 'alertCount'>[] = [
  { id: 'system', label: 'SYSTEM' },
  { id: 'portfolio', label: 'PORTFOLIO' },
  { id: 'oracles', label: 'ORACLES' },
  { id: 'risk', label: 'RISK' },
];

function AppContent() {
  const [activeTab, setActiveTab] = useState<string>('system');
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [mode, setMode] = useState<SystemMode>('LIVE PAPER');

  const systemStatus = useSystemStatus(1000);
  const { data: recon } = useReconciliation(5000);
  const { positions, liqCount } = usePositions(5000);
  const { addToast } = useToasts();

  // ── Track previous values for rising-edge toast logic ─────────────
  const prevReconRef = useRef(0);
  const prevLiqRef = useRef(0);
  const prevWsRef = useRef(true);

  // ── Drive alert strip from real data ────────────────────────────
  useEffect(() => {
    const newAlerts: Alert[] = [];

    if (recon.reconciling > 0) {
      newAlerts.push({
        id: 'recon',
        severity: 'critical',
        message: `RECONCILING: ${recon.reconciling} market${recon.reconciling > 1 ? 's' : ''} with sequence gaps`,
        timestamp: Date.now(),
      });
    }

    if (liqCount > 0) {
      const label = positions
        .filter(p => p.tau_pct >= 95)
        .slice(0, 2)
        .map(p => `"${p.market}"`)
        .join(', ');
      newAlerts.push({
        id: 'liq',
        severity: 'warning',
        message: `TIME DECAY: ${liqCount} pos ≥95% tau — forced close imminent${label ? ` (${label})` : ''}`,
        timestamp: Date.now(),
      });
    }

    if (!systemStatus.websocket_connected) {
      newAlerts.push({
        id: 'ws_down',
        severity: 'critical',
        message: 'WebSocket DISCONNECTED — order book data stale, trading halted',
        timestamp: Date.now(),
      });
    }

    setAlerts(newAlerts);
  }, [recon.reconciling, liqCount, systemStatus.websocket_connected, positions]);

  // ── Rising-edge toasts (only fire when a new condition starts) ────
  useEffect(() => {
    const prev = prevReconRef.current;
    if (recon.reconciling > 0 && prev === 0) {
      addToast({
        severity: 'critical',
        title: 'RECONCILING',
        message: `${recon.reconciling} market${recon.reconciling > 1 ? 's' : ''} — seq gap detected. Trading paused.`,
        duration: 12000,
        sound: 'siren',
      });
    }
    prevReconRef.current = recon.reconciling;
  }, [recon.reconciling, addToast]);

  useEffect(() => {
    const prev = prevLiqRef.current;
    if (liqCount > 0 && prev === 0) {
      const urgentPositions = positions.filter(p => p.tau_pct >= 95);
      addToast({
        severity: 'warning',
        title: 'TIME DECAY',
        message: urgentPositions
          .slice(0, 2)
          .map(p => `${p.market} — τ=${p.tau_pct}%`)
          .join(' | '),
        duration: 10000,
        sound: 'alarm',
      });
    }
    prevLiqRef.current = liqCount;
  }, [liqCount, positions, addToast]);

  useEffect(() => {
    const prev = prevWsRef.current;
    if (!systemStatus.websocket_connected && prev) {
      addToast({
        severity: 'critical',
        title: 'WS DISCONNECTED',
        message: 'CLOB WebSocket down — book data stale. System in degraded mode.',
        duration: 0, // requires manual dismiss
        sound: 'siren',
      });
    }
    prevWsRef.current = systemStatus.websocket_connected;
  }, [systemStatus.websocket_connected, addToast]);

  // ── Compute dynamic tab alert counts ─────────────────────────────
  const tabs: NavTab[] = useMemo(() => {
    const systemAlerts =
      (recon.reconciling > 0 ? 1 : 0) + (!systemStatus.websocket_connected ? 1 : 0);

    return BASE_TABS.map(tab => ({
      ...tab,
      alertCount:
        tab.id === 'system'
          ? systemAlerts
          : tab.id === 'risk'
          ? liqCount
          : 0,
    }));
  }, [recon.reconciling, systemStatus.websocket_connected, liqCount]);

  const dismissAlert = useCallback((id: string) => {
    setAlerts(prev => prev.filter(a => a.id !== id));
  }, []);

  const renderPanel = () => {
    switch (activeTab) {
      case 'system':    return <SystemHealth />;
      case 'portfolio': return <PortfolioArena />;
      case 'oracles':   return <OracleRadar />;
      case 'risk':      return <RiskMonitor />;
      default:          return <SystemHealth />;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-bg-primary text-text-primary overflow-hidden">
      <TopToolbar
        mode={mode}
        onModeChange={setMode}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        tabs={tabs}
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

import { useState, useEffect, useRef } from 'react';
import type { Position } from '../types';

const MOCK_POSITIONS: Position[] = [
  { id: 1, market: 'Trump wins 2028?', strategy: 'MOM', side: 'YES', size: 86, entry: 0.62, mark: 0.67, pnl: 7.12, pnl_pct: 8.3, tau_pct: 62, toxicity: 0.25, liquidation_zone: false },
  { id: 2, market: 'BTC > $100K Dec?', strategy: 'CORR', side: 'NO', size: 120, entry: 0.45, mark: 0.42, pnl: -4.80, pnl_pct: -4.0, tau_pct: 85, toxicity: 0.65, liquidation_zone: true },
  { id: 3, market: 'Fed cuts rates?', strategy: 'WHL', side: 'YES', size: 45, entry: 0.55, mark: 0.58, pnl: 2.45, pnl_pct: 5.4, tau_pct: 40, toxicity: 0.18, liquidation_zone: false },
  { id: 4, market: 'Crypto bull market?', strategy: 'MM', side: 'NO', size: 62, entry: 0.70, mark: 0.68, pnl: -1.24, pnl_pct: -2.0, tau_pct: 25, toxicity: 0.12, liquidation_zone: false },
  { id: 5, market: 'S&P 500 ATH Q3?', strategy: 'MOM', side: 'YES', size: 38, entry: 0.35, mark: 0.39, pnl: 4.56, pnl_pct: 12.0, tau_pct: 15, toxicity: 0.05, liquidation_zone: false },
  { id: 6, market: 'Oil price > $80?', strategy: 'CNTR', side: 'NO', size: 28, entry: 0.80, mark: 0.85, pnl: -7.50, pnl_pct: -26.8, tau_pct: 91, toxicity: 1.20, liquidation_zone: true },
];

interface PositionsData {
  positions: Position[];
  loading: boolean;
  source: 'paper_trading' | 'mock' | 'error';
  totalPnl: number;
  totalValue: number;
  liqCount: number;
}

export function usePositions(pollIntervalMs: number = 5000): PositionsData {
  const [positions, setPositions] = useState<Position[]>(MOCK_POSITIONS);
  const [loading, setLoading] = useState(false);
  const [source, setSource] = useState<'paper_trading' | 'mock' | 'error'>('mock');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchPositions = async () => {
      try {
        const res = await fetch('/api/risk/positions');
        if (res.ok && mountedRef.current) {
          const data: Position[] = await res.json();
          if (Array.isArray(data)) {
            // BUG FIX: Don't fall back to mock when the real engine returns [] (no open positions).
            // An empty array IS a valid real state. Only use mock if the endpoint itself is missing.
            setPositions(data);
            setSource('paper_trading');
          }
          setLoading(false);
        }
      } catch {
        // Keep mock data if endpoint not available
        setSource('mock');
      }
    };

    fetchPositions();
    timer = setInterval(fetchPositions, pollIntervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  const totalPnl = positions.reduce((s, p) => s + p.pnl, 0);
  const totalValue = positions.reduce((s, p) => s + p.size, 0);
  // BUG FIX: Liquidation zone = tau >= 95% (matches auto-close threshold in paper_trading.py).
  // tau > 85% is the "warning" zone; tau >= 95% is the forced close threshold.
  const liqCount = positions.filter(p => p.tau_pct >= 95).length;

  return { positions, loading, source, totalPnl, totalValue, liqCount };
}

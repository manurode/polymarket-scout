import { useState, useEffect, useRef } from 'react';

export interface EquityPoint {
  timestamp: number;   // Unix seconds
  equity: number;      // Total account value in USD
}

interface EquityHistoryData {
  points: EquityPoint[];
  loading: boolean;
  /** Current equity (last point) */
  currentEquity: number;
  /** First point equity (for P&L calculation) */
  startEquity: number;
  /** Absolute P&L since first snapshot */
  totalPnl: number;
  /** P&L percentage */
  totalPnlPct: number;
  /** Max drawdown amount */
  maxDrawdown: number;
}

export function useEquityHistory(pollIntervalMs: number = 30000): EquityHistoryData {
  const [points, setPoints] = useState<EquityPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;

    const fetchPerformance = async () => {
      try {
        const res = await fetch('/api/portfolio/performance');
        if (res.ok && mountedRef.current) {
          const data = await res.json();
          if (Array.isArray(data.history)) {
            setPoints(data.history as EquityPoint[]);
          }
          setLoading(false);
        }
      } catch {
        setLoading(false);
      }
    };

    fetchPerformance();
    const timer = setInterval(fetchPerformance, pollIntervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  const currentEquity = points.length > 0 ? points[points.length - 1].equity : 0;
  const startEquity = points.length > 0 ? points[0].equity : 0;
  const totalPnl = currentEquity - startEquity;
  const totalPnlPct = startEquity > 0 ? (totalPnl / startEquity) * 100 : 0;

  // Max drawdown: biggest peak-to-trough drop
  let peak = startEquity;
  let maxDrawdown = 0;
  for (const p of points) {
    if (p.equity > peak) peak = p.equity;
    const dd = peak - p.equity;
    if (dd > maxDrawdown) maxDrawdown = dd;
  }

  return { points, loading, currentEquity, startEquity, totalPnl, totalPnlPct, maxDrawdown };
}

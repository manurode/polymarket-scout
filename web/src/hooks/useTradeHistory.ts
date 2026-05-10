import { useState, useEffect, useRef } from 'react';

export interface ClosedTrade {
  id: number;
  token_id: string;
  strategy: string;
  market: string;
  side: 'YES' | 'NO';
  size: number;
  entry: number;
  exit: number;
  slippage_pct: number;
  slippage_usd: number;
  commission_usd: number;
  pnl: number;
  pnl_pct: number;
  reason: string;  // "tp" | "sl" | "tau" | "expired" | "manual"
  opened_at: number;
  closed_at: number;
}

interface TradeHistoryData {
  trades: ClosedTrade[];
  loading: boolean;
  totalPnl: number;
  winRate: number;
}

export function useTradeHistory(pollIntervalMs: number = 15000): TradeHistoryData {
  const [trades, setTrades] = useState<ClosedTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;

    const fetchHistory = async () => {
      try {
        const res = await fetch('/api/portfolio/history?limit=100');
        if (res.ok && mountedRef.current) {
          const data = await res.json();
          if (Array.isArray(data.trades)) {
            setTrades(data.trades);
          }
          setLoading(false);
        }
      } catch {
        // Silently fall back to empty state; backend mock handles dev mode
        setLoading(false);
      }
    };

    fetchHistory();
    const timer = setInterval(fetchHistory, pollIntervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0);
  const wins = trades.filter(t => t.pnl > 0).length;
  const winRate = trades.length > 0 ? wins / trades.length : 0;

  return { trades, loading, totalPnl, winRate };
}

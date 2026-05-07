import { useState, useEffect, useRef } from 'react';
import type { StrategyRanking, Allocation } from '../types';

const EMPTY_ALLOCATION: Allocation = {
  active: 0, frozen: 0, retired: 0, total_equity: 0,
  pnl_24h: 0, pnl_24h_pct: 0, max_drawdown: 0,
};

export function usePortfolio(pollIntervalMs: number = 10000) {
  const [strategies, setStrategies] = useState<StrategyRanking[]>([]);
  const [allocation, setAllocation] = useState<Allocation>(EMPTY_ALLOCATION);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchData = async () => {
      try {
        const [stratRes, allocRes] = await Promise.all([
          fetch('/api/portfolio/strategies'),
          fetch('/api/portfolio/allocation'),
        ]);

        if (stratRes.ok && mountedRef.current) {
          const data: StrategyRanking[] = await stratRes.json();
          setStrategies(data);
        }
        if (allocRes.ok && mountedRef.current) {
          const data: Allocation = await allocRes.json();
          setAllocation(data);
        }
        if (mountedRef.current) {
          setLoading(false);
          setError(null);
        }
      } catch (err) {
        if (mountedRef.current) {
          setLoading(false);
          setError(err instanceof Error ? err.message : 'Failed to fetch portfolio data');
        }
      }
    };

    fetchData();
    timer = setInterval(fetchData, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { strategies, allocation, loading, error };
}

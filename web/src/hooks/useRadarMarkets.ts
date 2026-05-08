import { useState, useEffect, useRef } from 'react';

export interface RadarMarket {
  condition_id: string;
  question: string;
  volume_24h: number;
  liquidity: number;
  spread: number | null;
  mid_price: number | null;
  timestamp: number;
}

export interface RadarData {
  markets: RadarMarket[];
  count: number;
  source: string;
  error?: string;
}

/**
 * Hook that fetches real market data from Gamma API via radar scan.
 */
export function useRadarMarkets(pollIntervalMs: number = 30000): RadarData {
  const [data, setData] = useState<RadarData>({
    markets: [],
    count: 0,
    source: 'initializing',
  });
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchMarkets = async () => {
      try {
        const res = await fetch('/api/markets/radar');
        if (res.ok && mountedRef.current) {
          const data = await res.json();
          setData(data);
        }
      } catch (err) {
        if (mountedRef.current) {
          setData(prev => ({
            ...prev,
            source: 'error',
            error: err instanceof Error ? err.message : 'Unknown error',
          }));
        }
      }
    };

    fetchMarkets();
    timer = setInterval(fetchMarkets, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return data;
}

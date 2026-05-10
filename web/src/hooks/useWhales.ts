import { useState, useEffect, useRef } from 'react';
import type { AlphaWhale, WhaleFlow } from '../types';

const EMPTY_FLOW: WhaleFlow = { markets: [], avg_conviction_multiplier: 1.0 };

export function useWhales(pollIntervalMs: number = 15000) {
  const [alphaWhales, setAlphaWhales] = useState<AlphaWhale[]>([]);
  const [whaleFlow, setWhaleFlow] = useState<WhaleFlow>(EMPTY_FLOW);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchData = async () => {
      try {
        const res = await fetch('/api/whales');
        if (res.ok && mountedRef.current) {
          const data = await res.json();
          setAlphaWhales(data.alpha_whales || []);

          // The server returns whale_flow as a raw list:
          //   [{condition_id, net_flow_1h, whale_consensus, ...}, ...]
          // Normalise it into the WhaleFlow shape the UI expects.
          const rawFlow: Array<Record<string, unknown>> = Array.isArray(data.whale_flow)
            ? data.whale_flow
            : (data.whale_flow?.markets ?? []);

          const markets = rawFlow.map(f => ({
            market: String(f.condition_id ?? f.market ?? ''),
            flow_usd: Number(f.net_flow_1h ?? f.flow_usd ?? 0),
            direction: (Number(f.net_flow_1h ?? f.flow_usd ?? 0) >= 0
              ? 'buy'
              : 'sell') as 'buy' | 'sell',
          }));

          // avg_conviction_multiplier: prefer explicit field, else derive from whale_consensus
          const avgCm =
            typeof data.whale_flow?.avg_conviction_multiplier === 'number'
              ? data.whale_flow.avg_conviction_multiplier
              : rawFlow.length > 0
              ? rawFlow.reduce((sum, f) => sum + Number(f.whale_consensus ?? 1.0), 0) /
                rawFlow.length
              : 1.0;

          setWhaleFlow({ markets, avg_conviction_multiplier: avgCm });
          setLoading(false);
          setError(null);
        }
      } catch (err) {
        if (mountedRef.current) {
          setLoading(false);
          setError(err instanceof Error ? err.message : 'Failed to fetch whale data');
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

  return { alphaWhales, whaleFlow, loading, error };
}

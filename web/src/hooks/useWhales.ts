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
          setWhaleFlow(data.whale_flow || EMPTY_FLOW);
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

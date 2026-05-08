import { useState, useEffect, useRef } from 'react';

interface SpoofingEntry {
  token_id: string;
  spoof_score: number;
  classification: string;
  requires_pause: boolean;
  recommended_action?: Record<string, unknown>;
}

interface SpoofingData {
  markets: SpoofingEntry[];
}

const DEFAULT_DATA: SpoofingData = {
  markets: [
    { token_id: 'mock-trump', spoof_score: 0.62, classification: 'PROBABLE', requires_pause: false },
    { token_id: 'mock-btc', spoof_score: 0.35, classification: 'SUSPICIOUS', requires_pause: false },
    { token_id: 'mock-fed', spoof_score: 0.12, classification: 'NORMAL', requires_pause: false },
  ],
};

export function useSpoofing(pollIntervalMs: number = 10000) {
  const [data, setData] = useState<SpoofingData>(DEFAULT_DATA);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchData = async () => {
      try {
        const res = await fetch('/api/oracles/spoofing');
        if (res.ok && mountedRef.current) {
          const d: SpoofingData = await res.json();
          setData(d);
          setLoading(false);
        }
      } catch {
        // Keep default
      }
    };

    fetchData();
    timer = setInterval(fetchData, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { data, loading };
}

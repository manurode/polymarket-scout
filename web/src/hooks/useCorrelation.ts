import { useState, useEffect, useRef } from 'react';

export interface CorrelationPos {
  market: string;
  side: string;
}

export interface CorrelationData {
  positions: CorrelationPos[];
  labels: string[];
  matrix: number[][];
  avg_correlation: number;
  source: string;
}

export function useCorrelation(pollIntervalMs: number = 10000): {
  data: CorrelationData;
  source: string;
} {
  const [data, setData] = useState<CorrelationData>({
    positions: [],
    labels: [],
    matrix: [],
    avg_correlation: 0,
    source: 'no_data',
  });
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchData = async () => {
      try {
        const res = await fetch('/api/risk/correlation');
        if (res.ok && mountedRef.current) {
          const d: CorrelationData = await res.json();
          setData(d);
        }
      } catch {
        // Keep defaults
      }
    };

    fetchData();
    timer = setInterval(fetchData, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { data, source: data.source };
}

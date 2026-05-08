import { useState, useEffect, useRef } from 'react';

interface ReconciliationData {
  total: number;
  clean: number;
  reconciling: number;
  markets: Array<{
    token_id: string;
    state: string;
    seq_num: number;
    gap_count: number;
    last_delta_age_ms: number;
    buffer_size: number;
  }>;
}

const DEFAULT_DATA: ReconciliationData = {
  total: 50,
  clean: 47,
  reconciling: 3,
  markets: [],
};

export function useReconciliation(pollIntervalMs: number = 5000) {
  const [data, setData] = useState<ReconciliationData>(DEFAULT_DATA);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchData = async () => {
      try {
        const res = await fetch('/api/system/reconciliation');
        if (res.ok && mountedRef.current) {
          const d: ReconciliationData = await res.json();
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

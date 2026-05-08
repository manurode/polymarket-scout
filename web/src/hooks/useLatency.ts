import { useState, useEffect, useRef } from 'react';

export interface LatencyStage {
  id: string;
  label: string;
  actual_ms: number;
  budget_ms: number;
  source: string;
}

interface LatencyData {
  stages: LatencyStage[];
  total_actual_ms: number;
  total_budget_ms: number;
}

const DEFAULT_STAGES: LatencyStage[] = [
  { id: 'ws_to_book', label: 'WS→Book', actual_ms: 0, budget_ms: 5, source: 'no_data' },
  { id: 'obi_spoof', label: 'OBI+TFI→Spoof', actual_ms: 0, budget_ms: 5, source: 'no_data' },
  { id: 'signal_decision', label: 'Signal→Decision', actual_ms: 0, budget_ms: 10, source: 'no_data' },
  { id: 'kelly_position', label: 'Kelly→Position', actual_ms: 0, budget_ms: 5, source: 'no_data' },
  { id: 'risk_trade', label: 'Risk→Trade', actual_ms: 0, budget_ms: 3, source: 'no_data' },
  { id: 'radar_scan', label: 'Radar Scan', actual_ms: 0, budget_ms: 1000, source: 'no_data' },
];

export function useLatency(pollIntervalMs: number = 5000): {
  stages: LatencyStage[];
  totalActual: number;
  totalBudget: number;
  source: string;
} {
  const [stages, setStages] = useState<LatencyStage[]>(DEFAULT_STAGES);
  const [totalActual, setTotalActual] = useState(0);
  const [totalBudget, setTotalBudget] = useState(0);
  const [source, setSource] = useState('no_data');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchLatency = async () => {
      try {
        const res = await fetch('/api/system/latency');
        if (res.ok && mountedRef.current) {
          const data: LatencyData = await res.json();
          setStages(data.stages);
          setTotalActual(data.total_actual_ms);
          setTotalBudget(data.total_budget_ms);
          // Determine most meaningful source
          const sources = data.stages.map(s => s.source);
          const hasReal = sources.some(s => s !== 'no_data' && s !== 'default');
          const hasDefault = sources.some(s => s === 'default');
          setSource(hasReal ? 'live' : hasDefault ? 'estimated' : 'no_data');
        }
      } catch {
        // Keep defaults
      }
    };

    fetchLatency();
    timer = setInterval(fetchLatency, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { stages, totalActual, totalBudget, source };
}

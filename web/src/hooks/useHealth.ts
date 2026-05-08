import { useState, useEffect, useRef } from 'react';

interface HealthData {
  status: string;
  timestamp: string;
  uptime_seconds: number;
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function useHealth(pollIntervalMs: number = 5000) {
  const [uptime, setUptime] = useState<string>('0s');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchHealth = async () => {
      try {
        const res = await fetch('/api/health');
        if (res.ok && mountedRef.current) {
          const data: HealthData = await res.json();
          setUptime(formatUptime(data.uptime_seconds));
        }
      } catch {
        // Ignore
      }
    };

    fetchHealth();
    timer = setInterval(fetchHealth, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { uptime };
}

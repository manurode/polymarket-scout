import { useState, useEffect, useRef } from 'react';
import type { RateLimitBudget } from '../types';

const DEFAULT_BUDGETS: Record<string, RateLimitBudget> = {
  reconciliation: { available: 70, total: 100, label: 'Reconciliation' },
  onboarding: { available: 20, total: 100, label: 'Onboarding' },
  ad_hoc: { available: 10, total: 100, label: 'Ad-hoc' },
};

export function useRateLimits(pollIntervalMs: number = 5000) {
  const [budgets, setBudgets] = useState(DEFAULT_BUDGETS);
  const [loading, setLoading] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchBudgets = async () => {
      try {
        const res = await fetch('/api/system/rate-limits');
        if (res.ok && mountedRef.current) {
          const data: Record<string, RateLimitBudget> = await res.json();
          setBudgets(data);
          setLoading(false);
        }
      } catch {
        // Keep default data if server unreachable
      }
    };

    fetchBudgets();
    timer = setInterval(fetchBudgets, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { budgets, loading };
}

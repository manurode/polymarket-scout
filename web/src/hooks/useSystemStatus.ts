import { useState, useEffect, useRef } from 'react';
import type { SystemStatus } from '../types';

const DEFAULT_STATUS: SystemStatus = {
  mode: 'full',
  portfolio_epoch: 3,
  active_strategies: ['momentum_follow', 'market_making', 'corr_arb'],
  alpha_whales: 12,
  websocket_connected: true,
  tracked_markets_book: 50,
  tracked_markets_trades: 50,
  heartbeats: {
    clob_ws: { status: 'green', label: 'CLOB WebSocket', latency_ms: 12, subscribed: '50/50' },
    gamma_api: { status: 'green', label: 'Gamma API', latency_ms: 258 },
    polygon_rpc: { status: 'green', label: 'Polygon RPC', latency_s: 1.4 },
    redis_bus: { status: 'green', label: 'Redis Bus', latency_ms: 0.5 },
  },
  degradation_metrics: {
    mode: 'full',
    can_trade: true,
    component_health: { clob_ws: true, redis: true },
  },
};

/**
 * Hook that fetches system status from the backend API.
 * Falls back to default mock data if the server is unreachable.
 */
export function useSystemStatus(pollIntervalMs: number = 2000): SystemStatus {
  const [status, setStatus] = useState<SystemStatus>(DEFAULT_STATUS);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchStatus = async () => {
      try {
        const res = await fetch('/api/system/status');
        if (res.ok && mountedRef.current) {
          const data = await res.json();
          setStatus(data);
        }
      } catch {
        // Server not running — keep default mock data
      }
    };

    // Fetch immediately, then poll
    fetchStatus();
    timer = setInterval(fetchStatus, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return status;
}

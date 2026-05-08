import { useState, useEffect, useRef } from 'react';
import type { WalletStatus } from '../types';

const DEFAULT_WALLET: WalletStatus = {
  usdc_free: 10000,
  usdc_collateral: 0,
  usdc_total: 10000,
  pol_balance: 100,
  pol_usd_value: 82,
  ctf_allowance: true,
  ctf_contract: '0x4D97DCd7C0408F728A009Ff07556F758a0969709',
};

export function useWallet(pollIntervalMs: number = 5000) {
  const [wallet, setWallet] = useState<WalletStatus>(DEFAULT_WALLET);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchWallet = async () => {
      try {
        const res = await fetch('/api/wallet');
        if (res.ok && mountedRef.current) {
          const data: WalletStatus = await res.json();
          setWallet(data);
          setLoading(false);
        }
      } catch {
        // Keep default if server unreachable
      }
    };

    fetchWallet();
    timer = setInterval(fetchWallet, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { wallet, loading };
}

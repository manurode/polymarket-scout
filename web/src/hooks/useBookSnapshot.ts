import { useState, useEffect, useRef } from 'react';

export interface BookLevel {
  price: number;
  size: number;
}

export interface BookSnapshot {
  token_id: string;
  bids: BookLevel[];
  asks: BookLevel[];
  mid_price: number | null;
  spread: number | null;
  obi: number | null;
  tfi: number | null;
  source: string;
}

export function useBookSnapshot(pollIntervalMs: number = 10000): {
  book: BookSnapshot | null;
  source: string;
} {
  const [book, setBook] = useState<BookSnapshot | null>(null);
  const [source, setSource] = useState('no_data');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let timer: ReturnType<typeof setInterval>;

    const fetchBook = async () => {
      try {
        // Get first tracked token
        const trackedRes = await fetch('/api/markets/tracked');
        let tokenId = 'none';
        if (trackedRes.ok) {
          const tracked = await trackedRes.json();
          if (tracked.markets && tracked.markets.length > 0) {
            tokenId = tracked.markets[0].token_id;
          }
        }

        const res = await fetch(`/api/book/snapshot?token_id=${encodeURIComponent(tokenId)}`);
        if (res.ok && mountedRef.current) {
          const data: BookSnapshot = await res.json();
          setBook(data);
          setSource(data.source);
        }
      } catch {
        // Keep null
      }
    };

    fetchBook();
    timer = setInterval(fetchBook, pollIntervalMs);

    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [pollIntervalMs]);

  return { book, source };
}

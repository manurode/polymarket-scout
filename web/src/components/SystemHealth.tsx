import type { SystemStatus } from '../types';

interface Props {
  status: SystemStatus;
}

export function SystemHealth({ status }: Props) {
  const hb = status.heartbeats;

  return (
    <div className="p-4 space-y-4">
      {/* ── Section Header ────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold tracking-wider text-text-primary">
          SYSTEM HEALTH
        </h2>
        <span className="text-[10px] text-text-tertiary font-mono">
          last snapshot: 2.1s ago
        </span>
      </div>

      {/* ── Heartbeats + Rate Limits ──────────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        {/* Heartbeats */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            HEARTBEATS
          </h3>
          <div className="space-y-2">
            {Object.entries(hb).map(([key, beat]) => (
              <div key={key} className="flex items-center justify-between text-[11px] font-mono">
                <div className="flex items-center gap-2">
                  <span className={`
                    w-2 h-2 rounded-full
                    ${beat.status === 'green' ? 'bg-profit' : beat.status === 'amber' ? 'bg-warning' : 'bg-loss animate-pulse-red'}
                  `} />
                  <span className="text-text-secondary">{beat.label}</span>
                </div>
                <span className="text-text-primary">
                  {beat.latency_ms != null ? `${beat.latency_ms}ms` : ''}
                  {beat.latency_s != null ? `${beat.latency_s}s` : ''}
                  {beat.subscribed ? ` · ${beat.subscribed}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Rate Limit Budget */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            RATE LIMIT BUDGET
          </h3>
          <div className="space-y-3">
            <SLBudget label="Reconciliation" pct={70} color="bg-info" />
            <SLBudget label="Onboarding" pct={20} color="bg-profit" />
            <SLBudget label="Ad-hoc" pct={10} color="bg-text-tertiary" />
          </div>
          <div className="mt-2 text-[10px] text-text-tertiary font-mono">
            Tokens available: 3.2 / 4.0
          </div>
        </div>
      </div>

      {/* ── Reconciliation Matrix ──────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          RECONCILIATION MATRIX
        </h3>
        <div className="flex items-center gap-2 mb-1.5">
          <div className="flex-1 bg-bg-tertiary rounded-full h-2.5 overflow-hidden">
            <div className="bg-profit h-full rounded-full" style={{ width: '94%' }} />
          </div>
          <span className="text-[11px] font-mono text-profit">47/50 CLEAN</span>
        </div>
        <div className="text-[10px] text-text-tertiary">
          No markets in RECONCILING state
        </div>
      </div>

      {/* ── Degradation + Latency Budget ───────────────────────── */}
      <div className="grid grid-cols-2 gap-4">
        {/* Degradation Mode */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            DEGRADATION MODE
          </h3>
          <div className="flex items-center gap-3">
            <div className="w-16 h-16 rounded-full border-4 border-profit flex items-center justify-center">
              <span className="text-sm font-bold text-profit">FULL</span>
            </div>
            <div className="text-[11px] text-text-secondary space-y-0.5">
              <div>✓ Market Making</div>
              <div>✓ Correlation Arb</div>
              <div>✓ Momentum</div>
            </div>
          </div>
        </div>

        {/* Latency Budget */}
        <div className="bg-bg-secondary border border-bg-hover rounded p-3">
          <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
            LATENCY BUDGET (Critical Path)
          </h3>
          <div className="space-y-1.5">
            <LatencyBar label="WS→Book" actual={1.2} budget={5} />
            <LatencyBar label="OBI+TFI→Spoof" actual={2.8} budget={5} />
            <LatencyBar label="Signal→Decision" actual={8.1} budget={10} />
            <LatencyBar label="Kelly→Position" actual={1.9} budget={5} />
            <LatencyBar label="Risk→Trade" actual={0.8} budget={3} />
            <div className="border-t border-bg-hover mt-2 pt-1.5 flex justify-between text-[10px] font-mono">
              <span className="text-text-secondary">TOTAL</span>
              <span className="text-text-primary">14.8ms / 25ms (59%)</span>
            </div>
          </div>
        </div>
      </div>

      {/* ── Wallet Monitor ─────────────────────────────────────── */}
      <div className="bg-bg-secondary border border-bg-hover rounded p-3">
        <h3 className="text-[10px] text-text-tertiary tracking-wider mb-2">
          WALLET MONITOR (On-Chain · Polygon)
        </h3>
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-bg-tertiary rounded p-2.5">
            <div className="text-[10px] text-text-tertiary mb-1">USDC</div>
            <div className="text-sm font-mono font-bold text-text-primary">$1,247.50</div>
            <div className="text-[10px] text-text-tertiary mt-0.5">Free · $892.30 in collateral</div>
          </div>
          <div className="bg-bg-tertiary rounded p-2.5">
            <div className="text-[10px] text-text-tertiary mb-1">POL (Gas)</div>
            <div className="text-sm font-mono font-bold text-profit">4.2 POL</div>
            <div className="text-[10px] text-text-tertiary mt-0.5">$3.44 · MIN: 2.0 POL</div>
          </div>
          <div className="bg-bg-tertiary rounded p-2.5">
            <div className="text-[10px] text-text-tertiary mb-1">CTF Allowance</div>
            <div className="text-sm font-mono font-bold text-profit">✓ APPROVED</div>
            <div className="text-[10px] text-text-tertiary mt-0.5">Unlimited · CTFExchange</div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Mini components ──────────────────────────────────────────────────

function SLBudget({ label, pct, color }: { label: string; pct: number; color: string }) {
  return (
    <div>
      <div className="flex justify-between text-[10px] font-mono mb-0.5">
        <span className="text-text-secondary">{label}</span>
        <span className="text-text-primary">{pct}%</span>
      </div>
      <div className="bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
        <div className={`${color} h-full rounded-full`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function LatencyBar({ label, actual, budget }: { label: string; actual: number; budget: number }) {
  const pct = (actual / budget) * 100;
  const color = pct > 80 ? 'bg-loss' : pct > 50 ? 'bg-warning' : 'bg-profit';
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className="text-text-tertiary w-28">{label}</span>
      <div className="flex-1 bg-bg-tertiary rounded-full h-1.5 overflow-hidden">
        <div className={`${color} h-full rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-text-primary w-24 text-right">{actual}ms / {budget}ms</span>
    </div>
  );
}

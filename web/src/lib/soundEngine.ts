/**
 * Scout Lab v2.0 — Sound Engine
 *
 * Synthesizes alert sounds using Web Audio API.
 * No audio files — all sounds are generated programmatically for zero latency
 * and dynamic frequency/duration customization.
 *
 * Sound palette (per DASHBOARD.md §6.4):
 * - Click (metallic tick): order fill confirmed
 * - Bell (ding): position closed in profit
 * - Double tone (dry): position closed in loss
 * - Alarm (pulsing): τ > 85%, toxicity > 1.5, whale enters market
 * - Siren (sweep): RECONCILING, spoof ≥ 0.7, kill switch
 * - POL alert (triple pulse grave): gas < 2.0 POL
 */

type SoundType = 'click' | 'bell' | 'loss' | 'alarm' | 'siren' | 'pol';

class SoundEngine {
  private ctx: AudioContext | null = null;
  private _muted = false;

  private getContext(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext();
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume();
    }
    return this.ctx;
  }

  /** Play a sound by type. Critical sounds ignore mute. */
  play(type: SoundType) {
    // Critical sounds ALWAYS play (siren, pol)
    const isCritical = type === 'siren' || type === 'pol';
    if (this._muted && !isCritical) return;

    switch (type) {
      case 'click': this._click(); break;
      case 'bell': this._bell(); break;
      case 'loss': this._loss(); break;
      case 'alarm': this._alarm(); break;
      case 'siren': this._siren(); break;
      case 'pol': this._pol(); break;
    }
  }

  set muted(v: boolean) { this._muted = v; }
  get muted() { return this._muted; }

  // ── Sound generators ─────────────────────────────────────────

  /** Metallic tick — 50ms, 800Hz. Fast decay simulates metal impact. */
  private _click() {
    const ctx = this.getContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = 800;
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.05);
    osc.connect(gain).connect(ctx.destination);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.05);
  }

  /** Bell ding — 200ms, 1200Hz. Higher pitch for bigger profit. */
  private _bell() {
    const ctx = this.getContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = 1200;
    gain.gain.setValueAtTime(0.25, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.2);
    osc.connect(gain).connect(ctx.destination);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.2);
  }

  /** Double dry tone — 2 × 80ms, 400Hz. Unpleasant by design. */
  private _loss() {
    const ctx = this.getContext();
    [0, 0.12].forEach(delay => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.value = 400;
      gain.gain.setValueAtTime(0.3, ctx.currentTime + delay);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.08);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + 0.08);
    });
  }

  /** Pulsing alarm — 3 pulses, 200ms each, 600Hz. Repeats every 30s. */
  private _alarm() {
    const ctx = this.getContext();
    for (let i = 0; i < 3; i++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const t = ctx.currentTime + i * 0.25;
      osc.type = 'square';
      osc.frequency.value = 600;
      gain.gain.setValueAtTime(0.25, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.2);
    }
  }

  /** Siren — frequency sweep 300→1200Hz over 500ms, repeated twice. */
  private _siren() {
    const ctx = this.getContext();
    for (let rep = 0; rep < 2; rep++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const t = ctx.currentTime + rep * 0.55;
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(300, t);
      osc.frequency.linearRampToValueAtTime(1200, t + 0.5);
      gain.gain.setValueAtTime(0.2, t);
      gain.gain.setValueAtTime(0.2, t + 0.45);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.5);
    }
  }

  /** POL alert — triple low pulse, 100ms, 200Hz. Distinct from trading sounds. */
  private _pol() {
    const ctx = this.getContext();
    for (let i = 0; i < 3; i++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const t = ctx.currentTime + i * 0.2;
      osc.type = 'triangle';
      osc.frequency.value = 200;
      gain.gain.setValueAtTime(0.35, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.1);
    }
  }
}

/** Singleton sound engine instance. */
export const soundEngine = new SoundEngine();

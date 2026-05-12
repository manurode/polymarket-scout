"""
Signal Pipeline — Genera señales de trading reales desde datos del Radar.

Estrategias implementadas (sin depender del CLOB):
1. Momentum (trend following)
2. Mean Reversion
3. Volume Breakout

Cada estrategia analiza los snapshots del radar y devuelve señales
con dirección, precio de entrada y confianza.

Uso:
    pipeline = SignalPipeline()
    signals = pipeline.generate(snapshots)
    # signals: [{"market": "...", "strategy": "momentum", "side": "YES", 
    #            "entry_price": 0.62, "confidence": 0.7}, ...]
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MarketHistory:
    """Historial de precios para un mercado."""
    condition_id: str
    question: str
    prices: list[float] = field(default_factory=list)  # últimos N precios
    volumes: list[float] = field(default_factory=list)  # últimos N volúmenes
    spreads: list[float] = field(default_factory=list)
    max_history: int = 20

    def add_snapshot(self, price: float, volume: float, spread: float | None) -> None:
        self.prices.append(price)
        self.volumes.append(volume)
        if spread is not None:
            self.spreads.append(spread)
        if len(self.prices) > self.max_history:
            self.prices.pop(0)
            if self.volumes:
                self.volumes.pop(0)
            if self.spreads:
                self.spreads.pop(0)

    @property
    def current_price(self) -> float | None:
        return self.prices[-1] if self.prices else None

    @property
    def ma(self) -> float | None:
        """Media móvil simple."""
        if len(self.prices) < 3:
            return None
        return sum(self.prices) / len(self.prices)

    @property
    def momentum(self) -> float | None:
        """Momentum: cambio porcentual desde hace N/2 períodos."""
        if len(self.prices) < 4:
            return None
        half = len(self.prices) // 2
        old = sum(self.prices[:half]) / half
        recent = sum(self.prices[half:]) / (len(self.prices) - half)
        if old == 0:
            return 0
        return (recent - old) / old

    @property
    def avg_volume(self) -> float:
        if not self.volumes:
            return 0
        return sum(self.volumes) / len(self.volumes)

    @property
    def volume_spike(self) -> float | None:
        """Ratio de volumen actual vs media. >2 = spike."""
        if len(self.volumes) < 3:
            return None
        avg = self.avg_volume
        if avg == 0:
            return 1.0
        return self.volumes[-1] / avg

    @property
    def avg_spread(self) -> float | None:
        if not self.spreads:
            return None
        return sum(self.spreads) / len(self.spreads)

    @property
    def recent_volume_ma(self) -> float | None:
        """Media móvil de volumen en la ventana de historia. Usado para confluencia."""
        if len(self.volumes) < 3:
            return None
        # Excluir el último (volumen actual) de la MA para evitar sesgo
        if len(self.volumes) >= 4:
            return sum(self.volumes[:-1]) / (len(self.volumes) - 1)
        return sum(self.volumes) / len(self.volumes)

    @property
    def recent_volume_3min(self) -> float:
        """Volumen más reciente (último snapshot)."""
        return self.volumes[-1] if self.volumes else 0.0


@dataclass
class Signal:
    """Señal de trading generada por una estrategia."""
    market: str
    question: str
    condition_id: str
    strategy: str          # "momentum", "mean_reversion", "volume_breakout"
    side: str              # "YES" | "NO"
    entry_price: float
    confidence: float      # 0.0 - 1.0
    reason: str

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "question": self.question,
            "condition_id": self.condition_id,
            "strategy": self.strategy,
            "side": self.side,
            "entry_price": self.entry_price,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class SignalPipeline:
    """Pipeline de señales multi-estrategia."""

    def __init__(self):
        self._history: dict[str, MarketHistory] = {}
        self._last_signal_time: dict[str, float] = {}  # cooldown por mercado

    def update_history(self, snapshots: list[dict]) -> None:
        """Registra nuevos snapshots en el historial."""
        for snap in snapshots:
            cid = snap.get("condition_id", "")
            if not cid:
                continue
            price = snap.get("price_yes")
            volume = snap.get("volume", 0)
            spread = snap.get("spread")

            if price is None:
                continue

            if cid not in self._history:
                self._history[cid] = MarketHistory(
                    condition_id=cid,
                    question=snap.get("question", ""),
                )

            self._history[cid].add_snapshot(
                price=float(price),
                volume=float(volume),
                spread=float(spread) if spread else None,
            )

    def generate(self, snapshots: list[dict], cooldown_s: float = 120) -> list[Signal]:
        """Genera señales de trading a partir de los snapshots actuales.

        Parameters
        ----------
        snapshots : list[dict]
            Snapshots del radar scan actual.
        cooldown_s : float
            Tiempo mínimo entre señales para el mismo mercado.

        Returns
        -------
        list[Signal]
            Señales ordenadas por confianza descendente.
        """
        import time

        # Actualizar historial
        self.update_history(snapshots)
        signals: list[Signal] = []

        for snap in snapshots:
            cid = snap.get("condition_id", "")
            if not cid:
                continue

            hist = self._history.get(cid)
            if not hist:
                continue

            # ── Cooldown check ──
            now = time.time()
            if cid in self._last_signal_time:
                if now - self._last_signal_time[cid] < cooldown_s:
                    continue

            # ── Estrategias ──

            # 1. Momentum
            mom = hist.momentum
            if mom is not None and abs(mom) > 0.02:  # 2% threshold
                side = "YES" if mom > 0 else "NO"
                conf = min(abs(mom) / 0.1, 0.9)  # scale to 0-0.9
                if conf > 0.3:  # minimum confidence
                    signals.append(Signal(
                        market=hist.question[:60],
                        question=hist.question,
                        condition_id=cid,
                        strategy="momentum",
                        side=side,
                        entry_price=hist.current_price or 0,
                        confidence=round(conf, 2),
                        reason=f"Momentum {mom:+.1%} over {len(hist.prices)} periods",
                    ))

            # 2. Mean Reversion
            if hist.ma and hist.current_price and len(hist.prices) >= 8:
                deviation = (hist.current_price - hist.ma) / hist.ma if hist.ma > 0 else 0
                if abs(deviation) > 0.05:  # 5% deviation from MA
                    # Apostar a que vuelve a la media
                    side = "NO" if deviation > 0 else "YES"
                    conf = min(abs(deviation) / 0.15, 0.8)
                    if conf > 0.3:
                        signals.append(Signal(
                            market=hist.question[:60],
                            question=hist.question,
                            condition_id=cid,
                            strategy="mean_reversion",
                            side=side,
                            entry_price=hist.current_price,
                            confidence=round(conf, 2),
                            reason=f"Deviation {deviation:+.1%} from MA {hist.ma:.3f}",
                        ))

            # 3. Volume Breakout
            vol_spike = hist.volume_spike
            if vol_spike is not None and vol_spike > 2.5:  # 2.5x normal volume
                # High volume → entrar en dirección del momentum o long
                side = "YES"  # default bullish on high volume
                if mom is not None and mom < -0.01:
                    side = "NO"
                conf = min((vol_spike - 2) / 5, 0.7)  # scale 0-0.7
                if conf > 0.25:
                    signals.append(Signal(
                        market=hist.question[:60],
                        question=hist.question,
                        condition_id=cid,
                        strategy="volume_breakout",
                        side=side,
                        entry_price=hist.current_price or 0,
                        confidence=round(conf, 2),
                        reason=f"Volume spike {vol_spike:.1f}x avg (${hist.volumes[-1]:.0f} vs ${hist.avg_volume:.0f})",
                    ))

        # ── Ordenar por confianza y limitar ──
        signals.sort(key=lambda s: s.confidence, reverse=True)

        # Marcar cooldown
        for sig in signals[:5]:  # solo las top 5
            self._last_signal_time[sig.condition_id] = time.time()

        return signals[:5]  # máximo 5 señales por ciclo

    def get_history_size(self) -> int:
        """Número de mercados con historial."""
        return sum(1 for h in self._history.values() if len(h.prices) >= 4)

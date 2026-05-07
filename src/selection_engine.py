"""
Selection Engine — Ranking de mercados para el Top 50.

La arquitectura v2.0 usa un sistema de dos capas:
  1. Radar (Gamma API) escanea ~200 mercados.
  2. Selection Engine rankea y selecciona el Top 50 para deep-dive.

El score compuesto prioriza mercados con alto volumen, buena liquidez,
lejos de la expiración y con spread razonable.

Usage:
    engine = SelectionEngine(top_n=50)
    top50, events = engine.rank(snapshots)
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Pesos del score ───────────────────────────────────────────────

SCORE_WEIGHTS = {
    "volume": 0.40,
    "liquidity": 0.30,
    "recency": 0.20,
    "spread": 0.10,
}

# Umbrales
SPREAD_PENALTY_THRESHOLD = 0.15   # spreads > 15% reciben penalización
EXPIRY_WARN_HOURS = 24            # mercados que expiran en < 24h penalizados


@dataclass
class MarketScore:
    """Resultado del scoring para un mercado."""
    condition_id: str
    question: str
    slug: str
    volume: float
    liquidity: float
    spread: Optional[float]
    score: float
    snapshot: dict = field(repr=False)


@dataclass
class RankingResult:
    """Resultado completo del ranking."""
    top: list[MarketScore]
    all_scored: list[MarketScore]
    enter: list[str]   # condition_ids que ENTRAN al Top N
    exit: list[str]    # condition_ids que SALEN del Top N


class SelectionEngine:
    """Rankea mercados por score compuesto y mantiene el Top N.

    Parameters
    ----------
    top_n : int
        Número de mercados en el Top (default 50).
    weights : dict
        Pesos para cada componente del score.
    """

    def __init__(self, top_n: int = 50, weights: dict | None = None):
        self.top_n = top_n
        self.weights = weights or SCORE_WEIGHTS.copy()

        # Estado: condition_ids que estaban en el Top N en el ranking anterior
        self._previous_top: set[str] = set()

    # ── Scoring ───────────────────────────────────────────────────

    def _compute_score(
        self,
        snapshot: dict,
        max_volume: float = 1.0,
        max_liquidity: float = 1.0,
    ) -> float:
        """Calcula el score para un mercado individual.

        Componentes:
          - volume_score:   log10 normalizado del volumen
          - liquidity_score: log10 normalizado de la liquidez
          - recency_score:   penalización por cercanía a expiración
          - spread_score:    bonificación/penalización por spread
        """
        volume = max(0, snapshot.get("volume", 0) or 0)
        liquidity = max(0, snapshot.get("liquidity", 0) or 0)
        spread = snapshot.get("spread")

        # Volume: log10 normalizado contra el máximo del batch
        vol_score = math.log10(volume + 1) / math.log10(max_volume + 1) if max_volume > 1 else 0

        # Liquidity: similar
        liq_score = math.log10(liquidity + 1) / math.log10(max_liquidity + 1) if max_liquidity > 1 else 0

        # Recency: 0 si va a expirar pronto, 1 si tiene mucho tiempo
        recency_score = 1.0
        end_date = snapshot.get("end_date")  # opcional, no siempre disponible
        if end_date:
            hours_left = (end_date - time.time()) / 3600
            if hours_left <= 0:
                recency_score = 0.0
            elif hours_left < EXPIRY_WARN_HOURS:
                recency_score = hours_left / EXPIRY_WARN_HOURS
            # else: 1.0

        # Spread: bonificación si es tight, penalización si es wide
        if spread is not None and spread > 0:
            if spread <= 0.02:
                spread_score = 1.0       # spread muy tight → bonificación
            elif spread >= SPREAD_PENALTY_THRESHOLD:
                spread_score = 0.0       # spread > 15% → penalización total
            else:
                # Interpolación lineal entre 0.02 y 0.15
                spread_score = 1.0 - (spread - 0.02) / (SPREAD_PENALTY_THRESHOLD - 0.02)
        else:
            spread_score = 0.5  # sin datos de spread → neutro

        # Score compuesto
        return (
            vol_score * self.weights["volume"]
            + liq_score * self.weights["liquidity"]
            + recency_score * self.weights["recency"]
            + spread_score * self.weights["spread"]
        )

    # ── Ranking ───────────────────────────────────────────────────

    def rank(self, snapshots: list[dict]) -> RankingResult:
        """Rankea todos los snapshots y retorna el Top N + eventos de cambio.

        Parameters
        ----------
        snapshots : list[dict]
            Lista de snapshots de mercado (formato scanner).

        Returns
        -------
        RankingResult
            Con el Top N, todos los scores, y listas de entradas/salidas.
        """
        if not snapshots:
            return RankingResult(top=[], all_scored=[], enter=[], exit=[])

        # Normalización: encontrar máximos del batch
        max_vol = max((s.get("volume", 0) or 0) for s in snapshots)
        max_liq = max((s.get("liquidity", 0) or 0) for s in snapshots)

        # Calcular scores
        scored = []
        for s in snapshots:
            score = self._compute_score(s, max_vol, max_liq)
            scored.append(MarketScore(
                condition_id=s.get("condition_id", ""),
                question=s.get("question", ""),
                slug=s.get("slug", ""),
                volume=s.get("volume", 0) or 0,
                liquidity=s.get("liquidity", 0) or 0,
                spread=s.get("spread"),
                score=score,
                snapshot=s,
            ))

        # Ordenar descendente por score
        scored.sort(key=lambda ms: ms.score, reverse=True)

        # Top N
        top = scored[:self.top_n]
        current_ids = {ms.condition_id for ms in top}

        # Detectar entradas y salidas
        enter_ids = current_ids - self._previous_top
        exit_ids = self._previous_top - current_ids

        # Actualizar estado
        self._previous_top = current_ids

        return RankingResult(
            top=top,
            all_scored=scored,
            enter=list(enter_ids),
            exit=list(exit_ids),
        )

    # ── Consulta ──────────────────────────────────────────────────

    def is_top(self, condition_id: str) -> bool:
        """Verifica si un condition_id está actualmente en el Top N."""
        return condition_id in self._previous_top

    def get_top_ids(self) -> set[str]:
        """Retorna los condition_ids del Top N actual."""
        return self._previous_top.copy()

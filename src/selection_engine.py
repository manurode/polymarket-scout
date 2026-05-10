"""
Selection Engine — Ranking de mercados para el Top 50.

La arquitectura v2.0 usa un sistema de dos capas:
  1. Radar (Gamma API) escanea ~200 mercados.
  2. Selection Engine rankea y selecciona el Top 50 para deep-dive.

El score compuesto prioriza mercados con alto volumen RECIENTE, buena liquidez,
lejos de la expiración y con spread razonable.

Filtros anti-mercados-zombi (v2.1):
  - Prioridad temporal: volumen_24h tiene un 70% del peso, volumen_total solo 10%.
  - Filtro de spread: spreads >15% reciben un multiplicador de penalización 0.1x.
  - Liquidez real: mercados con liquidez <$500 (dato Gamma API) quedan excluidos del Top N.

Usage:
    engine = SelectionEngine(top_n=50)
    result = engine.rank(snapshots)
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Pesos del score ───────────────────────────────────────────────
# v2.1: El volumen se divide en 24h (señal reciente) y total (histórico).
# Esto evita que mercados zombi con mucho volumen histórico pero inactivos
# acaparen los slots del Top 50.

SCORE_WEIGHTS = {
    "volume_24h": 0.70,   # Volumen últimas 24h — señal de actividad real
    "volume_total": 0.10, # Volumen histórico — peso reducido para evitar zombis
    "liquidity": 0.10,    # Liquidez actual del libro
    "recency": 0.10,      # Tiempo hasta expiración
}

# Umbrales
SPREAD_PENALTY_THRESHOLD = 0.15   # spreads > 15% → multiplicador 0.1x en score final
SPREAD_PENALTY_MULTIPLIER = 0.1   # Factor de penalización para spreads excesivos
MIN_LIQUIDITY_USD = 500.0          # Liquidez mínima para entrar al Top N (Gamma API)
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
        max_volume_24h: float = 1.0,
        max_volume_total: float = 1.0,
        max_liquidity: float = 1.0,
    ) -> float:
        """Calcula el score para un mercado individual.

        Componentes (v2.1):
          - volume_24h_score:  log10 normalizado del volumen de las últimas 24h (peso 70%).
          - volume_total_score: log10 normalizado del volumen histórico total (peso 10%).
          - liquidity_score:   log10 normalizado de la liquidez del libro (peso 10%).
          - recency_score:     penalización por cercanía a expiración (peso 10%).

        Penalización de spread (multiplicador post-score):
          Si spread > 15%, el score final se multiplica por SPREAD_PENALTY_MULTIPLIER (0.1).
          Esto envía efectivamente los mercados zombi al final de la lista.
        """
        # ── Volúmenes ─────────────────────────────────────────────
        # volume_24h: campo específico de actividad reciente; fallback a 0
        volume_24h = max(0, snapshot.get("volume_24h", 0) or 0)
        # volume: volumen histórico total del mercado
        volume_total = max(0, snapshot.get("volume", 0) or 0)
        liquidity = max(0, snapshot.get("liquidity", 0) or 0)
        spread = snapshot.get("spread")

        # ── Volume 24h: log10 normalizado contra el máximo del batch ──
        vol_24h_score = (
            math.log10(volume_24h + 1) / math.log10(max_volume_24h + 1)
            if max_volume_24h > 1 else 0.0
        )

        # ── Volume total: log10 normalizado ───────────────────────
        vol_total_score = (
            math.log10(volume_total + 1) / math.log10(max_volume_total + 1)
            if max_volume_total > 1 else 0.0
        )

        # ── Liquidity: log10 normalizado ──────────────────────────
        liq_score = (
            math.log10(liquidity + 1) / math.log10(max_liquidity + 1)
            if max_liquidity > 1 else 0.0
        )

        # ── Recency: 0 si va a expirar pronto, 1 si tiene mucho tiempo ──
        recency_score = 1.0
        end_date = snapshot.get("end_date")  # opcional, no siempre disponible
        if end_date:
            hours_left = (end_date - time.time()) / 3600
            if hours_left <= 0:
                recency_score = 0.0
            elif hours_left < EXPIRY_WARN_HOURS:
                recency_score = hours_left / EXPIRY_WARN_HOURS
            # else: 1.0

        # ── Score base compuesto ──────────────────────────────────
        base_score = (
            vol_24h_score   * self.weights["volume_24h"]
            + vol_total_score * self.weights["volume_total"]
            + liq_score       * self.weights["liquidity"]
            + recency_score   * self.weights["recency"]
        )

        # ── Penalización de spread (multiplicador post-score) ─────
        # Spreads > 15% son señal de mercado zombi o ilíquido.
        # Aplicamos un multiplicador 0.1x para empujarlos al final de la lista.
        if spread is not None and spread > SPREAD_PENALTY_THRESHOLD:
            base_score *= SPREAD_PENALTY_MULTIPLIER

        return base_score

    # ── Ranking ───────────────────────────────────────────────────

    def rank(self, snapshots: list[dict]) -> RankingResult:
        """Rankea todos los snapshots y retorna el Top N + eventos de cambio.

        Parameters
        ----------
        snapshots : list[dict]
            Lista de snapshots de mercado (formato scanner). Se espera que cada
            snapshot incluya los campos ``volume`` (total), ``volume_24h`` (últimas
            24h, dato Gamma API), ``liquidity`` y ``spread``.

        Returns
        -------
        RankingResult
            Con el Top N, todos los scores, y listas de entradas/salidas.

        Notes
        -----
        Filtros anti-zombi aplicados antes del ranking (v2.1):
          1. Mercados con ``liquidity`` < MIN_LIQUIDITY_USD ($500) quedan excluidos
             del Top N independientemente de su score.
          2. Mercados con ``spread`` > SPREAD_PENALTY_THRESHOLD (15%) reciben un
             multiplicador 0.1x sobre su score final.
        """
        if not snapshots:
            return RankingResult(top=[], all_scored=[], enter=[], exit=[])

        # Normalización: encontrar máximos del batch
        max_vol_24h = max((s.get("volume_24h", 0) or 0) for s in snapshots)
        max_vol_total = max((s.get("volume", 0) or 0) for s in snapshots)
        max_liq = max((s.get("liquidity", 0) or 0) for s in snapshots)

        # Calcular scores
        scored = []
        for s in snapshots:
            score = self._compute_score(s, max_vol_24h, max_vol_total, max_liq)
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

        # ── Filtro de liquidez real (Gamma API) ───────────────────
        # Mercados con liquidez < MIN_LIQUIDITY_USD ($500) no pueden entrar al Top N.
        # Se puntúan igualmente (para diagnóstico en all_scored) pero quedan excluidos.
        eligible = [ms for ms in scored if ms.liquidity >= MIN_LIQUIDITY_USD]
        ineligible_ids = {ms.condition_id for ms in scored if ms.liquidity < MIN_LIQUIDITY_USD}

        # Top N: solo mercados elegibles por liquidez
        top = eligible[:self.top_n]
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

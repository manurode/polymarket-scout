"""
Selection Engine — Ranking de mercados para el Top 50.

La arquitectura v2.0 usa un sistema de dos capas:
  1. Radar (Gamma API) escanea ~200 mercados.
  2. Selection Engine rankea y selecciona el Top 50 para deep-dive.

El score compuesto prioriza mercados con alto volumen RECIENTE, buena liquidez,
lejos de la expiración y con spread razonable.

Filtros anti-mercados-zombi — "Filtro de Pulso" (v3.1):
  - Hard Gate de Actividad: vol24h == 0 → score=0, excepto mercados nuevos (<48h).
  - Validación de Spread: spread == "N/A" o libro vacío → ignorado, SALVO que
    vol24h > $1000 (margen de cortesía para mercados aún no consultados al CLOB).
  - Densidad de Órdenes: order_count==0 → ignorado, SALVO margen de cortesía.
  - Ranking Inverso de Pulso: los mercados con actividad se ordenan por vol24h DESC.
  - Hard limit de liquidez: liquidez < $500 → score=0.
  - Penalización agresiva de spread: spreads > 10% reciben un multiplicador 0.01x.
  - Filtro de probabilidad extrema: precio < 2% o > 98% con spread alto → score=0.

Usage:
    engine = SelectionEngine(top_n=50)
    result = engine.rank(snapshots)
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Pesos del score ───────────────────────────────────────────────
# v3.0 "Filtro de Pulso": el vol24h sigue siendo dominante.
# La liquidez ahora solo puntúa si hay órdenes reales en el CLOB
# (order_density), evitando que la liquidez "fantasma" de Gamma infle el score.

SCORE_WEIGHTS = {
    "volume_24h":    0.75,  # Volumen últimas 24h — señal de actividad real (dominante)
    "order_density": 0.15,  # Densidad de órdenes en el CLOB (reemplaza liquidez bruta)
    "recency":       0.10,  # Tiempo hasta expiración
}

# Umbrales
SPREAD_PENALTY_THRESHOLD    = 0.10    # spreads > 10% → multiplicador 0.01x en score final
SPREAD_PENALTY_MULTIPLIER   = 0.01    # Factor de penalización AGRESIVO para spreads excesivos
MIN_LIQUIDITY_USD           = 500.0   # Liquidez mínima — mercados por debajo reciben score=0
EXTREME_PRICE_LOW           = 0.02    # Precio < 2%: evento considerado imposible
EXTREME_PRICE_HIGH          = 0.98    # Precio > 98%: evento considerado seguro
EXPIRY_WARN_HOURS           = 24      # mercados que expiran en < 24h penalizados
NEW_MARKET_WINDOW_HOURS     = 48      # Mercados creados en las últimas N horas son "nuevos"
# Margen de cortesía: mercados con vol24h > este umbral pueden pasar el Hard Gate
# de spread/order_count vacíos. El Scanner L1 (Gamma) aún no ha consultado el CLOB;
# el WS (L2) los validará en el siguiente ciclo.
MIN_VOL24H_FOR_SPREAD_GRACE = 1_000.0  # $1 000 — umbral para el margen de cortesía


@dataclass
class MarketScore:
    """Resultado del scoring para un mercado."""
    condition_id: str
    question: str
    slug: str
    volume: float
    volume_24h: float
    liquidity: float
    spread: Optional[float]
    order_count: int
    score: float
    snapshot: dict = field(repr=False)


@dataclass
class RankingResult:
    """Resultado completo del ranking."""
    top: list[MarketScore]
    all_scored: list[MarketScore]
    enter: list[str]   # condition_ids que ENTRAN al Top N
    exit: list[str]    # condition_ids que SALEN del Top N


def _is_new_market(snapshot: dict) -> bool:
    """Retorna True si el mercado probablemente es nuevo (< NEW_MARKET_WINDOW_HOURS horas).

    Estrategia en dos pasos:
    1. Si ``created_at`` (timestamp UNIX) está disponible, lo usa directamente.
    2. Si no está disponible (Gamma a veces no lo expone), usa una heurística:
       volumen total bajo + vol24h == 0 es señal fuerte de mercado recién creado.
    """
    created_at = snapshot.get("created_at")  # timestamp UNIX esperado
    if created_at is not None and created_at > 0:
        age_hours = (time.time() - float(created_at)) / 3600
        return age_hours <= NEW_MARKET_WINDOW_HOURS

    # Heurística: sin created_at → mercado con volumen total muy bajo y sin vol24h
    # probablemente acabó de crearse. Le damos el beneficio de la duda.
    volume_total = snapshot.get("volume", 0) or 0
    volume_24h   = snapshot.get("volume_24h", 0) or 0
    return volume_total < 500 and volume_24h == 0


def _parse_spread(snapshot: dict) -> Optional[float]:
    """
    Extrae el spread del snapshot de forma robusta.

    - Si spread es None, "N/A" o string vacío → retorna None (libro vacío / sin datos).
    - Si spread es numérico → lo retorna como float.
    """
    raw = snapshot.get("spread")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw_stripped = raw.strip().upper()
        if raw_stripped in ("N/A", "", "NULL", "NONE"):
            return None
        try:
            return float(raw_stripped.replace("%", "")) / 100.0 if "%" in raw_stripped else float(raw_stripped)
        except ValueError:
            return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
        max_order_count: float = 1.0,
    ) -> float:
        """Calcula el score para un mercado individual.

        Componentes (v3.0 — Filtro de Pulso):
          - volume_24h_score:  log10 normalizado del volumen de las últimas 24h (peso 75%).
          - order_density_score: log10 normalizado del número de órdenes CLOB (peso 15%).
          - recency_score:     penalización por cercanía a expiración (peso 10%).

        Hard Gates (score=0 inmediato, antes de cualquier cálculo):
          1. spread == None → libro no consultado.
             EXCEPCIÓN: si vol24h > MIN_VOL24H_FOR_SPREAD_GRACE → margen de cortesía
             (pasa con score reducido; el WS lo validará en el siguiente ciclo).
          2. order_count == 0 → CLOB sin órdenes.
             EXCEPCIÓN: misma gracia que Gate 1 si vol24h suficiente.
          3. vol24h == 0 y el mercado NO es nuevo (<48h) → sin pulso.
          4. Liquidez < MIN_LIQUIDITY_USD ($500) → inoperable.
          5. Precio en extremos (< 2% o > 98%) con spread alto → score=0.

        Penalización agresiva de spread (multiplicador post-score):
          Si spread > 10%, el score final se multiplica por 0.01.
        """
        # ── Datos base ────────────────────────────────────────────
        volume_24h   = max(0, snapshot.get("volume_24h", 0) or 0)
        liquidity    = max(0, snapshot.get("liquidity", 0) or 0)
        order_count  = max(0, snapshot.get("order_count", 0) or 0)  # órdenes en el CLOB
        price        = snapshot.get("price") or snapshot.get("price_yes")  # precio mid
        spread       = _parse_spread(snapshot)

        # ── Margen de cortesía: ¿puede ignorar Gates 1 y 2? ──────
        # Un mercado con vol24h > $1000 pero sin datos de CLOB aún
        # (el Scanner L1 aún no lo ha enriquecido) recibe el beneficio
        # de la duda. El WS lo validará en el siguiente ciclo.
        has_vol24h_grace = volume_24h >= MIN_VOL24H_FOR_SPREAD_GRACE

        # ── HARD GATE 1: Spread N/A → libro de órdenes sin datos ──
        # Si el spread es N/A y NO tiene volumen suficiente, el mercado
        # es un zombi. Si SÍ tiene volumen, lo dejamos pasar con un score
        # conservador (el CLOB lo validará pronto).
        if spread is None and not has_vol24h_grace:
            return 0.0

        # ── HARD GATE 2: CLOB vacío → sin órdenes activas ────────
        # order_count proviene del análisis del libro CLOB.
        # Si es 0 y no tiene volumen suficiente → zombi confirmado.
        if order_count == 0 and not has_vol24h_grace:
            return 0.0

        # ── HARD GATE 3: vol24h == 0 → sin pulso ─────────────────
        # El mercado no se ha movido hoy. Excepto si es recién creado
        # (< 48h), en cuyo caso se le da el beneficio de la duda.
        if volume_24h == 0 and not _is_new_market(snapshot):
            return 0.0

        # ── HARD GATE 4: Liquidez mínima ─────────────────────────
        # Si la liquidez del libro (Gamma API) es inferior a $500,
        # el mercado es inoperable. Score=0 sin más cálculos.
        if liquidity < MIN_LIQUIDITY_USD:
            return 0.0

        # ── HARD GATE 5: Probabilidad extrema + spread alto ──────
        # Mercados con precio < 2% o > 98% y spread elevado son
        # eventos que el mercado da por imposibles/seguros.
        if price is not None and spread is not None and spread > SPREAD_PENALTY_THRESHOLD:
            if price < EXTREME_PRICE_LOW or price > EXTREME_PRICE_HIGH:
                return 0.0

        # ── Volume 24h: log10 normalizado contra el máximo del batch ──
        vol_24h_score = (
            math.log10(volume_24h + 1) / math.log10(max_volume_24h + 1)
            if max_volume_24h > 1 else 0.0
        )

        # ── Order Density: log10 normalizado del número de órdenes ──
        # Sustituye la liquidez bruta de Gamma. Un mercado con muchas
        # órdenes activas tiene un CLOB sano y activo ("pulso real").
        order_density_score = (
            math.log10(order_count + 1) / math.log10(max_order_count + 1)
            if max_order_count > 1 else 0.0
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
            vol_24h_score        * self.weights["volume_24h"]
            + order_density_score  * self.weights["order_density"]
            + recency_score        * self.weights["recency"]
        )

        # ── Penalización agresiva de spread (multiplicador post-score) ──
        # Spreads > 10% son señal de mercado zombi o ilíquido.
        # Multiplicador 0.01x: hunde el mercado al fondo de la lista.
        # spread puede ser None para mercados en margen de cortesía (aún sin CLOB).
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
            24h), ``liquidity``, ``spread``, ``order_count`` (órdenes CLOB activas)
            y opcionalmente ``created_at`` (timestamp UNIX de creación).

        Returns
        -------
        RankingResult
            Con el Top N, todos los scores, y listas de entradas/salidas.

        Notes
        -----
        Filtro de Pulso (v3.0):
          1. spread N/A → libro vacío → ignorado.
          2. order_count == 0 → CLOB sin órdenes → ignorado.
          3. vol24h == 0 y mercado no es nuevo → sin pulso → score=0.
          4. liquidez < $500 → inoperable → score=0.
          5. precio extremo con spread alto → score=0.
          6. Penalización spread >10% → multiplicador 0.01x.
          7. Ranking FINAL por vol24h DESC (no por score compuesto).
             Esto garantiza que los mercados más activos HOY estén arriba.
        """
        if not snapshots:
            return RankingResult(top=[], all_scored=[], enter=[], exit=[])

        # Normalización: encontrar máximos del batch
        max_vol_24h    = max((s.get("volume_24h", 0) or 0) for s in snapshots)
        max_order_count = max((s.get("order_count", 0) or 0) for s in snapshots)

        # Calcular scores
        scored = []
        for s in snapshots:
            spread_parsed = _parse_spread(s)
            score = self._compute_score(s, max_vol_24h, max_order_count)
            scored.append(MarketScore(
                condition_id=s.get("condition_id", ""),
                question=s.get("question", ""),
                slug=s.get("slug", ""),
                volume=s.get("volume", 0) or 0,
                volume_24h=s.get("volume_24h", 0) or 0,
                liquidity=s.get("liquidity", 0) or 0,
                spread=spread_parsed,
                order_count=s.get("order_count", 0) or 0,
                score=score,
                snapshot=s,
            ))

        # ── Filtro de elegibilidad — doble barrera ────────────────
        # score > 0 ya garantiza que pasaron todos los Hard Gates.
        # Mantenemos el filtro explícito de liquidez como segunda barrera.
        eligible = [
            ms for ms in scored
            if ms.score > 0 and ms.liquidity >= MIN_LIQUIDITY_USD
        ]

        # ── Ranking Final: Pulso = vol24h DESC ───────────────────
        # Una vez filtrados los mercados con vida real, los ordenamos
        # por volumen de las últimas 24h de mayor a menor.
        # Esto pone arriba los mercados donde la gente está peleando
        # por el precio AHORA MISMO, no los históricos con alto volumen total.
        eligible.sort(key=lambda ms: ms.volume_24h, reverse=True)

        # all_scored también ordenado por score (para logs/debugging)
        scored.sort(key=lambda ms: ms.score, reverse=True)

        # Top N
        top = eligible[:self.top_n]
        current_ids = {ms.condition_id for ms in top}

        # Detectar entradas y salidas
        enter_ids = current_ids - self._previous_top
        exit_ids  = self._previous_top - current_ids

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

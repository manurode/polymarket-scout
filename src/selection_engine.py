"""
Selection Engine — Ranking de mercados para el Top 50.

Arquitectura v4.0 — «Filtros Institucionales»:
  1. Radar (Gamma API) escanea ~200 mercados.
  2. Selection Engine aplica filtros institucionales y rankea el Top 50.

Criterios v4.0:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ FILTRO DE RANGO CENTRAL (Target 0.50)                               │
  │   Precio en 0.15–0.85 → multiplicador de recompensa x2.0.          │
  │   Operamos donde el resultado sigue en disputa.                     │
  ├─────────────────────────────────────────────────────────────────────┤
  │ FILTRO DE LIQUIDEZ ACTIVA                                           │
  │   order_count (Top del libro CLOB) < 10 → descartado.              │
  │   Necesitamos «compañeros de juego» en el libro.                   │
  ├─────────────────────────────────────────────────────────────────────┤
  │ EXCLUSIÓN DE MERCADOS DE «COLA LARGA»                               │
  │   precio < 0.02 → descartado (sin spread para MM rentable).        │
  ├─────────────────────────────────────────────────────────────────────┤
  │ DETECCIÓN DE ACTIVIDAD                                              │
  │   Sin movimiento de precio en los últimos 30 min → penalización     │
  │   drástica (multiplicador 0.05x sobre el score final).             │
  └─────────────────────────────────────────────────────────────────────┘

Resto de filtros heredados (Filtro de Pulso v3.1):
  - Hard Gate de Actividad: vol24h == 0 → score=0, excepto mercados nuevos (<48h).
  - Hard limit de liquidez: liquidez < $500 → score=0.
  - Penalización agresiva de spread: spreads > 10% → multiplicador 0.01x.
  - Margen de cortesía: vol24h > $1 000 → pasa Gates de spread/order_count vacíos.

Usage:
    engine = SelectionEngine(top_n=50)
    result = engine.rank(snapshots)
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Pesos del score ───────────────────────────────────────────────────────────
# v4.0 «Filtros Institucionales»: el vol24h sigue dominando.
# La densidad de órdenes sigue aportando, la recency completa el compuesto.

SCORE_WEIGHTS = {
    "volume_24h":    0.70,  # Volumen últimas 24h — señal de actividad real (dominante)
    "order_density": 0.20,  # Densidad de órdenes en el CLOB (pulso del libro)
    "recency":       0.10,  # Tiempo hasta expiración
}

# ── Umbrales globales ─────────────────────────────────────────────────────────

# Spread
SPREAD_PENALTY_THRESHOLD    = 0.10    # spreads > 10% → multiplicador 0.01x
SPREAD_PENALTY_MULTIPLIER   = 0.01    # Factor de penalización AGRESIVO

# Liquidez
MIN_LIQUIDITY_USD           = 500.0   # Liquidez mínima — por debajo: score=0

# Filtro de Cola Larga (Long Tail Exclusion)
LONG_TAIL_PRICE_THRESHOLD   = 0.02    # Precio < 2% → evento «imposible» sin spread

# Filtro de Rango Central (Central Range Reward)
CENTRAL_RANGE_LOW           = 0.15   # Límite inferior del rango central
CENTRAL_RANGE_HIGH          = 0.85   # Límite superior del rango central
CENTRAL_RANGE_MULTIPLIER    = 2.0    # Recompensa x2 para mercados en disputa

# Filtro de Liquidez Activa (Active Liquidity Gate)
MIN_ACTIVE_ORDER_COUNT      = 10     # Mínimo de órdenes Top-of-book CLOB para operar

# Detección de Actividad (Stale Market Penalty)
STALE_MARKET_WINDOW_SEC     = 30 * 60   # 30 minutos sin movimiento → «zombie»
STALE_MARKET_MULTIPLIER     = 0.05      # Penalización drástica si no hay actividad reciente

# Margen de cortesía: vol24h > umbral → puede ignorar Gates de CLOB vacíos
MIN_VOL24H_FOR_SPREAD_GRACE = 1_000.0   # $1 000

# Ventanas de tiempo
EXPIRY_WARN_HOURS           = 24     # Mercados que expiran en < 24h penalizados
NEW_MARKET_WINDOW_HOURS     = 48     # Mercados creados en las últimas N horas son «nuevos»

# Precio extremo alto (complementario al Long Tail)
EXTREME_PRICE_HIGH          = 0.98   # Precio > 98% → evento dado por seguro


# ── Dataclasses de resultado ──────────────────────────────────────────────────

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
    price: Optional[float]
    score: float
    is_central_range: bool      # True si el precio está en el rango 0.15–0.85
    is_stale: bool              # True si no ha habido movimiento en 30 min
    snapshot: dict = field(repr=False)


@dataclass
class RankingResult:
    """Resultado completo del ranking."""
    top: list[MarketScore]
    all_scored: list[MarketScore]
    enter: list[str]   # condition_ids que ENTRAN al Top N
    exit: list[str]    # condition_ids que SALEN del Top N


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_new_market(snapshot: dict) -> bool:
    """Retorna True si el mercado probablemente es nuevo (< NEW_MARKET_WINDOW_HOURS horas).

    Estrategia en dos pasos:
    1. Si ``created_at`` (timestamp UNIX) está disponible, lo usa directamente.
    2. Si no está disponible, usa una heurística:
       volumen total bajo + vol24h == 0 es señal de mercado recién creado.
    """
    created_at = snapshot.get("created_at")
    if created_at is not None and created_at > 0:
        age_hours = (time.time() - float(created_at)) / 3600
        return age_hours <= NEW_MARKET_WINDOW_HOURS

    # Heurística: mercado con volumen total muy bajo y sin vol24h
    volume_total = snapshot.get("volume", 0) or 0
    volume_24h   = snapshot.get("volume_24h", 0) or 0
    return volume_total < 500 and volume_24h == 0


def _parse_spread(snapshot: dict) -> Optional[float]:
    """Extrae el spread del snapshot de forma robusta.

    - None / «N/A» / string vacío → retorna None (libro vacío / sin datos).
    - Numérico → retorna como float.
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


def _extract_price(snapshot: dict) -> Optional[float]:
    """Extrae el precio mid del snapshot (price / price_yes / outcomePrices[0])."""
    for key in ("price", "price_yes"):
        val = snapshot.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _is_stale_market(snapshot: dict) -> bool:
    """Retorna True si el mercado no ha registrado actividad en STALE_MARKET_WINDOW_SEC.

    Campos consultados (en orden de prioridad):
      1. ``last_trade_timestamp`` — timestamp UNIX del último trade.
      2. ``last_updated``         — timestamp UNIX de la última actualización del snapshot.
      3. ``timestamp``            — timestamp del snapshot en sí.

    Si ningún campo está disponible, devuelve False (beneficio de la duda).
    """
    now = time.time()
    for key in ("last_trade_timestamp", "last_updated", "timestamp"):
        val = snapshot.get(key)
        if val is not None:
            try:
                last_ts = float(val)
                if last_ts > 0:
                    return (now - last_ts) > STALE_MARKET_WINDOW_SEC
            except (TypeError, ValueError):
                pass
    return False  # sin datos → no penalizamos


# ── Motor principal ───────────────────────────────────────────────────────────

class SelectionEngine:
    """Rankea mercados por score compuesto con filtros institucionales v4.0.

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

        # Estado: condition_ids en el Top N del ranking anterior
        self._previous_top: set[str] = set()

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_score(
        self,
        snapshot: dict,
        max_volume_24h: float = 1.0,
        max_order_count: float = 1.0,
    ) -> tuple[float, bool, bool]:
        """Calcula el score para un mercado individual.

        Returns
        -------
        tuple[float, bool, bool]
            (score, is_central_range, is_stale)

        Hard Gates — score=0 inmediato:
          G1. Precio < LONG_TAIL_PRICE_THRESHOLD (0.02) → cola larga, sin spread.
          G2. Precio > EXTREME_PRICE_HIGH (0.98) → evento dado por seguro.
          G3. vol24h == 0 y mercado NO es nuevo (<48h) → sin pulso.
          G4. Liquidez < MIN_LIQUIDITY_USD ($500) → inoperable.
          G5. order_count < MIN_ACTIVE_ORDER_COUNT (10) → sin liquidez activa,
              SALVO margen de cortesía (vol24h > $1 000).
          G6. spread == None y sin margen de cortesía → libro no consultado.

        Multiplicadores post-score:
          • Rango Central (0.15–0.85): ×2.0 (recompensa).
          • Spread > 10%: ×0.01 (penalización agresiva).
          • Mercado estancado > 30 min: ×0.05 (penalización drástica).
        """
        # ── Datos base ────────────────────────────────────────────────────────
        volume_24h  = max(0.0, snapshot.get("volume_24h", 0) or 0)
        liquidity   = max(0.0, snapshot.get("liquidity", 0) or 0)
        order_count = max(0,   snapshot.get("order_count", 0) or 0)
        price       = _extract_price(snapshot)
        spread      = _parse_spread(snapshot)

        # ── Señales derivadas ─────────────────────────────────────────────────
        has_vol24h_grace = volume_24h >= MIN_VOL24H_FOR_SPREAD_GRACE

        is_central_range = (
            price is not None
            and CENTRAL_RANGE_LOW <= price <= CENTRAL_RANGE_HIGH
        )
        is_stale = _is_stale_market(snapshot)

        # ════════════════════════════════════════════════════════════════════
        # HARD GATES (orden de mayor a menor severidad)
        # ════════════════════════════════════════════════════════════════════

        # G1 — Exclusión de Cola Larga: precio < 0.02
        #   Mercados con precio casi nulo no tienen spread suficiente para MM.
        if price is not None and price < LONG_TAIL_PRICE_THRESHOLD:
            return 0.0, is_central_range, is_stale

        # G2 — Precio > 98%: evento ya resuelto por el mercado
        #   No hay información ni margen para hacer MM.
        if price is not None and price > EXTREME_PRICE_HIGH:
            return 0.0, is_central_range, is_stale

        # G3 — Hard Gate de Actividad: sin pulso hoy
        #   vol24h == 0 → mercado zombi, a menos que sea recién creado.
        if volume_24h == 0 and not _is_new_market(snapshot):
            return 0.0, is_central_range, is_stale

        # G4 — Liquidez mínima: libro sin profundidad suficiente
        if liquidity < MIN_LIQUIDITY_USD:
            return 0.0, is_central_range, is_stale

        # G5 — Filtro de Liquidez Activa: orden_count < 10
        #   Necesitamos contraparte en el libro para hacer MM.
        #   Margen de cortesía: si aún no tenemos datos del CLOB pero el
        #   mercado tiene vol24h > $1 000, lo dejamos pasar (L2 lo validará).
        if order_count < MIN_ACTIVE_ORDER_COUNT and not has_vol24h_grace:
            return 0.0, is_central_range, is_stale

        # G6 — Spread desconocido: CLOB no consultado aún
        if spread is None and not has_vol24h_grace:
            return 0.0, is_central_range, is_stale

        # ════════════════════════════════════════════════════════════════════
        # SCORE BASE COMPUESTO
        # ════════════════════════════════════════════════════════════════════

        # Volume 24h: log10 normalizado contra el máximo del batch
        vol_24h_score = (
            math.log10(volume_24h + 1) / math.log10(max_volume_24h + 1)
            if max_volume_24h > 1 else 0.0
        )

        # Order Density: log10 normalizado del número de órdenes CLOB activas
        order_density_score = (
            math.log10(order_count + 1) / math.log10(max_order_count + 1)
            if max_order_count > 1 else 0.0
        )

        # Recency: penalización por cercanía a expiración
        recency_score = 1.0
        end_date = snapshot.get("end_date")
        if end_date:
            hours_left = (end_date - time.time()) / 3600
            if hours_left <= 0:
                recency_score = 0.0
            elif hours_left < EXPIRY_WARN_HOURS:
                recency_score = hours_left / EXPIRY_WARN_HOURS

        base_score = (
            vol_24h_score        * self.weights["volume_24h"]
            + order_density_score  * self.weights["order_density"]
            + recency_score        * self.weights["recency"]
        )

        # ════════════════════════════════════════════════════════════════════
        # MULTIPLICADORES POST-SCORE
        # ════════════════════════════════════════════════════════════════════

        # 1. Recompensa de Rango Central (×2.0)
        #    Precio en 0.15–0.85: resultado aún en disputa → máxima prioridad.
        if is_central_range:
            base_score *= CENTRAL_RANGE_MULTIPLIER

        # 2. Penalización agresiva de spread (×0.01)
        #    Spread > 10%: mercado ilíquido o zombi → hundir al fondo.
        if spread is not None and spread > SPREAD_PENALTY_THRESHOLD:
            base_score *= SPREAD_PENALTY_MULTIPLIER

        # 3. Penalización de mercado estancado (×0.05)
        #    Sin actividad en los últimos 30 min → degradar drásticamente.
        if is_stale:
            base_score *= STALE_MARKET_MULTIPLIER

        return base_score, is_central_range, is_stale

    # ── Ranking ───────────────────────────────────────────────────────────────

    def rank(self, snapshots: list[dict]) -> RankingResult:
        """Rankea todos los snapshots y retorna el Top N + eventos de cambio.

        Parameters
        ----------
        snapshots : list[dict]
            Lista de snapshots de mercado (formato scanner). Campos esperados:
            ``volume``, ``volume_24h``, ``liquidity``, ``spread``,
            ``order_count`` (órdenes CLOB activas), ``price`` / ``price_yes``,
            ``last_trade_timestamp`` (opcional, para detección de actividad),
            ``created_at`` (timestamp UNIX de creación).

        Returns
        -------
        RankingResult
            Con el Top N, todos los scores, y listas de entradas/salidas.

        Pipeline v4.0:
          1. Hard Gates institucionales (Long Tail, Actividad, Liquidez Activa…).
          2. Score compuesto: vol24h × 70% + order_density × 20% + recency × 10%.
          3. Multiplicadores: ×2.0 si rango central, ×0.01 si spread alto,
             ×0.05 si mercado estancado > 30 min.
          4. Filtro de elegibilidad: score > 0 AND liquidez ≥ $500.
          5. Ranking final por vol24h DESC (actividad real, no score histórico).
        """
        if not snapshots:
            return RankingResult(top=[], all_scored=[], enter=[], exit=[])

        # Normalización del batch
        max_vol_24h     = max((s.get("volume_24h", 0) or 0) for s in snapshots)
        max_order_count = max((s.get("order_count", 0) or 0) for s in snapshots)

        # Scoring
        scored: list[MarketScore] = []
        for s in snapshots:
            score, is_central, is_stale = self._compute_score(
                s, max_vol_24h, max_order_count
            )
            spread_parsed = _parse_spread(s)
            price         = _extract_price(s)
            scored.append(MarketScore(
                condition_id=s.get("condition_id", ""),
                question=s.get("question", ""),
                slug=s.get("slug", ""),
                volume=s.get("volume", 0) or 0,
                volume_24h=s.get("volume_24h", 0) or 0,
                liquidity=s.get("liquidity", 0) or 0,
                spread=spread_parsed,
                order_count=s.get("order_count", 0) or 0,
                price=price,
                score=score,
                is_central_range=is_central,
                is_stale=is_stale,
                snapshot=s,
            ))

        # ── Filtro de elegibilidad ────────────────────────────────────────────
        # score > 0 garantiza que pasaron todos los Hard Gates.
        # La barrera de liquidez es redundante pero explícita por claridad.
        eligible = [
            ms for ms in scored
            if ms.score > 0 and ms.liquidity >= MIN_LIQUIDITY_USD
        ]

        # ── Ranking Final: Pulso = vol24h DESC ───────────────────────────────
        # Ordenamos por volumen de las últimas 24h (actividad real HOY),
        # no por score compuesto, para evitar que el multiplicador ×2.0
        # eleve artificialmente mercados de bajo volumen con precio central.
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

    # ── Consulta ──────────────────────────────────────────────────────────────

    def is_top(self, condition_id: str) -> bool:
        """Verifica si un condition_id está actualmente en el Top N."""
        return condition_id in self._previous_top

    def get_top_ids(self) -> set[str]:
        """Retorna los condition_ids del Top N actual."""
        return self._previous_top.copy()

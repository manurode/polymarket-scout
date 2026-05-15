"""
Selection Engine — Ranking de mercados con Scoring Multidimensional (Doble Perfil).

Arquitectura v5.0 — «Bifurcación MM vs Direccional»:

  Perfil MM (Market Making):
    Premia: altísimo volumen 24h, alta liquidez L2, precios centrales (0.30–0.70).
    Castiga: resolución en <48h, eventos deportivos (adverse selection por goles).
    → El daemon _market_making_loop opera exclusivamente en estos mercados.

  Perfil Direccional (Momentum / Catalyst):
    Premia: mercados que expiran en ≤7 días, picos recientes de volumen,
            eventos de noticias inminentes.
    Castiga: mercados que expiran en 2025/2026 (líneas planas).
    → El daemon _autonomous_execution_loop vigila exclusivamente estos mercados.

Los castigos del Bandit (Hard Gates, spread, stale) permanecen INTACTOS en ambos
perfiles. La única diferencia es QUÉ mercados se priorizan para cada estrategia.

Usage:
    engine = SelectionEngine(top_n_mm=10, top_n_directional=10)
    result = engine.rank(snapshots)
    # result.mm_top, result.directional_top
"""

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Constantes — Perfil MM
# ═══════════════════════════════════════════════════════════════════════════════

# Pesos del score MM: el volumen 24h es el rey para market making
MM_SCORE_WEIGHTS = {
    "volume_24h":      0.50,   # Volumen últimas 24h — pulso del mercado
    "liquidity":       0.30,   # Liquidez en el book — profundidad para MM
    "order_density":   0.15,   # Densidad de órdenes CLOB activas
    "recency":         0.05,   # Tiempo hasta expiración (penaliza <48h)
}

# Rango central para MM: más estrecho que el general (0.30–0.70)
MM_CENTRAL_LOW  = 0.30
MM_CENTRAL_HIGH = 0.70
MM_CENTRAL_MULTIPLIER = 2.0       # ×2 si el precio está en rango central

# Penalizaciones severas específicas de MM
MM_SPORTS_PENALTY_MULTIPLIER    = 0.01   # ×0.01 si es evento deportivo
MM_IMMINENT_EXPIRY_HOURS        = 48     # <48h para resolución → penalizar
MM_IMMINENT_EXPIRY_MULTIPLIER   = 0.01   # ×0.01 si expira en <48h

# Hard Gates heredados del Bandit (INTACTOS)
SPREAD_PENALTY_THRESHOLD    = 0.10
SPREAD_PENALTY_MULTIPLIER   = 0.01
MIN_LIQUIDITY_USD           = 500.0
LONG_TAIL_PRICE_THRESHOLD   = 0.02
EXTREME_PRICE_HIGH          = 0.98
CENTRAL_RANGE_LOW           = 0.15
CENTRAL_RANGE_HIGH          = 0.85
CENTRAL_RANGE_MULTIPLIER    = 2.0
MIN_ACTIVE_ORDER_COUNT      = 10
STALE_MARKET_WINDOW_SEC     = 30 * 60
STALE_MARKET_MULTIPLIER     = 0.05
MIN_VOL24H_FOR_SPREAD_GRACE = 1_000.0
EXPIRY_WARN_HOURS           = 24
NEW_MARKET_WINDOW_HOURS     = 48

# ── Filtro de Seguridad de Volumen Mínimo ────────────────────────────────────
# Mercados con vol24h por debajo de este umbral son «fantasmas»:
# sin liquidez orgánica real, generan señales falsas y slippage inasumible.
MIN_VOLUME_24H_THRESHOLD    = 5_000.0  # USD — umbral de seguridad global
MM_NICHE_CLOB_DEPTH         = 2_000.0  # USD — excepción MM: si el book CLOB
                                        #   tiene >$2K de profundidad, el MM
                                        #   puede operar aunque vol24h sea bajo
MM_LOW_VOL_PENALTY          = 0.10     # ×0.10 — penalización soft para MM en
                                        #   mercados con vol24h < umbral

# ── v5.1 STOCHASTIC MARKET BLACKLIST ──────────────────────────────────────────
# Mercados cuyo precio depende de tweets/redes sociales en vez de flujos
# macro/financieros/geopolíticos. El MM sólo opera donde la liquidez fluye
# de forma racional, no en mercados de nicho hiper-volátiles.
STOCHASTIC_BLACKLIST_RE = re.compile(
    r'\b(?:tweets|SpaceX)\b|'                # whole-word: "tweets", "SpaceX"
    r'(?<![\w-])post(?![\w-])',              # "post" como palabra aislada —
                                              # excluye "post-election", "post-merger", etc.
                                              # pero captura "will Musk post", "X post", etc.
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Constantes — Perfil Direccional
# ═══════════════════════════════════════════════════════════════════════════════

DIRECTIONAL_SCORE_WEIGHTS = {
    "expiry_acceleration": 0.40,   # Expira pronto → máxima prioridad
    "volume_surge":        0.35,   # Pico de volumen reciente vs media
    "momentum":            0.20,   # Señal direccional (price delta reciente)
    "liquidity":           0.05,   # Liquidez mínima para ejecutar
}

DIRECTIONAL_NEAR_TERM_HOURS      = 7 * 24    # ≤7 días → premio máximo
DIRECTIONAL_FAR_EXPIRY_DAYS      = 30        # >30 días → empieza a penalizar
DIRECTIONAL_FAR_EXPIRY_MULTIPLIER = 0.01     # ×0.01 si expira en >6 meses (2025/2026)
DIRECTIONAL_NEAR_TERM_MULTIPLIER  = 3.0      # ×3.0 si expira en ≤7 días
DIRECTIONAL_MIN_LIQUIDITY         = 200.0     # Liquidez mínima más laxa para direccional
DIRECTIONAL_MIN_VOL24H            = MIN_VOLUME_24H_THRESHOLD  # $5K — umbral de seguridad


# ═══════════════════════════════════════════════════════════════════════════════
# Detección de deportes
# ═══════════════════════════════════════════════════════════════════════════════

SPORTS_TAGS = {
    "sports", "soccer", "football", "nfl", "nba", "mlb", "nhl",
    "cricket", "tennis", "f1", "formula 1", "mma", "ufc", "boxing",
    "rugby", "golf", "basketball", "baseball", "hockey", "volleyball",
    "esports", "olympics",
}

SPORTS_KEYWORDS_RE = re.compile(
    r'\b(football|soccer|basketball|baseball|hockey|tennis|cricket|rugby|golf'
    r'|ufc|mma|boxing|formula\s*1|grand prix|premier league|la\s*liga'
    r'|serie\s*a|bundesliga|champions league|world cup|super bowl'
    r'|nfl|nba|mlb|nhl|mls|epl|ipl|laliga|ligue\s*1|serie a'
    r'|wimbledon|us open|australian open|french open|tour de france'
    r'|olympics|fifa|uefa|ncaa|march madness|playoffs?)\b',
    re.IGNORECASE,
)


def _is_sports_event(snapshot: dict) -> bool:
    """Detecta si un mercado es un evento deportivo.

    Estrategia en dos pasos:
    1. Revisa los tags del snapshot (Gamma API).
    2. Si no hay tags, busca keywords en la pregunta y título del evento.
    """
    tags = snapshot.get("tags", [])
    if tags and isinstance(tags, list):
        for tag in tags:
            tag_lower = str(tag).lower().strip()
            if tag_lower in SPORTS_TAGS:
                return True

    # Fallback: keyword matching en question + event_title
    question = snapshot.get("question", "") or ""
    event_title = snapshot.get("event_title", "") or ""
    combined = f"{question} {event_title}"
    return bool(SPORTS_KEYWORDS_RE.search(combined))


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers (heredados + nuevos)
# ═══════════════════════════════════════════════════════════════════════════════

def _is_new_market(snapshot: dict) -> bool:
    created_at = snapshot.get("created_at")
    if created_at is not None and created_at > 0:
        age_hours = (time.time() - float(created_at)) / 3600
        return age_hours <= NEW_MARKET_WINDOW_HOURS
    volume_total = snapshot.get("volume", 0) or 0
    volume_24h   = snapshot.get("volume_24h", 0) or 0
    return volume_total < 500 and volume_24h == 0


def _parse_spread(snapshot: dict) -> Optional[float]:
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
    for key in ("price", "price_yes", "price_yes"):
        val = snapshot.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _is_stale_market(snapshot: dict) -> bool:
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
    return False


def _hours_until_expiry(snapshot: dict) -> Optional[float]:
    """Horas hasta la fecha de expiración. None si no hay end_date."""
    end_date = snapshot.get("end_date")
    if not end_date or end_date <= 0:
        return None
    return (end_date - time.time()) / 3600


def _volume_surge_factor(snapshot: dict) -> float:
    """Relación vol24h / volumen total como proxy de aceleración reciente."""
    volume_24h = max(0.0, snapshot.get("volume_24h", 0) or 0)
    volume_total = max(1.0, snapshot.get("volume", 0) or 0)
    return volume_24h / volume_total


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════════

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
    is_central_range: bool
    is_stale: bool
    is_sports: bool = False
    hours_to_expiry: Optional[float] = None
    is_low_vol: bool = False            # True si vol24h < MIN_VOLUME_24H_THRESHOLD
    snapshot: dict = field(default_factory=dict, repr=False)


@dataclass
class RankingResult:
    """Resultado completo del ranking (compatible con código existente)."""
    top: list[MarketScore]
    all_scored: list[MarketScore]
    enter: list[str]
    exit: list[str]


@dataclass
class DualRankingResult:
    """Resultado de ranking dual (v5.0): MM + Direccional independientes."""
    mm_top: list[MarketScore]
    mm_all_scored: list[MarketScore]
    mm_enter: list[str]
    mm_exit: list[str]

    directional_top: list[MarketScore]
    directional_all_scored: list[MarketScore]
    directional_enter: list[str]
    directional_exit: list[str]

    # Watchlist para MM niche: mercados con vol24h bajo pero potencial CLOB profundo
    mm_low_vol_watchlist: list[MarketScore] = field(default_factory=list)

    # Compatibilidad con código legacy: .top apunta a mm_top
    @property
    def top(self) -> list[MarketScore]:
        return self.mm_top

    @property
    def all_scored(self) -> list[MarketScore]:
        return self.mm_all_scored

    @property
    def enter(self) -> list[str]:
        return self.mm_enter

    @property
    def exit(self) -> list[str]:
        return self.mm_exit


# ═══════════════════════════════════════════════════════════════════════════════
# Selection Engine v5.0
# ═══════════════════════════════════════════════════════════════════════════════

class SelectionEngine:
    """Rankea mercados con scoring dual: MM + Direccional.

    Parameters
    ----------
    top_n_mm : int
        Nº de mercados en el Top de Market Making (default 10).
    top_n_directional : int
        Nº de mercados en el Top Direccional (default 10).
    """

    def __init__(
        self,
        top_n_mm: int = 10,
        top_n_directional: int = 10,
    ):
        self.top_n_mm = top_n_mm
        self.top_n_directional = top_n_directional

        # Estado anterior para detectar entradas/salidas
        self._previous_mm_top: set[str] = set()
        self._previous_directional_top: set[str] = set()

    # ── Common Hard Gates (Bandit punishments — INTACTOS) ─────────────────────

    def _passes_common_hard_gates(self, snapshot: dict) -> tuple[bool, str]:
        """Hard Gates que aplican a AMBOS perfiles. Retorna (pasa, razón).

        Estos son los castigos del Bandit que se mantienen intactos.
        """
        volume_24h = max(0.0, snapshot.get("volume_24h", 0) or 0)
        liquidity  = max(0.0, snapshot.get("liquidity", 0) or 0)
        order_count = max(0, snapshot.get("order_count", 0) or 0)
        price       = _extract_price(snapshot)
        spread      = _parse_spread(snapshot)
        has_grace   = volume_24h >= MIN_VOL24H_FOR_SPREAD_GRACE

        # G1 — Cola Larga
        if price is not None and price < LONG_TAIL_PRICE_THRESHOLD:
            return False, "long_tail"

        # G2 — Precio extremo
        if price is not None and price > EXTREME_PRICE_HIGH:
            return False, "extreme_price"

        # G3 — Sin pulso (zombi)
        if volume_24h == 0 and not _is_new_market(snapshot):
            return False, "no_pulse"

        # G4 — Liquidez mínima
        if liquidity < MIN_LIQUIDITY_USD:
            return False, "low_liquidity"

        # G5 — Sin liquidez activa (sin margen de cortesía)
        if order_count < MIN_ACTIVE_ORDER_COUNT and not has_grace:
            return False, "low_order_count"

        # G6 — Spread desconocido (sin margen de cortesía)
        if spread is None and not has_grace:
            return False, "unknown_spread"

        # G7 — v5.1 STOCHASTIC MARKET BLACKLIST
        # Ignorar mercados basados en tweets/redes sociales (ej. Elon Musk).
        # El MM opera en flujos macro/financieros/geopolíticos, no en nichos
        # hiper-volátiles donde el precio depende de un solo tweet.
        question = snapshot.get("question", "") or ""
        if STOCHASTIC_BLACKLIST_RE.search(question):
            return False, "stochastic_blacklist"

        return True, "ok"

    # ── Perfil MM: Scoring ────────────────────────────────────────────────────

    def _compute_mm_score(
        self,
        snapshot: dict,
        max_volume_24h: float = 1.0,
        max_liquidity: float = 1.0,
        max_order_count: float = 1.0,
    ) -> tuple[float, bool, bool, bool, Optional[float]]:
        """Calcula el score para el perfil Market Making.

        Returns
        -------
        tuple[float, bool, bool, bool, Optional[float]]
            (score, is_central_range, is_stale, is_sports, hours_to_expiry)
        """
        volume_24h  = max(0.0, snapshot.get("volume_24h", 0) or 0)
        liquidity   = max(0.0, snapshot.get("liquidity", 0) or 0)
        order_count = max(0,   snapshot.get("order_count", 0) or 0)
        price       = _extract_price(snapshot)
        spread      = _parse_spread(snapshot)

        is_central_range = (
            price is not None
            and MM_CENTRAL_LOW <= price <= MM_CENTRAL_HIGH
        )
        is_stale = _is_stale_market(snapshot)
        is_sports = _is_sports_event(snapshot)
        hours_left = _hours_until_expiry(snapshot)

        # ── Hard Gates (Bandit — INTACTOS) ──
        passes, reason = self._passes_common_hard_gates(snapshot)
        if not passes:
            return 0.0, is_central_range, is_stale, is_sports, hours_left

        # ── Score base compuesto ──────────────────────────────────────────
        vol_score = (
            math.log10(volume_24h + 1) / math.log10(max_volume_24h + 1)
            if max_volume_24h > 1 else 0.0
        )

        liq_score = (
            math.log10(liquidity + 1) / math.log10(max_liquidity + 1)
            if max_liquidity > 1 else 0.0
        )

        order_score = (
            math.log10(order_count + 1) / math.log10(max_order_count + 1)
            if max_order_count > 1 else 0.0
        )

        # Recency: caída lineal cuando quedan <24h
        recency_score = 1.0
        if hours_left is not None:
            if hours_left <= 0:
                recency_score = 0.0
            elif hours_left < EXPIRY_WARN_HOURS:
                recency_score = hours_left / EXPIRY_WARN_HOURS

        base_score = (
            vol_score   * MM_SCORE_WEIGHTS["volume_24h"]
            + liq_score   * MM_SCORE_WEIGHTS["liquidity"]
            + order_score * MM_SCORE_WEIGHTS["order_density"]
            + recency_score * MM_SCORE_WEIGHTS["recency"]
        )

        # ── Multiplicadores ───────────────────────────────────────────────
        # 1. Recompensa de Rango Central (0.30–0.70)
        if is_central_range:
            base_score *= MM_CENTRAL_MULTIPLIER

        # 2. Penalización de spread > 10% (Bandit — INTACTO)
        if spread is not None and spread > SPREAD_PENALTY_THRESHOLD:
            base_score *= SPREAD_PENALTY_MULTIPLIER

        # 3. Penalización de mercado estancado (Bandit — INTACTO)
        if is_stale:
            base_score *= STALE_MARKET_MULTIPLIER

        # 4. ⛔ Penalización SEVERA por evento deportivo (adverse selection)
        if is_sports:
            base_score *= MM_SPORTS_PENALTY_MULTIPLIER

        # 5. ⛔ Penalización SEVERA por expiración inminente (<48h)
        if hours_left is not None and 0 < hours_left < MM_IMMINENT_EXPIRY_HOURS:
            base_score *= MM_IMMINENT_EXPIRY_MULTIPLIER

        # 6. ⛔ Filtro de Seguridad: penalización soft por volumen bajo
        #    Mercados con vol24h < $5K son «fantasmas» — el MM solo opera
        #    si el CLOB confirma >$2K de profundidad (vía MM loop).
        if volume_24h < MIN_VOLUME_24H_THRESHOLD:
            base_score *= MM_LOW_VOL_PENALTY  # ×0.10

        return base_score, is_central_range, is_stale, is_sports, hours_left

    # ── Perfil Direccional: Scoring ───────────────────────────────────────────

    def _compute_directional_score(
        self,
        snapshot: dict,
        max_volume_24h: float = 1.0,
        max_volume_surge: float = 1.0,
    ) -> tuple[float, Optional[float], float]:
        """Calcula el score para el perfil Direccional (Momentum/Catalyst).

        Returns
        -------
        tuple[float, Optional[float], float]
            (score, hours_to_expiry, volume_surge)
        """
        volume_24h = max(0.0, snapshot.get("volume_24h", 0) or 0)
        liquidity  = max(0.0, snapshot.get("liquidity", 0) or 0)
        price      = _extract_price(snapshot)
        hours_left = _hours_until_expiry(snapshot)
        vol_surge  = _volume_surge_factor(snapshot)

        # ── Hard Gates específicos del perfil direccional ──
        # Gate D1: Liquidez mínima (más laxa que MM)
        if liquidity < DIRECTIONAL_MIN_LIQUIDITY:
            return 0.0, hours_left, vol_surge

        # Gate D2: Volumen 24h mínimo
        if volume_24h < DIRECTIONAL_MIN_VOL24H:
            logger.debug(
                "[SKIP_DIR] Token=%s | Reason=Low_Volume_24h (Vol24h=$%.0f < $%d) | %s",
                snapshot.get("condition_id", "")[:16],
                volume_24h,
                int(DIRECTIONAL_MIN_VOL24H),
                snapshot.get("question", "")[:50],
            )
            return 0.0, hours_left, vol_surge

        # Gate D3: Precio extremo (sin oportunidad direccional)
        if price is not None and (price < 0.02 or price > 0.98):
            logger.debug(
                "[SKIP_DIR] Token=%s | Reason=Extreme_Price (Price=%.4f) | %s",
                snapshot.get("condition_id", "")[:16],
                price,
                snapshot.get("question", "")[:50],
            )
            return 0.0, hours_left, vol_surge

        # ── Score base compuesto ──────────────────────────────────────────

        # Expiry acceleration: máximo para ≤7 días, decae con el tiempo
        if hours_left is None:
            expiry_score = 0.5  # sin fecha → neutro
        elif hours_left <= 0:
            expiry_score = 0.0  # ya expiró
        elif hours_left <= DIRECTIONAL_NEAR_TERM_HOURS:
            # 0–7 días: score máximo (1.0 a 0.5)
            expiry_score = 1.0 - (hours_left / DIRECTIONAL_NEAR_TERM_HOURS) * 0.5
        elif hours_left <= DIRECTIONAL_FAR_EXPIRY_DAYS * 24:
            # 7–30 días: decaimiento gradual
            extra = hours_left - DIRECTIONAL_NEAR_TERM_HOURS
            max_extra = DIRECTIONAL_FAR_EXPIRY_DAYS * 24 - DIRECTIONAL_NEAR_TERM_HOURS
            expiry_score = 0.5 * (1.0 - extra / max_extra) if max_extra > 0 else 0.0
        else:
            # >30 días o >6 meses: casi nulo
            expiry_score = 0.05

        # Volume surge: ratio vol24h/vol_total
        surge_score = (
            vol_surge / max_volume_surge if max_volume_surge > 0 else 0.0
        )

        # Momentum proxy: si hay price_yes, usamos la distancia desde 0.50
        # Mercados en movimiento tienen precio lejos del centro
        momentum_score = 0.0
        if price is not None:
            momentum_score = abs(price - 0.50) * 2.0  # 0 en 0.50, 1.0 en 0 o 1

        # Liquidez normalizada (solo para asegurar ejecución)
        liq_score = min(1.0, liquidity / 10_000.0)

        base_score = (
            expiry_score   * DIRECTIONAL_SCORE_WEIGHTS["expiry_acceleration"]
            + surge_score    * DIRECTIONAL_SCORE_WEIGHTS["volume_surge"]
            + momentum_score * DIRECTIONAL_SCORE_WEIGHTS["momentum"]
            + liq_score      * DIRECTIONAL_SCORE_WEIGHTS["liquidity"]
        )

        # ── Multiplicadores ───────────────────────────────────────────────

        # Premio por expiración cercana (≤7 días)
        if hours_left is not None and 0 < hours_left <= DIRECTIONAL_NEAR_TERM_HOURS:
            base_score *= DIRECTIONAL_NEAR_TERM_MULTIPLIER

        # ⛔ Castigo SEVERO por expiración lejana (>6 meses → 2025/2026)
        if hours_left is not None and hours_left > 180 * 24:
            base_score *= DIRECTIONAL_FAR_EXPIRY_MULTIPLIER

        return base_score, hours_left, vol_surge

    # ── Ranking ───────────────────────────────────────────────────────────────

    def rank(self, snapshots: list[dict]) -> DualRankingResult:
        """Rankea mercados para ambos perfiles: MM y Direccional.

        Parameters
        ----------
        snapshots : list[dict]
            Lista de snapshots de mercado del scanner.

        Returns
        -------
        DualRankingResult
            Con rankings independientes para MM y Direccional.
        """
        if not snapshots:
            return DualRankingResult(
                mm_top=[], mm_all_scored=[], mm_enter=[], mm_exit=[],
                directional_top=[], directional_all_scored=[],
                directional_enter=[], directional_exit=[],
            )

        # ── Normalización batch ──────────────────────────────────────────
        max_vol_24h     = max((s.get("volume_24h", 0) or 0) for s in snapshots)
        max_order_count = max((s.get("order_count", 0) or 0) for s in snapshots)
        max_liquidity   = max((s.get("liquidity", 0) or 0) for s in snapshots)
        max_vol_surge   = max(_volume_surge_factor(s) for s in snapshots)

        mm_scored: list[MarketScore] = []
        dir_scored: list[MarketScore] = []

        for s in snapshots:
            vol24 = s.get("volume_24h", 0) or 0
            is_low_vol = vol24 < MIN_VOLUME_24H_THRESHOLD

            # ── MM Score ──────────────────────────────────────────────
            mm_score, is_central, is_stale, is_sports, hrs_left = self._compute_mm_score(
                s, max_vol_24h, max_liquidity, max_order_count,
            )
            mm_scored.append(MarketScore(
                condition_id=s.get("condition_id", ""),
                question=s.get("question", ""),
                slug=s.get("slug", ""),
                volume=s.get("volume", 0) or 0,
                volume_24h=vol24,
                liquidity=s.get("liquidity", 0) or 0,
                spread=_parse_spread(s),
                order_count=s.get("order_count", 0) or 0,
                price=_extract_price(s),
                score=mm_score,
                is_central_range=is_central,
                is_stale=is_stale,
                is_sports=is_sports,
                hours_to_expiry=hrs_left,
                is_low_vol=is_low_vol,
                snapshot=s,
            ))

            # ── Direccional Score ──────────────────────────────────────
            dir_score, dir_hrs, dir_surge = self._compute_directional_score(
                s, max_vol_24h, max_vol_surge,
            )
            dir_scored.append(MarketScore(
                condition_id=s.get("condition_id", ""),
                question=s.get("question", ""),
                slug=s.get("slug", ""),
                volume=s.get("volume", 0) or 0,
                volume_24h=vol24,
                liquidity=s.get("liquidity", 0) or 0,
                spread=_parse_spread(s),
                order_count=s.get("order_count", 0) or 0,
                price=_extract_price(s),
                score=dir_score,
                is_central_range=_extract_price(s) is not None and MM_CENTRAL_LOW <= (_extract_price(s) or 0) <= MM_CENTRAL_HIGH,
                is_stale=_is_stale_market(s),
                is_sports=_is_sports_event(s),
                hours_to_expiry=dir_hrs,
                is_low_vol=is_low_vol,
                snapshot=s,
            ))

        # ── Filtro de elegibilidad + ranking ────────────────────────────

        # MM: score > 0 + liquidez mínima, ordenado por vol24h DESC
        mm_eligible = [
            ms for ms in mm_scored
            if ms.score > 0 and ms.liquidity >= MIN_LIQUIDITY_USD
        ]
        mm_eligible.sort(key=lambda ms: ms.volume_24h, reverse=True)
        mm_top = mm_eligible[:self.top_n_mm]

        # Direccional: score > 0, ordenado por score DESC
        dir_eligible = [
            ms for ms in dir_scored
            if ms.score > 0
        ]
        dir_eligible.sort(key=lambda ms: ms.score, reverse=True)
        dir_top = dir_eligible[:self.top_n_directional]

        # all_scored ordenados por score
        mm_scored.sort(key=lambda ms: ms.score, reverse=True)
        dir_scored.sort(key=lambda ms: ms.score, reverse=True)

        # ── Entradas / Salidas ─────────────────────────────────────────
        mm_current_ids = {ms.condition_id for ms in mm_top}
        mm_enter = list(mm_current_ids - self._previous_mm_top)
        mm_exit  = list(self._previous_mm_top - mm_current_ids)
        self._previous_mm_top = mm_current_ids

        dir_current_ids = {ms.condition_id for ms in dir_top}
        dir_enter = list(dir_current_ids - self._previous_directional_top)
        dir_exit  = list(self._previous_directional_top - dir_current_ids)
        self._previous_directional_top = dir_current_ids

        # ── MM Niche Watchlist ────────────────────────────────────────
        # Mercados con vol24h bajo pero que pasaron los demás Hard Gates.
        # El MM loop los revisará contra el CLOB: si depth > $2K, los promueve.
        mm_low_vol_watchlist = [
            ms for ms in mm_scored
            if ms.is_low_vol and ms.score > 0 and ms.liquidity >= MIN_LIQUIDITY_USD
        ]

        return DualRankingResult(
            mm_top=mm_top,
            mm_all_scored=mm_scored,
            mm_enter=mm_enter,
            mm_exit=mm_exit,
            directional_top=dir_top,
            directional_all_scored=dir_scored,
            directional_enter=dir_enter,
            directional_exit=dir_exit,
            mm_low_vol_watchlist=mm_low_vol_watchlist,
        )

    # ── Consultas ─────────────────────────────────────────────────────────────

    def is_top(self, condition_id: str) -> bool:
        """Verifica si un condition_id está en el Top MM."""
        return condition_id in self._previous_mm_top

    def get_top_ids(self) -> set[str]:
        """Retorna los condition_ids del Top MM actual."""
        return self._previous_mm_top.copy()

    def get_mm_top_ids(self) -> set[str]:
        """Retorna los condition_ids del Top MM."""
        return self._previous_mm_top.copy()

    def get_directional_top_ids(self) -> set[str]:
        """Retorna los condition_ids del Top Direccional."""
        return self._previous_directional_top.copy()

"""
Market Making Engine — Captura pasiva de spread con protección adversa.

Implementa el Market Making Líquido (§2.1 del ARCHITECTURE_V2.md):
- Coloca órdenes límite en ambos lados del libro.
- Quote width dinámico ajustado por volatilidad, inventario y time-decay.
- Protección anti-selección adversa (OBI extremo, whale, flash crash, reconciling).
- v2.0: Inventory Skew Pricing, Anti-Toxic Flow Guard, OBI-Linked Spread.
- Fair price: micro-price CLOB L2 ponderado por tamaño, fallback a precio Gamma.

Uso:
    mm = MarketMaker(book_analyzer, time_decay_manager)
    quote = mm.calculate_quote(token_id, condition_id, fair_price, spread, inventory)
    if quote and mm.should_quote(token_id):
        # place bid at quote.bid_price, ask at quote.ask_price
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from src.book_analyzer import BookAnalyzer
from src.time_decay import TimeDecayManager
from src.spoof_detector import SpoofDetector

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

DEFAULT_BASE_MULTIPLIER = 1.0     # constante de calibración
MAX_OBI_FOR_QUOTING = 0.95        # |OBI| > 0.95 → cancelar órdenes (solo sesgo absoluto)
MIN_TOTAL_SIZE_FOR_OBI = 1000.0   # USD mínimos en best bid+ask para considerar OBI válido
FLASH_CRASH_THRESHOLD = 0.05      # 5% en < 30s
FLASH_CRASH_WINDOW = 30           # segundos
REENTRY_DELAY = 30                # segundos para reincorporarse tras pausa

# ── v2.0: Inventory Skew Pricing ──────────────────────────────────
TARGET_INVENTORY = 0.0             # inventario objetivo (neutral)
MAX_INVENTORY_LIMIT = 10000.0      # USD máximos de inventario para skew completo
SKEW_RISK_FACTOR = 0.5             # factor de riesgo del skew (0-1)

# ── v2.0: Anti-Toxic Flow Guard ───────────────────────────────────
TOXIC_FLOW_VOLUME_RATIO = 3.0      # 300% sobre la media → pausa
TOXIC_FLOW_SHORT_WINDOW = 60       # ventana corta: 1 minuto
TOXIC_FLOW_LONG_WINDOW = 1200      # ventana larga: 20 minutos
TOXIC_FLOW_PAUSE_DURATION = 60     # segundos de pausa
TOXIC_FLOW_MIN_VOLUME = 500.0      # USD mínimo para considerar flujo tóxico

# ── v2.0: OBI-Linked Spread ───────────────────────────────────────
OBI_SPREAD_WIDEN_THRESHOLD = 0.8   # |OBI| > 0.8 → ensanchar spread
OBI_SPREAD_WIDEN_FACTOR = 1.5      # multiplicador del spread (50% más ancho)

# ── v2.1: OBI Toxic Flow Evacuation ──────────────────────────────
OBI_EVACUATION_THRESHOLD = 0.85    # |OBI| > 0.85 en contra de la posición → flujo tóxico
OBI_EVACUATION_CYCLES = 3          # ciclos consecutivos necesarios para evacuar

# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class Quote:
    """Una cotización de Market Making (bid + ask)."""
    token_id: str
    condition_id: str
    fair_price: float           # P: precio justo estimado
    spread: float               # S: spread observable del mercado
    bid_price: float            # precio de compra
    ask_price: float            # precio de venta
    quote_width_multiplier: float
    bid_size: float             # tamaño en USD
    ask_size: float
    timestamp: float
    # Metadatos de ajuste
    volatility_scalar: float = 1.0
    inventory_scalar: float = 1.0
    time_decay_scalar: float = 1.0
    obi_scalar: float = 1.0
    # v2.0: Inventory skew
    inventory_skew: float = 0.0
    net_inventory: float = 0.0
    mode: str = "Normal"
    paused: bool = False
    pause_reason: str = ""


@dataclass
class MarketMakerState:
    """Estado interno del Market Maker para un mercado."""
    token_id: str
    condition_id: str
    last_quote_time: float = 0.0
    pause_until: float = 0.0     # timestamp hasta el que está pausado
    pause_reason: str = ""
    inventory_yes: float = 0.0   # inventario en YES (positivo = long YES)
    inventory_no: float = 0.0    # inventario en NO
    # v2.0: Volume tracking for toxic flow detection
    _volume_history: deque = field(default_factory=lambda: deque(maxlen=1500))
    # v2.0: Recovery mode tracking
    recovery_mode: bool = False
    # v2.1: OBI Toxic Flow Evacuation tracking
    _obi_consecutive_against: int = 0  # ciclos seguidos con OBI en contra de la posición
    _last_obi_check: float = 0.0       # timestamp de la última evaluación OBI

    @property
    def net_inventory(self) -> float:
        """Inventario neto: positivo = long YES, negativo = short YES."""
        return self.inventory_yes - self.inventory_no


# ── MarketMaker ────────────────────────────────────────────────────

class MarketMaker:
    """Market Maker líquido con protección anti-selección adversa.

    v2.0: Inventory Skew Pricing, Anti-Toxic Flow Guard, OBI-Linked Spread.

    Parameters
    ----------
    book_analyzer : BookAnalyzer
        Analizador de order books para fair price y OBI.
    time_decay : TimeDecayManager | None
        Gestor de riesgo temporal.
    spoof_detector : SpoofDetector | None
        Detector de spoofing (para OBI extremes).
    base_multiplier : float
        Multiplicador base del quote width.
    max_obi : float
        Umbral de |OBI| que dispara cancelación de órdenes (default 0.95).
        Solo se pausa si el libro está absolutamente sesgado hacia un lado.
    max_inventory : float
        Límite de inventario para skew completo.
    """

    def __init__(
        self,
        book_analyzer: BookAnalyzer,
        time_decay: Optional[TimeDecayManager] = None,
        spoof_detector: Optional[SpoofDetector] = None,
        base_multiplier: float = DEFAULT_BASE_MULTIPLIER,
        max_obi: float = MAX_OBI_FOR_QUOTING,
        max_inventory: float = MAX_INVENTORY_LIMIT,
    ):
        self._books = book_analyzer
        self._time_decay = time_decay or TimeDecayManager()
        self._spoof = spoof_detector
        self.base_multiplier = base_multiplier
        self.max_obi = max_obi
        self.max_inventory = max_inventory

        # Estado por mercado
        self._states: dict[str, MarketMakerState] = {}

    # ── Quote Calculation ─────────────────────────────────────────

    def calculate_quote(
        self,
        token_id: str,
        condition_id: str = "",
        fair_price: Optional[float] = None,
        spread: Optional[float] = None,
        inventory_yes: float = 0.0,
        inventory_no: float = 0.0,
        position_size_kelly: float = 0.0,
        realized_vol_1h: float = 0.0,
        avg_vol: float = 0.01,
        created_at: float = 0.0,
        end_date: float = 0.0,
        now: Optional[float] = None,
        recovery_mode: bool = False,
    ) -> Optional[Quote]:
        """Calcula una cotización (bid + ask) para un mercado.

        v2.0: Aplica Inventory Skew Pricing y OBI-Linked Spread.

        Parameters
        ----------
        token_id : str
            Token del mercado.
        condition_id : str
            Condition ID del mercado.
        fair_price : float | None
            Precio justo estimado. Si es None, se usa el mid del BookAnalyzer.
        spread : float | None
            Spread observable. Si es None, se calcula del BookAnalyzer.
        inventory_yes : float
            Inventario actual en YES (USD). Positivo = long YES.
        inventory_no : float
            Inventario actual en NO (USD).
        position_size_kelly : float
            Tamaño de posición recomendado por Kelly (opcional).
        realized_vol_1h : float
            Volatilidad realizada en la última hora.
        avg_vol : float
            Volatilidad media del mercado.
        created_at : float
            Timestamp de creación del mercado (para time-decay).
        end_date : float
            Timestamp de expiración (para time-decay).
        now : float | None
            Timestamp actual.
        recovery_mode : bool
            Si la estrategia está en RECOVERY (reduce tamaños).

        Returns
        -------
        Quote | None
            None si el mercado está pausado por protección adversa.
        """
        import time as _time
        if now is None:
            now = _time.time()

        # ── 1. Fair Price ─────────────────────────────────────
        if fair_price is None:
            fair_price = self._books.get_mid_price(token_id)
        if fair_price <= 0 or fair_price >= 1.0:
            return None  # precio inválido

        # ── 2. Spread ─────────────────────────────────────────
        if spread is None:
            spread = self._books.get_spread(token_id)
        if spread <= 0:
            spread = 0.02  # default 2%

        # ── 2.5 OBI intensity factor (v2.0) ──────────────────
        obi = self._books.get_obi(token_id, min_total_size=MIN_TOTAL_SIZE_FOR_OBI)
        obi_scalar = 1.0
        if abs(obi) > OBI_SPREAD_WIDEN_THRESHOLD:
            # Spread se ensancha proporcionalmente al exceso sobre el umbral
            excess = (abs(obi) - OBI_SPREAD_WIDEN_THRESHOLD) / (1.0 - OBI_SPREAD_WIDEN_THRESHOLD)
            obi_scalar = 1.0 + excess * (OBI_SPREAD_WIDEN_FACTOR - 1.0)
            obi_scalar = min(2.0, obi_scalar)

        # ── 3. Quote Width Multiplier ─────────────────────────
        qw_mult, vol_scalar, inv_scalar, td_scalar = self._calculate_quote_scalars(
            token_id=token_id,
            inventory_yes=inventory_yes,
            inventory_no=inventory_no,
            realized_vol_1h=realized_vol_1h,
            avg_vol=avg_vol,
            created_at=created_at,
            end_date=end_date,
            now=now,
        )
        qw_mult *= obi_scalar  # OBI spread widening applied here

        # ── 3.5 Inventory Skew (v2.0) ─────────────────────────
        net_inv = inventory_yes - inventory_no
        skew = self._calculate_inventory_skew(net_inv)
        # skew is in price units (dollar amount to shift both bid and ask)
        # When long tokens (net_inv > 0): skew > 0 → bid/ask shift DOWN
        # When short tokens (net_inv < 0): skew < 0 → bid/ask shift UP

        # ── 4. Bid/Ask Prices ─────────────────────────────────
        half_spread = (spread / 2.0) * qw_mult * obi_scalar
        bid_price = max(0.01, fair_price - half_spread - skew)
        ask_price = min(0.99, fair_price + half_spread - skew)

        # ── 5. Position Size ──────────────────────────────────
        if position_size_kelly <= 0:
            position_size_kelly = 100.0  # default
        half_size = position_size_kelly / 2.0

        # ── Recovery mode: reduce size by 80% ──────────────────
        if recovery_mode:
            half_size *= 0.2  # 80% reduction → 20% of normal

        # ── 6. Check pause ────────────────────────────────────
        state = self._get_state(token_id, condition_id)
        paused = now < state.pause_until
        pause_reason = state.pause_reason if paused else ""

        # Determine mode label
        mode = "Recovery" if recovery_mode else "Normal"

        return Quote(
            token_id=token_id,
            condition_id=condition_id,
            fair_price=fair_price,
            spread=spread,
            bid_price=bid_price,
            ask_price=ask_price,
            quote_width_multiplier=qw_mult,
            bid_size=half_size,
            ask_size=half_size,
            timestamp=now,
            volatility_scalar=round(vol_scalar, 3),
            inventory_scalar=round(inv_scalar, 3),
            time_decay_scalar=round(td_scalar, 3),
            obi_scalar=round(obi_scalar, 3),
            inventory_skew=round(skew, 6),
            net_inventory=round(net_inv, 2),
            mode=mode,
            paused=paused,
            pause_reason=pause_reason,
        )

    # ── Inventory Skew (v2.0) ──────────────────────────────────────

    def _calculate_inventory_skew(self, net_inventory: float) -> float:
        """Calcula el skew de inventario para ajustar bid/ask.

        Cuando el bot está cargado de tokens (net_inventory > 0), el skew
        es positivo y desplaza ambos precios hacia ABAJO, incentivando la
        venta y desincentivando la compra.

        Formula:
            skew = (net_inventory / max_inventory_limit) * risk_factor

        Parameters
        ----------
        net_inventory : float
            Inventario neto: positivo = long YES, negativo = short.

        Returns
        -------
        float
            Valor del skew en unidades de precio.
        """
        if self.max_inventory <= 0:
            return 0.0

        ratio = net_inventory / self.max_inventory
        # Clamp to [-1, 1] to prevent extreme skew
        ratio = max(-1.0, min(1.0, ratio))
        skew = ratio * SKEW_RISK_FACTOR
        return skew

    # ── Quote Width Multiplier ────────────────────────────────────

    def _calculate_quote_scalars(
        self,
        token_id: str,
        inventory_yes: float,
        inventory_no: float,
        realized_vol_1h: float,
        avg_vol: float,
        created_at: float,
        end_date: float,
        now: float,
    ) -> tuple[float, float, float, float]:
        """Calcula el multiplicador dinámico del quote width y sus componentes.

        Returns
        -------
        tuple[float, float, float, float]
            (quote_width_multiplier, volatility_scalar, inventory_scalar, time_decay_scalar)

        quote_width_multiplier = base_multiplier
                               × volatility_scalar
                               × inventory_scalar
                               × time_decay_scalar
        """
        # Volatility scalar: más volátil → spreads más anchos
        if avg_vol > 0:
            vol_scalar = 1.0 + (realized_vol_1h / avg_vol - 1.0) * 0.5
        else:
            vol_scalar = 1.0
        vol_scalar = max(0.8, min(2.0, vol_scalar))

        # Inventory scalar: si estamos long YES → spread más ancho en YES
        total_inv = abs(inventory_yes) + abs(inventory_no)
        if total_inv > 0:
            inv_imbalance = (inventory_yes - inventory_no) / total_inv
            inv_scalar = 1.0 + abs(inv_imbalance) * 0.5
        else:
            inv_scalar = 1.0
        inv_scalar = max(0.5, min(2.0, inv_scalar))

        # Time-decay scalar
        if created_at > 0 and end_date > 0:
            td_scalar = self._time_decay.get_time_decay_scalar(
                created_at, end_date, now,
            )
        else:
            td_scalar = 1.0

        qw_mult = self.base_multiplier * vol_scalar * inv_scalar * td_scalar
        return qw_mult, vol_scalar, inv_scalar, td_scalar

    # ── Legacy alias (backward compatibility) ──────────────────

    def _calculate_quote_width_multiplier(self, **kwargs) -> float:
        """Legacy wrapper — returns just the multiplier."""
        result, _, _, _ = self._calculate_quote_scalars(**kwargs)
        return result

    # ── Adverse Selection Protection ──────────────────────────────

    def should_quote(
        self,
        token_id: str,
        condition_id: str = "",
        whale_detected: bool = False,
        flash_crash: bool = False,
        book_reconciling: bool = False,
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Determina si es seguro cotizar en este mercado.

        Returns
        -------
        tuple[bool, str]
            (debería_cotizar, razón_si_no)
        """
        import time as _time
        if now is None:
            now = _time.time()

        state = self._get_state(token_id, condition_id)

        # 1. Pausa activa
        if now < state.pause_until:
            remaining = state.pause_until - now
            return False, f"paused: {state.pause_reason} ({remaining:.0f}s remaining)"

        # 2. Book en RECONCILING
        if book_reconciling:
            self._pause(token_id, condition_id, REENTRY_DELAY, "book_reconciling", now)
            return False, "book in RECONCILING state"

        # 3. OBI extremo (con filtro anti-"calderilla")
        obi = self._books.get_obi(token_id, min_total_size=MIN_TOTAL_SIZE_FOR_OBI)
        if abs(obi) > self.max_obi:
            self._pause(token_id, condition_id, REENTRY_DELAY, f"extreme_obi={obi:.2f}", now)
            return False, f"extreme OBI: {obi:.2f}"

        # 4. Whale detectado
        if whale_detected:
            self._pause(token_id, condition_id, REENTRY_DELAY, "whale_detected", now)
            return False, "whale activity detected"

        # 5. Flash crash/spike
        if flash_crash:
            self._pause(token_id, condition_id, REENTRY_DELAY, "flash_crash", now)
            return False, "flash crash/spike detected"

        # 6. Toxic flow check (v2.0)
        is_toxic, toxic_reason = self.check_toxic_flow(token_id, now)
        if is_toxic:
            self._pause(token_id, condition_id, TOXIC_FLOW_PAUSE_DURATION, toxic_reason, now)
            return False, toxic_reason

        return True, "ok"

    # ── Anti-Toxic Flow Guard (v2.0) ─────────────────────────────

    def record_volume(
        self,
        token_id: str,
        volume: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Registra volumen de trading para un mercado (ventana deslizante).

        Llamar desde el orchestrator cada vez que se procesa un trade.

        Parameters
        ----------
        token_id : str
            Token del mercado.
        volume : float
            Volumen del trade en USD.
        timestamp : float | None
            Timestamp del trade.
        """
        import time as _time
        if timestamp is None:
            timestamp = _time.time()
        state = self._get_state(token_id)
        state._volume_history.append((timestamp, volume))

    def check_toxic_flow(
        self,
        token_id: str,
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Detecta flujo tóxico por explosión de volumen.

        Si el volumen en 1 minuto supera en 300% la media de los últimos
        20 minutos, se considera flujo tóxico.

        Parameters
        ----------
        token_id : str
            Token del mercado.
        now : float | None
            Timestamp actual.

        Returns
        -------
        tuple[bool, str]
            (es_tóxico, razón)
        """
        import time as _time
        if now is None:
            now = _time.time()

        state = self._states.get(token_id)
        if state is None or not state._volume_history:
            return False, ""

        short_cutoff = now - TOXIC_FLOW_SHORT_WINDOW
        long_cutoff = now - TOXIC_FLOW_LONG_WINDOW

        # Volumen en la ventana corta (1 min)
        short_vol = sum(
            v for ts, v in state._volume_history if ts >= short_cutoff
        )

        # Volumen en la ventana larga (20 min)
        long_vol = sum(
            v for ts, v in state._volume_history if ts >= long_cutoff
        )

        # Si no hay suficiente historial largo, no evaluar
        if long_vol < TOXIC_FLOW_MIN_VOLUME:
            return False, ""

        # Media por minuto en la ventana larga (20 min)
        long_minutes = TOXIC_FLOW_LONG_WINDOW / 60.0
        avg_per_minute = long_vol / long_minutes

        if avg_per_minute <= 0:
            return False, ""

        # Ratio del volumen corto vs media por minuto
        ratio = short_vol / avg_per_minute

        if ratio > TOXIC_FLOW_VOLUME_RATIO:
            return True, f"toxic_flow: 1m_vol=${short_vol:.0f} vs avg=${avg_per_minute:.0f}/min (ratio={ratio:.1f}x)"

        return False, ""

    # ── OBI Toxic Flow Evacuation (v2.1) ───────────────────────────

    def evaluate_obi_evacuation(
        self,
        token_id: str,
        side: str,
        condition_id: str = "",
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Evalúa si el Order Book Imbalance indica flujo tóxico contra la posición.

        Lógica: si tenemos una posición abierta y el OBI del L2 se inclina
        > OBI_EVACUATION_THRESHOLD en contra de nuestra posición durante
        más de OBI_EVACUATION_CYCLES ciclos consecutivos, alguien tiene
        información que nosotros no → evacuar inmediatamente a mercado.

        Parameters
        ----------
        token_id : str
            Token del mercado.
        side : str
            "YES" (long YES) o "NO" (long NO).
        condition_id : str
            Condition ID (opcional).
        now : float | None
            Timestamp actual.

        Returns
        -------
        tuple[bool, str]
            (debe_evacuar, razón)
        """
        import time as _time
        if now is None:
            now = _time.time()

        # Obtener OBI actual del BookAnalyzer
        obi = self._books.get_obi(token_id, min_total_size=MIN_TOTAL_SIZE_FOR_OBI)
        if abs(obi) <= OBI_EVACUATION_THRESHOLD:
            # OBI dentro de rango normal → resetear contador
            self._reset_obi_counter(token_id, condition_id)
            return False, ""

        state = self._get_state(token_id, condition_id)

        # Determinar si el OBI está en contra de la posición
        obi_against = False
        if side == "YES":
            # Long YES: OBI negativo = presión vendedora = EN CONTRA
            obi_against = obi < -OBI_EVACUATION_THRESHOLD
        elif side == "NO":
            # Long NO: OBI positivo = presión compradora de YES = NO baja = EN CONTRA
            obi_against = obi > OBI_EVACUATION_THRESHOLD

        if not obi_against:
            # OBI extremo pero a FAVOR de la posición → resetear contador
            self._reset_obi_counter(token_id, condition_id)
            return False, ""

        # OBI en contra → incrementar contador
        state._obi_consecutive_against += 1
        state._last_obi_check = now

        logger.warning(
            "OBI TOXIC WATCH ⚠️  %s side=%s obi=%.4f ciclos=%d/%d",
            token_id[:16], side, obi,
            state._obi_consecutive_against, OBI_EVACUATION_CYCLES,
        )

        if state._obi_consecutive_against >= OBI_EVACUATION_CYCLES:
            reason = (
                f"obi_evacuation: obi={obi:.3f} against {side} "
                f"for {state._obi_consecutive_against} consecutive cycles"
            )
            logger.error(
                "🚨 OBI EVACUATION TRIGGERED %s | side=%s obi=%.4f ciclos=%d — "
                "EMERGENCY DUMP a mercado",
                token_id[:16], side, obi, state._obi_consecutive_against,
            )
            # Resetear contador tras evacuar
            self._reset_obi_counter(token_id, condition_id)
            return True, reason

        return False, ""

    def _reset_obi_counter(self, token_id: str, condition_id: str = "") -> None:
        """Resetea el contador de OBI consecutivo en contra."""
        state = self._states.get(condition_id or token_id)
        if state and state._obi_consecutive_against > 0:
            state._obi_consecutive_against = 0

    # ── Flash Crash Detection ───────────────────────────────────────

    def detect_flash_crash(
        self,
        token_id: str,
        current_price: float,
        prices_last_30s: list[float],
    ) -> bool:
        """Detecta si hubo un flash crash (>5% en <30s).

        Parameters
        ----------
        token_id : str
        current_price : float
            Precio actual.
        prices_last_30s : list[float]
            Precios en los últimos 30 segundos.

        Returns
        -------
        bool
            True si se detecta flash crash.
        """
        if not prices_last_30s:
            return False

        # Máximo cambio absoluto en la ventana
        max_price = max(prices_last_30s)
        min_price = min(prices_last_30s)
        price_range = max_price - min_price

        # Cambio relativo al precio actual
        if current_price > 0:
            change_pct = price_range / current_price
            return change_pct > FLASH_CRASH_THRESHOLD

        return False

    # ── Fair Price Resolution ─────────────────────────────────────

    def get_fair_price(
        self,
        token_id: str,
        gamma_price: Optional[float] = None,
        max_clob_spread: float = 0.05,
    ) -> float:
        """Resuelve el fair price para un mercado.

        Jerarquía:
        1. Midpoint del CLOB L2 (vía BookAnalyzer)
        2. Precio Gamma (fallback)
        3. 0.50 (default absoluto)

        Si el spread CLOB > max_clob_spread, se usa Gamma.
        """
        # Intentar CLOB L2
        mid = self._books.get_mid_price(token_id)
        spread = self._books.get_spread(token_id)

        if mid > 0 and spread <= max_clob_spread:
            return mid

        # Fallback Gamma
        if gamma_price is not None and 0 < gamma_price < 1:
            return gamma_price

        # Default
        return 0.50

    # ── State Management ──────────────────────────────────────────

    def _get_state(self, token_id: str, condition_id: str = "") -> MarketMakerState:
        """Obtiene o crea el estado de un mercado."""
        key = condition_id or token_id
        if key not in self._states:
            self._states[key] = MarketMakerState(
                token_id=token_id,
                condition_id=condition_id,
            )
        return self._states[key]

    def _pause(
        self,
        token_id: str,
        condition_id: str,
        duration: float,
        reason: str,
        now: float,
    ) -> None:
        """Pausa el market making para un mercado."""
        key = condition_id or token_id
        state = self._get_state(token_id, condition_id)
        state.pause_until = now + duration
        state.pause_reason = reason
        logger.info("Market Making PAUSADO para %s: %s (%.0fs)", key, reason, duration)

    def update_inventory(
        self,
        token_id: str,
        condition_id: str = "",
        inventory_yes: float = 0.0,
        inventory_no: float = 0.0,
    ) -> None:
        """Actualiza el inventario conocido para un mercado."""
        state = self._get_state(token_id, condition_id)
        state.inventory_yes = inventory_yes
        state.inventory_no = inventory_no

    def is_paused(self, token_id: str, condition_id: str = "") -> bool:
        """Verifica si el mercado está pausado."""
        state = self._states.get(condition_id or token_id)
        if state is None:
            return False
        import time as _time
        return _time.time() < state.pause_until

    def get_state(self, token_id: str, condition_id: str = "") -> Optional[MarketMakerState]:
        """Retorna el estado completo de un mercado."""
        return self._states.get(condition_id or token_id)

    def remove_market(self, token_id: str, condition_id: str = "") -> None:
        """Elimina el estado de un mercado."""
        key = condition_id or token_id
        self._states.pop(key, None)

"""
Market Making Engine — Captura pasiva de spread con protección adversa.

Implementa el Market Making Líquido (§2.1 del ARCHITECTURE_V2.md):
- Coloca órdenes límite en ambos lados del libro.
- Quote width dinámico ajustado por volatilidad, inventario y time-decay.
- Protección anti-selección adversa (OBI extremo, whale, flash crash, reconciling).
- Fair price: midpoint del CLOB L2, fallback a precio Gamma.

Uso:
    mm = MarketMaker(book_analyzer, time_decay_manager)
    quote = mm.calculate_quote(token_id, condition_id, fair_price, spread, inventory)
    if quote and mm.should_quote(token_id):
        # place bid at quote.bid_price, ask at quote.ask_price
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.book_analyzer import BookAnalyzer
from src.time_decay import TimeDecayManager
from src.spoof_detector import SpoofDetector

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

DEFAULT_BASE_MULTIPLIER = 1.0     # constante de calibración
MAX_OBI_FOR_QUOTING = 0.70        # |OBI| > 0.70 → cancelar órdenes
FLASH_CRASH_THRESHOLD = 0.05      # 5% en < 30s
FLASH_CRASH_WINDOW = 30           # segundos
REENTRY_DELAY = 30                # segundos para reincorporarse tras pausa


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


# ── MarketMaker ────────────────────────────────────────────────────

class MarketMaker:
    """Market Maker líquido con protección anti-selección adversa.

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
        Umbral de |OBI| que dispara cancelación de órdenes.
    """

    def __init__(
        self,
        book_analyzer: BookAnalyzer,
        time_decay: Optional[TimeDecayManager] = None,
        spoof_detector: Optional[SpoofDetector] = None,
        base_multiplier: float = DEFAULT_BASE_MULTIPLIER,
        max_obi: float = MAX_OBI_FOR_QUOTING,
    ):
        self._books = book_analyzer
        self._time_decay = time_decay or TimeDecayManager()
        self._spoof = spoof_detector
        self.base_multiplier = base_multiplier
        self.max_obi = max_obi

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
    ) -> Optional[Quote]:
        """Calcula una cotización (bid + ask) para un mercado.

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

        # ── 3. Quote Width Multiplier ─────────────────────────
        qw_mult = self._calculate_quote_width_multiplier(
            token_id=token_id,
            inventory_yes=inventory_yes,
            inventory_no=inventory_no,
            realized_vol_1h=realized_vol_1h,
            avg_vol=avg_vol,
            created_at=created_at,
            end_date=end_date,
            now=now,
        )

        # ── 4. Bid/Ask Prices ─────────────────────────────────
        half_spread = (spread / 2.0) * qw_mult
        bid_price = max(0.01, fair_price - half_spread)
        ask_price = min(0.99, fair_price + half_spread)

        # ── 5. Position Size ──────────────────────────────────
        if position_size_kelly <= 0:
            position_size_kelly = 100.0  # default
        half_size = position_size_kelly / 2.0

        # ── 6. Check pause ────────────────────────────────────
        state = self._get_state(token_id, condition_id)
        paused = now < state.pause_until
        pause_reason = state.pause_reason if paused else ""

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
            paused=paused,
            pause_reason=pause_reason,
        )

    # ── Quote Width Multiplier ────────────────────────────────────

    def _calculate_quote_width_multiplier(
        self,
        token_id: str,
        inventory_yes: float,
        inventory_no: float,
        realized_vol_1h: float,
        avg_vol: float,
        created_at: float,
        end_date: float,
        now: float,
    ) -> float:
        """Calcula el multiplicador dinámico del quote width.

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

        return self.base_multiplier * vol_scalar * inv_scalar * td_scalar

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

        # 3. OBI extremo
        obi = self._books.get_obi(token_id)
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

        return True, "ok"

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

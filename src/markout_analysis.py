"""
Markout Analysis — Detección de Flujo Tóxico en Market Making.

Cuando una ballena compra agresivamente sabiendo algo que el mercado aún no
ha descontado, el Market Maker captura el spread ($2) pero acumula una posición
que pierde $50 en segundos. Este módulo detecta ese patrón trackeando el P&L
de las órdenes pasivas en intervalos fijos post-ejecución.

Uso:
    ma = MarkoutAnalyzer(book_analyzer)
    ma.record_fill(trade_id, token_id, price, size, side)
    toxicity = ma.get_toxicity(token_id)
    if toxicity >= 1.5:
        print("Flujo ALTAMENTE tóxico — pausar Market Making")
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

# Intervalos de markout (segundos después de la ejecución)
MARKOUT_INTERVALS = [1, 5, 10, 60]

# Umbrales de toxicidad
TOXICITY_CLEAN = 0.3        # < 0.3: flujo limpio
TOXICITY_MIXED = 0.7        # 0.3-0.7: flujo mixto
TOXICITY_TOXIC = 1.5        # 0.7-1.5: flujo tóxico
# > 1.5: altamente tóxico

# Ventana de cálculo
MIN_TRADES_FOR_ANALYSIS = 5
MAX_TRADES_WINDOW = 50      # máximo de trades a considerar (o última hora)
MAX_AGE_SECONDS = 3600      # 1 hora máxima


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class FillRecord:
    """Un fill de orden pasiva (nuestra orden fue ejecutada)."""
    trade_id: str
    token_id: str
    fill_price: float      # precio al que se ejecutó
    size: float            # tamaño en USD
    side: str              # "buy" (nos compraron YES) o "sell" (nos vendieron YES)
    fill_time: float       # timestamp de la ejecución
    # Markout prices (se actualizan con el tiempo)
    markout_prices: dict[str, float] = field(default_factory=dict)
    # P&L en cada intervalo
    markout_pnl: dict[str, float] = field(default_factory=dict)


@dataclass
class MarkoutScore:
    """Resultado del análisis de markout para un mercado."""
    token_id: str
    ms_short: float        # P&L medio en t+1s / spread capturado
    ms_medium: float       # P&L medio en t+5s / spread capturado
    ms_long: float         # P&L medio en t+10s / spread capturado
    markout_toxicity: float  # Score de toxicidad [0, ∞)
    classification: str    # "clean", "mixed", "toxic", "highly_toxic"
    trades_analyzed: int
    timestamp: float


class MarkoutAnalyzer:
    """Analizador de markout para detectar flujo tóxico.

    Parameters
    ----------
    book_analyzer : BookAnalyzer | None
        Analizador de order book para obtener mid prices actuales.
    max_trades : int
        Máximo de fills a trackear por mercado (default 50).
    max_age : float
        Edad máxima de un fill en segundos (default 3600).
    """

    def __init__(
        self,
        book_analyzer=None,
        max_trades: int = MAX_TRADES_WINDOW,
        max_age: float = MAX_AGE_SECONDS,
    ):
        self._book_analyzer = book_analyzer
        self.max_trades = max_trades
        self.max_age = max_age

        # Almacenamiento: token_id → lista de fills
        self._fills: dict[str, list[FillRecord]] = defaultdict(list)

    # ── Fill Recording ────────────────────────────────────────────

    def record_fill(
        self,
        trade_id: str,
        token_id: str,
        fill_price: float,
        size: float,
        side: str,
        fill_time: Optional[float] = None,
    ) -> FillRecord:
        """Registra una ejecución de nuestra orden pasiva.

        Parameters
        ----------
        trade_id : str
            Identificador único del trade.
        token_id : str
            Token del mercado.
        fill_price : float
            Precio de ejecución.
        size : float
            Tamaño en USD (nocional).
        side : str
            "buy" si nos compraron YES (nosotros éramos el ask),
            "sell" si nos vendieron YES (nosotros éramos el bid).
        fill_time : float | None
            Timestamp de ejecución (default: time.time()).

        Returns
        -------
        FillRecord
        """
        if fill_time is None:
            fill_time = time.time()

        record = FillRecord(
            trade_id=trade_id,
            token_id=token_id,
            fill_price=fill_price,
            size=size,
            side=side,
            fill_time=fill_time,
        )

        # Inicializar markout prices con el precio de fill
        current_mid = self._get_current_mid(token_id) or fill_price
        for interval in MARKOUT_INTERVALS:
            record.markout_prices[str(interval)] = current_mid
            record.markout_pnl[str(interval)] = self._compute_pnl(
                record, current_mid, interval,
            )

        fills = self._fills[token_id]
        fills.append(record)

        # Buffer circular
        if len(fills) > self.max_trades:
            self._fills[token_id] = fills[-self.max_trades:]

        return record

    # ── Markout Update (llamar periódicamente) ────────────────────

    def update_markouts(self, token_id: Optional[str] = None) -> int:
        """Actualiza los precios de markout para todos los fills activos.

        Debe llamarse periódicamente (cada 1-10 segundos) para actualizar
        los P&L mark-to-market de los fills.

        Parameters
        ----------
        token_id : str | None
            Si se especifica, solo actualiza ese mercado.

        Returns
        -------
        int
            Número de fills actualizados.
        """
        now = time.time()
        updated = 0

        tokens = [token_id] if token_id else list(self._fills.keys())

        for tid in tokens:
            mid = self._get_current_mid(tid)
            if mid is None:
                continue

            for fill in self._fills.get(tid, []):
                age = now - fill.fill_time

                # Actualizar P&L para cada intervalo de markout
                for interval in MARKOUT_INTERVALS:
                    key = str(interval)
                    if age >= interval:
                        fill.markout_prices[key] = mid
                        fill.markout_pnl[key] = self._compute_pnl(
                            fill, mid, interval,
                        )
                updated += 1

        return updated

    # ── Toxicity Calculation ──────────────────────────────────────

    def get_toxicity(self, token_id: str) -> MarkoutScore:
        """Calcula el Markout Toxicity Score para un mercado.

        Returns
        -------
        MarkoutScore
        """
        fills = self._get_recent_fills(token_id)
        now = time.time()

        if len(fills) < MIN_TRADES_FOR_ANALYSIS:
            return MarkoutScore(
                token_id=token_id,
                ms_short=0.0,
                ms_medium=0.0,
                ms_long=0.0,
                markout_toxicity=0.0,
                classification="clean",
                trades_analyzed=len(fills),
                timestamp=now,
            )

        # Calcular P&L medio para cada intervalo
        ms_values = {}
        avg_spread = 0.0

        for interval in MARKOUT_INTERVALS:
            key = str(interval)
            pnls = [f.markout_pnl.get(key, 0.0) for f in fills]
            avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0

            # El spread capturado es positivo (ganancia inmediata)
            # Si el P&L se vuelve negativo, es flujo tóxico
            avg_spread_captured = abs(avg_pnl) if avg_pnl > 0 else 0.01
            ms_values[interval] = avg_pnl / avg_spread_captured if avg_spread_captured > 0 else 0.0

        ms_short = ms_values.get(1, 0.0)
        ms_medium = ms_values.get(5, 0.0)
        ms_long = ms_values.get(10, 0.0)

        # Markout Toxicity = -1 × min(0, MS_short, MS_medium, MS_long)
        # Solo positivo si ALGÚN intervalo tiene P&L negativo
        min_ms = min(ms_short, ms_medium, ms_long)
        toxicity = -1.0 * min(0.0, min_ms)

        classification = self._classify_toxicity(toxicity)

        return MarkoutScore(
            token_id=token_id,
            ms_short=ms_short,
            ms_medium=ms_medium,
            ms_long=ms_long,
            markout_toxicity=toxicity,
            classification=classification,
            trades_analyzed=len(fills),
            timestamp=now,
        )

    # ── Action Recommendations ─────────────────────────────────────

    def get_recommended_response(self, token_id: str) -> dict:
        """Determina la acción recomendada según el nivel de toxicidad.

        Returns
        -------
        dict con:
            - action: "normal" | "widen_spread" | "aggressive_widen" | "pause"
            - spread_multiplier: float
            - position_size_multiplier: float
            - pause_minutes: int (0 si no pausa)
        """
        score = self.get_toxicity(token_id)
        t = score.markout_toxicity

        if t < TOXICITY_CLEAN:
            return {
                "action": "normal",
                "spread_multiplier": 1.0,
                "position_size_multiplier": 1.0,
                "pause_minutes": 0,
                "description": "Flujo limpio — operar normal",
            }
        elif t < TOXICITY_MIXED:
            return {
                "action": "widen_spread",
                "spread_multiplier": 1.5,   # +50% en el lado afectado
                "position_size_multiplier": 0.75,
                "pause_minutes": 0,
                "description": "Flujo mixto — ampliar spread asimétrico, reducir size 25%",
            }
        elif t < TOXICITY_TOXIC:
            return {
                "action": "aggressive_widen",
                "spread_multiplier": 3.0,  # 300% en el lado afectado
                "position_size_multiplier": 0.50,
                "pause_minutes": 0,
                "description": "Flujo tóxico — spread 300%, size 50%, alertar operador",
            }
        else:
            return {
                "action": "pause",
                "spread_multiplier": 1.0,
                "position_size_multiplier": 0.0,
                "pause_minutes": 30,
                "description": "Altamente tóxico — pausar Market Making 30 min",
            }

    # ── Internal ──────────────────────────────────────────────────

    def _get_recent_fills(self, token_id: str) -> list[FillRecord]:
        """Obtiene fills recientes (últimos max_trades o última hora)."""
        all_fills = self._fills.get(token_id, [])
        if not all_fills:
            return []

        now = time.time()
        cutoff = now - self.max_age

        # Filtrar por edad
        recent = [f for f in all_fills if f.fill_time > cutoff]

        # Tomar los últimos max_trades
        return recent[-self.max_trades:]

    def _get_current_mid(self, token_id: str) -> Optional[float]:
        """Obtiene el mid price actual del BookAnalyzer."""
        if self._book_analyzer is None:
            return None
        return self._book_analyzer.get_mid_price(token_id) or None

    @staticmethod
    def _compute_pnl(fill: FillRecord, current_mid: float, interval: int) -> float:
        """Calcula el P&L de un fill relativo al precio actual.

        Para market making en mercados binarios:
        - Si nos compraron YES (side="buy" del taker, nosotros éramos el ask):
          Nuestra posición: short YES → si el precio SUBE, perdemos.
          P&L = fill_price - current_mid (positivo si el precio bajó después)

        - Si nos vendieron YES (side="sell" del taker, nosotros éramos el bid):
          Nuestra posición: long YES → si el precio BAJA, perdemos.
          P&L = current_mid - fill_price (positivo si el precio subió después)
        """
        if fill.side == "buy":
            # Éramos el ask, short YES → ganamos si el precio baja
            return fill.fill_price - current_mid
        else:
            # Éramos el bid, long YES → ganamos si el precio sube
            return current_mid - fill.fill_price

    @staticmethod
    def _classify_toxicity(toxicity: float) -> str:
        """Clasifica el nivel de toxicidad."""
        if toxicity < TOXICITY_CLEAN:
            return "clean"
        elif toxicity < TOXICITY_MIXED:
            return "mixed"
        elif toxicity < TOXICITY_TOXIC:
            return "toxic"
        else:
            return "highly_toxic"

    # ── Maintenance ───────────────────────────────────────────────

    def purge_old_fills(self, token_id: Optional[str] = None) -> int:
        """Elimina fills más antiguos que max_age."""
        now = time.time()
        cutoff = now - self.max_age
        purged = 0

        tokens = [token_id] if token_id else list(self._fills.keys())
        for tid in tokens:
            old_count = len(self._fills[tid])
            self._fills[tid] = [f for f in self._fills.get(tid, []) if f.fill_time > cutoff]
            purged += old_count - len(self._fills[tid])

        return purged

    def remove_market(self, token_id: str) -> None:
        """Elimina todos los fills de un mercado."""
        self._fills.pop(token_id, None)

    def clear(self) -> None:
        """Limpia todos los datos."""
        self._fills.clear()

"""
TradeAggregator — Agregación de ejecuciones y cálculo de TFI.

Procesa trades recibidos del WebSocket del CLOB y calcula Trade Flow
Imbalance (TFI) en buckets temporales de 30s, 1m y 5m.

Uso:
    agg = TradeAggregator()
    agg.add_trade(condition_id, trade)
    tfi_30s = agg.get_tfi(condition_id, "30s")
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Un trade individual procesado."""
    timestamp: float
    price: float
    size: float
    side: str  # "buy" o "sell" (YES comprado o vendido)
    condition_id: str = ""
    trade_id: str = ""


@dataclass
class TFIResult:
    """Resultado del cálculo de TFI para un mercado."""
    condition_id: str
    tfi: float           # Trade Flow Imbalance en [-1, +1]
    buy_volume: float    # volumen comprador en el período
    sell_volume: float   # volumen vendedor en el período
    trade_count: int     # número de trades en el período
    window_seconds: float  # duración real de la ventana


@dataclass
class _TradeBucket:
    """Bucket temporal de trades para un mercado."""
    trades: list[TradeRecord] = field(default_factory=list)
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    def add(self, trade: TradeRecord) -> None:
        self.trades.append(trade)
        if trade.side == "buy":
            self.buy_volume += trade.size
        else:
            self.sell_volume += trade.size

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def tfi(self) -> float:
        """Trade Flow Imbalance: (buy - sell) / (buy + sell)."""
        total = self.buy_volume + self.sell_volume
        if total <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    @property
    def count(self) -> int:
        return len(self.trades)


# ── TradeAggregator ────────────────────────────────────────────────

class TradeAggregator:
    """Agrega trades en buckets temporales y calcula TFI.

    Parameters
    ----------
    windows : tuple[float, ...]
        Tamaños de ventana en segundos (default: 30s, 1m, 5m).
    max_trades_per_market : int
        Máximo de trades a almacenar por mercado (buffer circular).
    """

    DEFAULT_WINDOWS = (30, 60, 300)  # 30s, 1m, 5m

    def __init__(
        self,
        windows: tuple[float, ...] = DEFAULT_WINDOWS,
        max_trades_per_market: int = 10000,
    ):
        self.windows = sorted(windows)
        self.max_trades_per_market = max_trades_per_market

        # Almacenamiento: condition_id → lista de trades (orden cronológico)
        self._trades: dict[str, list[TradeRecord]] = defaultdict(list)

    # ── Trade Ingestion ───────────────────────────────────────────

    def add_trade(self, condition_id: str, trade_data: dict) -> TradeRecord:
        """Añade un trade al agregador.

        Parameters
        ----------
        condition_id : str
            Identificador del mercado.
        trade_data : dict
            Datos del trade. Campos esperados:
            - price: float
            - size: float
            - side: str ("BUY" o "SELL")
            - id: str (opcional)
            - timestamp: float/str (opcional, usa time.time() si no está)

        Returns
        -------
        TradeRecord
            El trade procesado.
        """
        now = time.time()

        # Parsear side: "BUY"/"SELL" → "buy"/"sell"
        side_raw = str(trade_data.get("side", "")).upper()
        side = "buy" if side_raw == "BUY" else "sell"

        trade = TradeRecord(
            timestamp=float(trade_data.get("timestamp", now)),
            price=float(trade_data.get("price", 0)),
            size=float(trade_data.get("size", 0)),
            side=side,
            condition_id=condition_id,
            trade_id=str(trade_data.get("id", "")),
        )

        trades = self._trades[condition_id]
        trades.append(trade)

        # Buffer circular: mantener solo los últimos N trades
        if len(trades) > self.max_trades_per_market:
            self._trades[condition_id] = trades[-self.max_trades_per_market:]

        return trade

    # ── TFI Calculation ───────────────────────────────────────────

    def get_tfi(self, condition_id: str, window: float = 60) -> TFIResult:
        """Calcula TFI para una ventana temporal específica.

        Parameters
        ----------
        condition_id : str
            Identificador del mercado.
        window : float
            Ventana en segundos (default 60s = 1m).

        Returns
        -------
        TFIResult
        """
        trades = self._trades.get(condition_id, [])
        if not trades:
            return TFIResult(
                condition_id=condition_id,
                tfi=0.0,
                buy_volume=0.0,
                sell_volume=0.0,
                trade_count=0,
                window_seconds=window,
            )

        now = time.time()
        cutoff = now - window

        # Filtrar trades en la ventana (búsqueda binaria desde el final)
        buy_vol = 0.0
        sell_vol = 0.0
        count = 0

        earliest = now
        for trade in reversed(trades):
            if trade.timestamp < cutoff:
                break
            if trade.side == "buy":
                buy_vol += trade.size
            else:
                sell_vol += trade.size
            count += 1
            earliest = trade.timestamp

        total = buy_vol + sell_vol
        tfi = (buy_vol - sell_vol) / total if total > 0 else 0.0

        return TFIResult(
            condition_id=condition_id,
            tfi=tfi,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            trade_count=count,
            window_seconds=now - earliest if count > 0 else window,
        )

    def get_all_tfis(self, condition_id: str) -> dict[str, TFIResult]:
        """Calcula TFI para todas las ventanas configuradas.

        Returns
        -------
        dict[str, TFIResult]
            {"30s": TFIResult, "60s": TFIResult, "300s": TFIResult}
        """
        return {
            f"{int(w)}s" if w < 60 else f"{int(w/60)}m": self.get_tfi(condition_id, w)
            for w in self.windows
        }

    # ── Volume Queries ────────────────────────────────────────────

    def get_volume_ratio(
        self, condition_id: str, window: float = 60
    ) -> float:
        """Ratio buy/sell en una ventana. >1 = más compras, <1 = más ventas."""
        tfi = self.get_tfi(condition_id, window)
        if tfi.sell_volume <= 0:
            return float("inf") if tfi.buy_volume > 0 else 1.0
        return tfi.buy_volume / tfi.sell_volume

    def get_trade_count(self, condition_id: str, window: float = 60) -> int:
        """Número de trades en una ventana."""
        return self.get_tfi(condition_id, window).trade_count

    # ── Maintenance ───────────────────────────────────────────────

    def purge(self, max_age: float = 3600) -> int:
        """Elimina trades más antiguos que max_age segundos.

        Returns número de mercados limpiados.
        """
        now = time.time()
        cutoff = now - max_age
        purged = 0

        for cid, trades in list(self._trades.items()):
            # Mantener solo trades recientes
            kept = [t for t in trades if t.timestamp > cutoff]
            if len(kept) < len(trades):
                purged += 1
                self._trades[cid] = kept

        return purged

    def remove_market(self, condition_id: str) -> None:
        """Elimina todos los datos de un mercado."""
        self._trades.pop(condition_id, None)

    def clear(self) -> None:
        """Limpia todos los datos."""
        self._trades.clear()

    @property
    def tracked_markets(self) -> list[str]:
        return list(self._trades.keys())

    @property
    def total_trades(self) -> int:
        return sum(len(trades) for trades in self._trades.values())

    def __len__(self) -> int:
        return len(self._trades)

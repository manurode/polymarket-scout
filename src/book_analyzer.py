"""
BookAnalyzer — Mantenimiento de L2 Order Book y cálculo de OBI.

Mantiene una copia local del libro de órdenes para cada mercado trackeado
usando arrays NumPy para acceso O(1) a niveles de precio. Procesa deltas
recibidos del WebSocket Manager y calcula Order Book Imbalance (OBI) en
tiempo real.

Uso:
    analyzer = BookAnalyzer()
    analyzer.apply_delta(token_id, delta)
    obi = analyzer.get_obi(token_id)  # [-1, +1]
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

MAX_BOOK_LEVELS = 20   # profundidad máxima del libro que mantenemos
LARGE_CANCEL_THRESHOLD = 1000.0  # $1K — cancelaciones que consideramos "grandes"


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class OrderBookLevel:
    """Un nivel de precio en el libro."""
    price: float
    size: float

    def __array__(self) -> np.ndarray:
        return np.array([self.price, self.size])


@dataclass
class BookSnapshot:
    """Snapshot completo del estado local del order book."""
    token_id: str
    bids: np.ndarray       # shape (MAX_BOOK_LEVELS, 2): [[price, size], ...]
    asks: np.ndarray       # shape (MAX_BOOK_LEVELS, 2)
    bid_count: int         # niveles activos en bids
    ask_count: int         # niveles activos en asks
    obi: float             # Order Book Imbalance en [-1, +1]
    mid_price: float       # precio medio del best bid/ask
    spread: float          # spread en valor absoluto
    seq_num: int
    timestamp: float
    large_cancellations_60s: int = 0


@dataclass
class _BookState:
    """Estado interno de un mercado."""
    token_id: str
    # Arrays NumPy pre-alocados para bids y asks
    # Formato: [[price, size], ...] ordenado por price (bids descendente, asks ascendente)
    bids: np.ndarray = field(default_factory=lambda: np.zeros((MAX_BOOK_LEVELS, 2)))
    asks: np.ndarray = field(default_factory=lambda: np.zeros((MAX_BOOK_LEVELS, 2)))
    bid_count: int = 0
    ask_count: int = 0
    seq_num: int = 0
    last_update: float = 0.0

    # Tracking de cancelaciones grandes (ventana 60s)
    _cancel_timestamps: list = field(default_factory=list)

    def record_cancellation(self, size: float) -> None:
        """Registra una cancelación si supera el umbral."""
        if size >= LARGE_CANCEL_THRESHOLD:
            now = time.monotonic()
            self._cancel_timestamps.append((now, size))
            # Purgar cancelaciones > 60s
            cutoff = now - 60
            self._cancel_timestamps = [
                (t, s) for t, s in self._cancel_timestamps if t > cutoff
            ]

    @property
    def large_cancellations_60s(self) -> int:
        """Número de cancelaciones grandes en los últimos 60s."""
        return len(self._cancel_timestamps)

    @property
    def best_bid(self) -> float:
        """Mejor precio de compra (o 0 si no hay bids)."""
        if self.bid_count == 0:
            return 0.0
        return float(self.bids[0, 0])

    @property
    def best_bid_size(self) -> float:
        """Tamaño en USD del mejor bid (o 0 si no hay bids)."""
        if self.bid_count == 0:
            return 0.0
        return float(self.bids[0, 1])

    @property
    def best_ask(self) -> float:
        """Mejor precio de venta (o 0 si no hay asks)."""
        if self.ask_count == 0:
            return 0.0
        return float(self.asks[0, 0])

    @property
    def best_ask_size(self) -> float:
        """Tamaño en USD del mejor ask (o 0 si no hay asks)."""
        if self.ask_count == 0:
            return 0.0
        return float(self.asks[0, 1])

    @property
    def top_size_total(self) -> float:
        """Suma del tamaño del mejor bid + mejor ask (USD)."""
        return self.best_bid_size + self.best_ask_size

    @property
    def mid_price(self) -> float:
        """Precio medio del best bid/ask (fair price)."""
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0 and ba > 0:
            return (bb + ba) / 2.0
        elif bb > 0:
            return bb
        elif ba > 0:
            return ba
        return 0.0

    @property
    def spread(self) -> float:
        """Spread absoluto (best_ask - best_bid)."""
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0 and ba > 0:
            return ba - bb
        return 0.0


# ── BookAnalyzer ──────────────────────────────────────────────────

class BookAnalyzer:
    """Analizador de order books en tiempo real.

    Mantiene copias locales del L2 order book para múltiples mercados.
    Calcula OBI (Order Book Imbalance) instantáneo usando operaciones
    vectorizadas de NumPy.

    Parameters
    ----------
    max_levels : int
        Niveles del libro a mantener (default 20).
    """

    def __init__(self, max_levels: int = MAX_BOOK_LEVELS):
        self.max_levels = max_levels
        self._books: dict[str, _BookState] = {}

    # ── Delta Application ──────────────────────────────────────────

    def apply_delta(self, token_id: str, delta: dict) -> BookSnapshot:
        """Aplica un delta de WebSocket al order book local.

        Parameters
        ----------
        token_id : str
            Identificador del mercado.
        delta : dict
            Delta del WebSocket CLOB. Espera campos:
            - bids: list[list[float, float]] — nuevas bids [[price, size], ...]
            - asks: list[list[float, float]] — nuevas asks
            - seq_num: int (opcional)
            - type: str (opcional, "new"/"update"/"delete")

        Returns
        -------
        BookSnapshot
            Estado actualizado del libro.
        """
        book = self._get_or_create_book(token_id)
        now = time.monotonic()

        seq = delta.get("seq_num", delta.get("sequence"))
        if seq is not None:
            book.seq_num = int(seq)

        delta_type = delta.get("type", "")

        # Procesar bids
        raw_bids = delta.get("bids", [])
        if raw_bids:
            if delta_type == "new":
                # Reemplazo completo
                book.bid_count = self._replace_levels(book.bids, raw_bids)
            else:
                # Merge: actualizar niveles existentes
                book.bid_count = self._merge_levels(book.bids, raw_bids, book.bid_count)

        # Procesar asks
        raw_asks = delta.get("asks", [])
        if raw_asks:
            if delta_type == "new":
                book.ask_count = self._replace_levels(book.asks, raw_asks)
            else:
                book.ask_count = self._merge_levels(book.asks, raw_asks, book.ask_count)

        # Detectar cancelaciones (delta sin bids/asks → posible cancel)
        changes = delta.get("changes", [])
        for change in changes:
            if change.get("side") in ("buy", "sell"):
                book.record_cancellation(abs(float(change.get("size", 0))))

        book.last_update = now

        return self._build_snapshot(token_id, book)

    def initialize_book(self, token_id: str, snapshot: dict) -> BookSnapshot:
        """Inicializa un order book desde un snapshot REST completo.

        Parameters
        ----------
        token_id : str
            Identificador del mercado.
        snapshot : dict
            Snapshot REST del CLOB con bids/asks completos.
            El CLOB devuelve entries como {"price": "0.55", "size": "150.0"}
            (strings) sin orden garantizado — esta función los ordena
            correctamente: bids DESC, asks ASC.
        """
        book = self._get_or_create_book(token_id)
        now = time.monotonic()

        raw_bids = snapshot.get("bids", [])
        raw_asks = snapshot.get("asks", [])

        book.bid_count = self._replace_levels(book.bids, raw_bids, sort_desc=True)
        book.ask_count = self._replace_levels(book.asks, raw_asks, sort_desc=False)

        seq = snapshot.get("seq_num")
        if seq is not None:
            book.seq_num = int(seq)

        book.last_update = now
        return self._build_snapshot(token_id, book)

    # ── Level Management ──────────────────────────────────────────

    def _replace_levels(self, arr: np.ndarray, levels: list, sort_desc: bool | None = None) -> int:
        """Reemplaza todos los niveles del array.

        Parameters
        ----------
        arr : np.ndarray
            Array pre-alocado de shape (max_levels, 2).
        levels : list
            Niveles raw del CLOB. Cada entry puede ser:
            - list/tuple: [price, size]
            - dict: {"price": "0.55", "size": "150.0"}  ← formato REST CLOB
        sort_desc : bool | None
            True  → ordenar por precio DESC (bids: mejor primero).
            False → ordenar por precio ASC  (asks: mejor primero).
            None  → respetar el orden recibido (deltas WS ya vienen ordenados).
        """
        parsed: list[tuple[float, float]] = []
        for level in levels:
            try:
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    price = float(level[0])
                    size  = float(level[1])
                elif isinstance(level, dict):
                    price = float(level.get("price", level.get("p", 0)))
                    size  = float(level.get("size",  level.get("s",  0)))
                else:
                    continue
                if price > 0:  # ignorar entradas de precio cero
                    parsed.append((price, size))
            except (TypeError, ValueError):
                continue

        if sort_desc is True:
            parsed.sort(key=lambda x: x[0], reverse=True)   # bids: mejor (más alto) primero
        elif sort_desc is False:
            parsed.sort(key=lambda x: x[0], reverse=False)  # asks: mejor (más bajo) primero

        count = min(len(parsed), self.max_levels)
        for i, (price, size) in enumerate(parsed[:count]):
            arr[i, 0] = price
            arr[i, 1] = size
        # Rellenar resto con cero
        if count < self.max_levels:
            arr[count:, :] = 0.0
        return count

    def _merge_levels(self, arr: np.ndarray, updates: list, current_count: int) -> int:
        """Mergea deltas en el array existente (upsert + delete si size=0)."""
        count = current_count

        for update in updates:
            if isinstance(update, (list, tuple)):
                price = float(update[0])
                size = float(update[1])
            elif isinstance(update, dict):
                price = float(update.get("price", update.get("p", 0)))
                size = float(update.get("size", update.get("s", 0)))
            else:
                continue

            # Buscar el nivel existente
            found = False
            for i in range(count):
                if abs(arr[i, 0] - price) < 1e-12:
                    if size <= 0:
                        # Delete: eliminar nivel desplazando hacia arriba
                        arr[i:count-1] = arr[i+1:count]
                        arr[count-1] = [0, 0]
                        count -= 1
                    else:
                        arr[i, 1] = size
                    found = True
                    break

            if not found and size > 0 and count < self.max_levels:
                # Insertar nuevo nivel
                arr[count, 0] = price
                arr[count, 1] = size
                count += 1

        return count

    # ── OBI Calculation ───────────────────────────────────────────

    def get_obi(self, token_id: str, levels: int = 10, min_total_size: float = 0.0) -> float:
        """Calcula Order Book Imbalance para los top N niveles.

        OBI = (Σ bid_volume - Σ ask_volume) / (Σ bid_volume + Σ ask_volume)
        Rango: [-1, +1]

        Si min_total_size > 0 y la suma del mejor bid + mejor ask es inferior
        a ese umbral, retorna 0.0 (filtro anti-"calderilla": desbalances con poco
        dinero en juego no son indicativos de spoofing ni flujo tóxico).

        Parameters
        ----------
        token_id : str
            Identificador del mercado.
        levels : int
            Niveles a considerar (default 10).
        min_total_size : float
            Umbral mínimo de tamaño USD en el top del libro para considerar
            el OBI válido. Default 0.0 (sin filtro).
        """
        book = self._books.get(token_id)
        if book is None:
            return 0.0

        # Filtro anti-"calderilla": si hay poco dinero en el mejor nivel, OBI no es fiable
        if min_total_size > 0 and book.top_size_total < min_total_size:
            return 0.0

        n_bids = min(levels, book.bid_count)
        n_asks = min(levels, book.ask_count)

        bid_volume = float(np.sum(book.bids[:n_bids, 1])) if n_bids > 0 else 0.0
        ask_volume = float(np.sum(book.asks[:n_asks, 1])) if n_asks > 0 else 0.0

        total = bid_volume + ask_volume
        if total <= 0:
            return 0.0

        return (bid_volume - ask_volume) / total

    def get_imbalance_direction(self, token_id: str) -> int:
        """Dirección del desequilibrio: +1 (presión compradora), -1 (vendedora), 0 (neutro)."""
        obi = self.get_obi(token_id)
        if obi > 0.15:
            return 1
        elif obi < -0.15:
            return -1
        return 0

    # ── Book Queries ──────────────────────────────────────────────

    def get_book(self, token_id: str) -> BookSnapshot | None:
        """Retorna el snapshot actual de un mercado."""
        book = self._books.get(token_id)
        if book is None:
            return None
        return self._build_snapshot(token_id, book)

    def get_mid_price(self, token_id: str) -> float:
        """Precio medio del best bid/ask."""
        book = self._books.get(token_id)
        return book.mid_price if book else 0.0

    def get_spread(self, token_id: str) -> float:
        """Spread absoluto."""
        book = self._books.get(token_id)
        return book.spread if book else 0.0

    def get_large_cancellations(self, token_id: str) -> int:
        """Cancelaciones grandes en los últimos 60s."""
        book = self._books.get(token_id)
        return book.large_cancellations_60s if book else 0

    def remove_book(self, token_id: str) -> None:
        """Elimina el tracking de un mercado (para liberar memoria)."""
        self._books.pop(token_id, None)

    # ── Internal ──────────────────────────────────────────────────

    def _get_or_create_book(self, token_id: str) -> _BookState:
        """Obtiene o crea el estado interno de un mercado."""
        if token_id not in self._books:
            self._books[token_id] = _BookState(token_id=token_id)
        return self._books[token_id]

    def _build_snapshot(self, token_id: str, book: _BookState) -> BookSnapshot:
        """Construye un BookSnapshot desde el estado interno."""
        return BookSnapshot(
            token_id=token_id,
            bids=book.bids.copy(),
            asks=book.asks.copy(),
            bid_count=book.bid_count,
            ask_count=book.ask_count,
            obi=self.get_obi(token_id),
            mid_price=book.mid_price,
            spread=book.spread,
            seq_num=book.seq_num,
            timestamp=book.last_update,
            large_cancellations_60s=book.large_cancellations_60s,
        )

    @property
    def tracked_markets(self) -> list[str]:
        return list(self._books.keys())

    def __len__(self) -> int:
        return len(self._books)

    def __bool__(self) -> bool:
        """A BookAnalyzer instance is always valid — never falsy."""
        return True

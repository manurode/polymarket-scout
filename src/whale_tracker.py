"""
Whale Tracker — Seguimiento on-chain de ballenas y Conviction Multiplier.

Implementa el sistema de tracking de ballenas (§3.2 del ARCHITECTURE_V2.md):

1. Whale Tracker Daemon: monitorea eventos on-chain de Polygon (CTF Exchange).
2. Alpha Whale Score: ranking de wallets por P&L, win rate, Sortino, consistencia.
3. Wallet Clustering Anti-Sybil: agrupa wallets del mismo operador.
4. Conviction Multiplier: amplifica o atenúa señales según flujo de ballenas.

Para paper trading, los datos on-chain se simulan con una interfaz abstracta
que puede conectarse a Alchemy/QuickNode WebSocket en producción.

Uso:
    wt = WhaleTracker()
    wt.update_wallet_profile("0xABC", pnl=5000, win_rate=0.65, trades=120)
    cm = wt.get_conviction_multiplier("0xmarket123")
"""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

WHALE_SCORE_THRESHOLD = 0.85       # top 5% → Alpha Whale
MIN_TRANSACTIONS_FOR_PROFILE = 50  # mínimo de transacciones para perfilar
CLUSTER_SIMILARITY_WINDOW_MS = 500  # ventana para behavioral clustering (ms)
CLUSTER_SIMILARITY_WINDOW_S = 5     # ventana para behavioral clustering manual (s)
WHALE_FACTOR = 0.40               # peso máximo del CM

# Ventanas de flujo
WHALE_FLOW_WINDOW_1H = 3600
WHALE_FLOW_WINDOW_24H = 86400


# ── Tipos ──────────────────────────────────────────────────────────

@dataclass
class WalletProfile:
    """Perfil de una wallet trackeada."""
    address: str
    total_pnl: float = 0.0
    win_rate: float = 0.0
    sortino: float = 0.0
    trades_per_week: float = 0.0
    total_trades: int = 0
    alpha_score: float = 0.0
    is_alpha_whale: bool = False
    cluster_id: str = ""
    last_updated: float = 0.0


@dataclass
class WhaleFlow:
    """Flujo de ballenas para un mercado."""
    condition_id: str
    net_flow_1h: float = 0.0          # buy - sell en USD (última hora)
    net_flow_24h: float = 0.0         # buy - sell en USD (24h)
    whale_consensus: float = 0.0       # [0, 1] — acuerdo entre ballenas
    whale_zscore: float = 0.0          # cuán inusual es este flujo
    active_whales: int = 0
    bullish_whales: int = 0
    bearish_whales: int = 0
    timestamp: float = 0.0


@dataclass
class ConvictionMultiplier:
    """Multiplicador de convicción."""
    cm: float                          # [0.6, 1.4]
    net_flow: float
    consensus: float
    zscore: float
    interpretation: str                # "bullish", "bearish", "neutral"


@dataclass
class _WhaleTrade:
    """Trade individual de una ballena."""
    wallet: str
    condition_id: str
    side: str          # "buy" o "sell"
    volume: float      # USD
    timestamp: float
    tx_hash: str = ""


@dataclass
class WalletCluster:
    """Cluster de wallets (operador fragmentado)."""
    cluster_id: str
    wallets: list[str] = field(default_factory=list)
    total_pnl: float = 0.0
    total_volume: float = 0.0
    cluster_alpha_score: float = 0.0
    last_active: float = 0.0


# ── WhaleTracker ───────────────────────────────────────────────────

class WhaleTracker:
    """Tracker de ballenas con clustering anti-Sybil.

    Parameters
    ----------
    alpha_threshold : float
        Umbral para considerar Alpha Whale (default 0.85).
    min_transactions : int
        Mínimo de transacciones para perfilar (default 50).
    """

    def __init__(
        self,
        alpha_threshold: float = WHALE_SCORE_THRESHOLD,
        min_transactions: int = MIN_TRANSACTIONS_FOR_PROFILE,
    ):
        self.alpha_threshold = alpha_threshold
        self.min_transactions = min_transactions

        # Perfiles de wallet
        self._wallets: dict[str, WalletProfile] = {}

        # Trades de ballenas: condition_id → lista
        self._whale_trades: dict[str, list[_WhaleTrade]] = defaultdict(list)

        # Flujo histórico para z-score
        self._flow_history: dict[str, list[float]] = defaultdict(list)

        # Clusters
        self._clusters: dict[str, WalletCluster] = {}

    # ── Wallet Profiling ─────────────────────────────────────────

    def update_wallet_profile(
        self,
        address: str,
        pnl: float = 0.0,
        win_rate: float = 0.0,
        sortino: float = 0.0,
        trades_per_week: float = 0.0,
        total_trades: int = 0,
    ) -> WalletProfile:
        """Actualiza o crea el perfil de una wallet.

        Returns
        -------
        WalletProfile
        """
        if address not in self._wallets:
            self._wallets[address] = WalletProfile(address=address)

        profile = self._wallets[address]
        profile.total_pnl = pnl
        profile.win_rate = win_rate
        profile.sortino = sortino
        profile.trades_per_week = trades_per_week
        profile.total_trades = total_trades
        profile.last_updated = time.time()

        # Calcular Alpha Whale Score
        profile.alpha_score = self._compute_alpha_score(profile)
        profile.is_alpha_whale = (
            profile.alpha_score >= self.alpha_threshold
            and total_trades >= self.min_transactions
        )

        return profile

    def _compute_alpha_score(self, profile: WalletProfile) -> float:
        """Calcula Alpha Whale Score (§3.2).

        alpha_score = pnl_percentile × 0.40
                    + win_rate × 0.25
                    + sortino_normalized × 0.20
                    + consistency × 0.15
        """
        # P&L percentil: aproximación simple basada en valor absoluto
        # En producción, esto se normalizaría contra todos los perfiles
        pnl_score = min(1.0, max(0.0, profile.total_pnl / 50000.0))

        # Sortino normalizado
        sortino_norm = min(1.0, max(0.0, profile.sortino / 3.0))

        # Consistencia: min(1.0, trades_per_week / 5)
        consistency = min(1.0, profile.trades_per_week / 5.0)

        return (
            pnl_score * 0.40
            + profile.win_rate * 0.25
            + sortino_norm * 0.20
            + consistency * 0.15
        )

    # ── Whale Trade Recording ─────────────────────────────────────

    def record_whale_trade(
        self,
        wallet: str,
        condition_id: str,
        side: str,
        volume: float,
        tx_hash: str = "",
        timestamp: Optional[float] = None,
    ) -> _WhaleTrade:
        """Registra un trade de una ballena.

        Solo se registra si la wallet es Alpha Whale.
        """
        if timestamp is None:
            timestamp = time.time()

        profile = self._wallets.get(wallet)
        if profile is None or not profile.is_alpha_whale:
            # No trackear si no es Alpha Whale
            return None

        trade = _WhaleTrade(
            wallet=wallet,
            condition_id=condition_id,
            side=side.lower(),
            volume=volume,
            timestamp=timestamp,
            tx_hash=tx_hash,
        )

        self._whale_trades[condition_id].append(trade)

        # Limpiar trades antiguos (>25h)
        cutoff = timestamp - WHALE_FLOW_WINDOW_24H * 1.1
        self._whale_trades[condition_id] = [
            t for t in self._whale_trades[condition_id]
            if t.timestamp > cutoff
        ]

        return trade

    # ── Whale Flow ────────────────────────────────────────────────

    def get_whale_flow(self, condition_id: str) -> WhaleFlow:
        """Calcula el flujo de ballenas para un mercado."""
        trades = self._whale_trades.get(condition_id, [])
        now = time.time()

        if not trades:
            return WhaleFlow(condition_id=condition_id, timestamp=now)

        cutoff_1h = now - WHALE_FLOW_WINDOW_1H
        cutoff_24h = now - WHALE_FLOW_WINDOW_24H

        # Flujo 1h
        buy_1h = sum(t.volume for t in trades if t.timestamp > cutoff_1h and t.side == "buy")
        sell_1h = sum(t.volume for t in trades if t.timestamp > cutoff_1h and t.side == "sell")
        net_flow_1h = buy_1h - sell_1h

        # Flujo 24h
        buy_24h = sum(t.volume for t in trades if t.timestamp > cutoff_24h and t.side == "buy")
        sell_24h = sum(t.volume for t in trades if t.timestamp > cutoff_24h and t.side == "sell")
        net_flow_24h = buy_24h - sell_24h

        # Consenso entre ballenas activas
        active_wallets = set(
            t.wallet for t in trades if t.timestamp > cutoff_1h
        )
        active_whales = len(active_wallets)

        # Dirección de cada ballena activa (neta en 1h)
        wallet_net = defaultdict(float)
        for t in trades:
            if t.timestamp > cutoff_1h:
                wallet_net[t.wallet] += t.volume if t.side == "buy" else -t.volume

        bullish = sum(1 for net in wallet_net.values() if net > 0)
        bearish = sum(1 for net in wallet_net.values() if net < 0)

        if active_whales > 0:
            consensus = abs(bullish - bearish) / active_whales
        else:
            consensus = 0.0

        # Z-score del flujo 1h
        self._flow_history[condition_id].append(net_flow_1h)
        if len(self._flow_history[condition_id]) > 100:
            self._flow_history[condition_id] = self._flow_history[condition_id][-100:]

        flow_list = self._flow_history[condition_id]
        if len(flow_list) >= 5:
            mean_flow = sum(flow_list[:-1]) / (len(flow_list) - 1)
            std_flow = (
                sum((f - mean_flow) ** 2 for f in flow_list[:-1]) / (len(flow_list) - 1)
            ) ** 0.5
            zscore = (net_flow_1h - mean_flow) / (std_flow + 1.0) if std_flow > 0 else 0.0
        else:
            zscore = 0.0

        return WhaleFlow(
            condition_id=condition_id,
            net_flow_1h=net_flow_1h,
            net_flow_24h=net_flow_24h,
            whale_consensus=consensus,
            whale_zscore=zscore,
            active_whales=active_whales,
            bullish_whales=bullish,
            bearish_whales=bearish,
            timestamp=now,
        )

    # ── Conviction Multiplier ─────────────────────────────────────

    def get_conviction_multiplier(self, condition_id: str) -> ConvictionMultiplier:
        """Calcula el Conviction Multiplier para un mercado.

        CM = 1.0 + tanh(whale_zscore) × whale_consensus × WHALE_FACTOR
        Rango: [0.6, 1.4]
        """
        flow = self.get_whale_flow(condition_id)

        cm = 1.0 + math.tanh(flow.whale_zscore) * flow.whale_consensus * WHALE_FACTOR

        # Clamp
        cm = max(0.6, min(1.4, cm))

        # Interpretación
        if cm > 1.1:
            interpretation = "bullish"
        elif cm < 0.9:
            interpretation = "bearish"
        else:
            interpretation = "neutral"

        return ConvictionMultiplier(
            cm=cm,
            net_flow=flow.net_flow_1h,
            consensus=flow.whale_consensus,
            zscore=flow.whale_zscore,
            interpretation=interpretation,
        )

    def apply_conviction(
        self,
        condition_id: str,
        signal_strength: float,
        position_size: float,
        max_position_size: float,
    ) -> tuple[float, float]:
        """Aplica el Conviction Multiplier a una señal y tamaño de posición.

        Returns
        -------
        tuple[float, float]
            (signal_strength_final, position_size_final)
        """
        cm_result = self.get_conviction_multiplier(condition_id)
        cm = cm_result.cm

        signal_final = signal_strength * cm
        size_final = position_size * cm

        # Nunca exceder max_position
        size_final = min(size_final, max_position_size)

        return signal_final, size_final

    # ── Wallet Clustering ─────────────────────────────────────────

    def cluster_wallets(self) -> list[WalletCluster]:
        """Ejecuta clustering de wallets (completo, batch).

        Agrupa wallets por:
        1. Funding: misma dirección de exchange + misma ventana temporal.
        2. Behavioral: mismas operaciones en <500ms.

        Returns
        -------
        list[WalletCluster]
        """
        # En paper trading / sin datos on-chain reales, devolvemos clusters vacíos
        # La implementación completa requiere datos históricos de Polygon
        return list(self._clusters.values())

    def add_wallet_to_cluster(
        self, wallet: str, cluster_id: str,
    ) -> None:
        """Añade manualmente una wallet a un cluster."""
        if cluster_id not in self._clusters:
            self._clusters[cluster_id] = WalletCluster(
                cluster_id=cluster_id,
            )

        cluster = self._clusters[cluster_id]
        if wallet not in cluster.wallets:
            cluster.wallets.append(wallet)

        profile = self._wallets.get(wallet)
        if profile:
            profile.cluster_id = cluster_id
            cluster.total_pnl += profile.total_pnl
            cluster.last_active = time.time()

        # Recalcular cluster alpha score
        n = len(cluster.wallets)
        cohesion = min(1.0, 0.7 + 0.3 * math.log2(n) if n > 1 else 0.7)
        cluster.cluster_alpha_score = min(1.0, cohesion * 0.9)

    # ── Query ─────────────────────────────────────────────────────

    def get_wallet_profile(self, address: str) -> Optional[WalletProfile]:
        """Retorna el perfil de una wallet."""
        return self._wallets.get(address)

    def is_alpha_whale(self, address: str) -> bool:
        """Verifica si una wallet es Alpha Whale."""
        profile = self._wallets.get(address)
        return profile is not None and profile.is_alpha_whale

    def get_alpha_whales(self) -> list[str]:
        """Retorna todas las wallets Alpha Whale."""
        return [
            addr for addr, p in self._wallets.items()
            if p.is_alpha_whale
        ]

    def get_market_whales(self, condition_id: str) -> list[str]:
        """Retorna ballenas activas en un mercado (última hora)."""
        now = time.time()
        cutoff = now - WHALE_FLOW_WINDOW_1H
        trades = self._whale_trades.get(condition_id, [])
        return list(set(
            t.wallet for t in trades if t.timestamp > cutoff
        ))

    def clear(self) -> None:
        """Limpia todos los datos."""
        self._wallets.clear()
        self._whale_trades.clear()
        self._flow_history.clear()
        self._clusters.clear()

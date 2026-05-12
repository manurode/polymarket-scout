"""
Portfolio Manager Dinámico — Asignación óptima de capital entre estrategias.

Implementa los 3 pilares de gestión de portfolio de la arquitectura v2.0:

1. Thompson Sampling Multi-Armed Bandit (§4.1):
   - Cada estrategia es un brazo en un problema MAB.
   - Recompensa: Sortino Ratio por época (6h).
   - Muestreo de distribuciones Beta(α=1+S, β=1+F) para cada estrategia.
   - Asignación proporcional a θ_i muestreado.

2. Kelly Fraccional Dinámico (§4.2):
   - f_kelly calculado para mercados binarios: f = p_true - (1-p_true)*P/(1-P).
   - k_dynamic = k_base × sortino_scalar × liquidity_scalar × time_scalar × corr_scalar.
   - k_base = 0.25 (Quarter Kelly conservador).
   - Ruin Gate: ninguna operación puede perder > 2% del portfolio.

3. Sortino Ratio (§4.2):
   - Métrica oficial: solo penaliza volatilidad a la baja.
   - Sortino = (R_p - MAR) / σ_downside.
   - Elimina el Ratio de Sharpe (inapropiado para retornos bimodales).

Uso:
    pm = PortfolioManager(strategies=["momentum", "contrarian", "market_making"])
    pm.update_strategy_performance("momentum", trades)
    allocation = pm.allocate(equity=10000)
    size = pm.position_size("momentum", edge=0.05, price=0.55, equity=10000)
"""

import math
import time
import random
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── Constantes ────────────────────────────────────────────────────

# Kelly
K_BASE = 0.25                # Quarter Kelly (conservador)
RUIN_LIMIT = 0.02            # 2% del equity máximo por operación
MIN_POSITION_SIZE = 5.0      # $5 mínimo
MAX_POSITION_SIZE = 5000.0   # $5000 máximo

# Bandit
DEFAULT_EPOCH_HOURS = 6      # reasignación cada 6h
PROBATION_EPOCHS = 4         # épocas de prueba para nuevas estrategias
PROBATION_ALLOCATION = 0.16  # 16% del capital durante prueba (base de exploración)
RECOVERY_ALLOCATION = 0.04   # v2.0: 4% durante recovery (grace period reducido)
RECOVERY_TRADES_REQUIRED = 10  # v2.0: trades necesarios en RECOVERY para evaluar salida
BETA_PRIOR_A = 1.0           # prior no informativo
BETA_PRIOR_B = 1.0

# Sortino
MAR = 0.0                    # Minimum Acceptable Return


# ── Tipos ──────────────────────────────────────────────────────────

class StrategyStatus(Enum):
    PROBATION = "probation"  # nueva, acumulando historial
    ACTIVE = "active"        # compitiendo
    ELITE = "elite"          # rendimiento consistente
    RECOVERY = "recovery"    # grace period: operando con tamaño reducido antes de FROZEN
    FROZEN = "frozen"        # 3 épocas consecutivas Sortino < 0 tras RECOVERY fallido
    RETIRED = "retired"      # 10 épocas sin recuperar


@dataclass
class StrategyState:
    """Estado interno de una estrategia en el bandit."""
    name: str
    status: StrategyStatus = StrategyStatus.PROBATION
    successes: int = 0       # épocas con Sortino > 0
    failures: int = 0        # épocas con Sortino ≤ 0
    consecutive_losses: int = 0
    probation_epochs: int = 0
    frozen_epochs: int = 0
    sortino_history: list[float] = field(default_factory=list)
    allocation: float = 0.0
    created_at: float = field(default_factory=time.time)
    # --- v2.0: RECOVERY grace period tracking ---
    recovery_trades_count: int = 0
    recovery_pnl_cumulative: float = 0.0
    # --- Campos para paper trading ---
    total_trades: int = 0
    winning_trades: int = 0
    cumulative_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0


@dataclass
class Allocation:
    """Resultado de asignación de capital."""
    strategy: str
    fraction: float          # fracción del equity total [0, 1]
    amount: float            # cantidad en USD
    status: str


@dataclass
class PositionSize:
    """Resultado del cálculo de position sizing."""
    f_kelly: float           # fracción Kelly pura
    k_dynamic: float         # modulador dinámico
    f_fractional: float      # fracción final
    size_raw: float          # tamaño en USD antes de gates
    size_final: float        # tamaño final después de todos los gates
    restricted_by: str       # "none", "ruin_gate", "correlation", "min", "max"
    sortino: float           # Sortino actual de la estrategia


# ── PortfolioManager ───────────────────────────────────────────────

class PortfolioManager:
    """Gestor de portfolio con Thompson Sampling + Kelly Fraccional.

    Parameters
    ----------
    strategies : list[str]
        Nombres de estrategias a gestionar.
    k_base : float
        Fracción base de Kelly (default 0.25 = Quarter Kelly).
    ruin_limit : float
        Máxima pérdida por operación como fracción del equity (default 0.02).
    epoch_hours : float
        Duración de cada época del bandit en horas (default 6).
    """

    def __init__(
        self,
        strategies: list[str],
        k_base: float = K_BASE,
        ruin_limit: float = RUIN_LIMIT,
        epoch_hours: float = DEFAULT_EPOCH_HOURS,
    ):
        self.k_base = k_base
        self.ruin_limit = ruin_limit
        self.epoch_hours = epoch_hours

        # Inicializar estrategias
        self._strategies: dict[str, StrategyState] = {}
        for name in strategies:
            self._strategies[name] = StrategyState(
                name=name,
                status=StrategyStatus.PROBATION,
            )

        self._epoch_counter = 0
        self._last_epoch_time = time.time()

    # ── Strategy Registration ─────────────────────────────────────

    def add_strategy(self, name: str) -> None:
        """Añade una nueva estrategia (entra en PROBATION)."""
        if name not in self._strategies:
            self._strategies[name] = StrategyState(
                name=name,
                status=StrategyStatus.PROBATION,
            )

    def remove_strategy(self, name: str) -> None:
        """Elimina una estrategia."""
        self._strategies.pop(name, None)

    # ── Performance Update ────────────────────────────────────────

    def update_strategy_performance(
        self,
        strategy_name: str,
        trades: list[dict],
    ) -> float:
        """Actualiza el rendimiento de una estrategia con sus trades cerrados.

        Parameters
        ----------
        strategy_name : str
            Nombre de la estrategia.
        trades : list[dict]
            Lista de trades cerrados con campos: pnl, amount_invested.

        Returns
        -------
        float
            Sortino Ratio calculado.
        """
        state = self._strategies.get(strategy_name)
        if state is None:
            return 0.0

        sortino = self._calculate_sortino(trades)
        state.sortino_history.append(sortino)

        # Actualizar contadores del bandit (éxito/fracaso binario)
        if sortino > 0:
            state.successes += 1
            state.consecutive_losses = 0
            state.frozen_epochs = 0
        else:
            state.failures += 1
            state.consecutive_losses += 1

        # Actualizar estado de la estrategia
        self._update_strategy_status(state)

        return sortino

    def _update_strategy_status(self, state: StrategyState) -> None:
        """Actualiza el estado de una estrategia según sus reglas de ciclo de vida.

        v2.0: RECOVERY grace period antes de FROZEN.
        - ACTIVE/ELITE con 3 pérdidas consecutivas → RECOVERY (no FROZEN)
        - RECOVERY: tras 10 trades, si PnL ≤ 0 → FROZEN; si PnL > 0 → ACTIVE
        - FROZEN: 10 épocas sin recuperar → RETIRED
        """
        if state.status == StrategyStatus.PROBATION:
            state.probation_epochs += 1
            if state.probation_epochs >= PROBATION_EPOCHS:
                state.status = StrategyStatus.ACTIVE
            return

        if state.status in (StrategyStatus.ACTIVE, StrategyStatus.ELITE):
            if state.consecutive_losses >= 3:
                # v2.0: Ir a RECOVERY en lugar de FROZEN
                state.status = StrategyStatus.RECOVERY
                state.recovery_trades_count = 0
                state.recovery_pnl_cumulative = 0.0
                state.frozen_epochs = 0
            elif state.sortino_history and state.sortino_history[-1] > 2.0:
                state.status = StrategyStatus.ELITE
            return

        if state.status == StrategyStatus.RECOVERY:
            # Evaluar salida de RECOVERY tras N trades
            if state.recovery_trades_count >= RECOVERY_TRADES_REQUIRED:
                if state.recovery_pnl_cumulative > 0:
                    # PnL positivo → volver a ACTIVE
                    state.status = StrategyStatus.ACTIVE
                    state.consecutive_losses = 0
                    state.recovery_trades_count = 0
                    state.recovery_pnl_cumulative = 0.0
                else:
                    # PnL ≤ 0 tras recovery → FROZEN
                    state.status = StrategyStatus.FROZEN
                    state.frozen_epochs = 0
                    state.recovery_trades_count = 0
                    state.recovery_pnl_cumulative = 0.0
            return

        if state.status == StrategyStatus.FROZEN:
            state.frozen_epochs += 1
            if state.sortino_history and state.sortino_history[-1] > 0:
                # Recuperación: volver a ACTIVE
                state.status = StrategyStatus.ACTIVE
                state.consecutive_losses = 0
            elif state.frozen_epochs >= 10:
                state.status = StrategyStatus.RETIRED
            return

    # ── Capital Allocation (Thompson Sampling) ────────────────────

    def allocate(self, equity: float) -> list[Allocation]:
        """Asigna capital entre estrategias usando Thompson Sampling.

        Parameters
        ----------
        equity : float
            Capital total disponible en USD.

        Returns
        -------
        list[Allocation]
            Asignación para cada estrategia.
        """
        import logging
        _log = logging.getLogger("portfolio_manager.bandit")

        # Muestrear theta_i ~ Beta(1+S_i, 1+F_i) para cada estrategia
        thetas = {}
        for name, state in self._strategies.items():
            if state.status == StrategyStatus.RETIRED:
                thetas[name] = 0.0
            elif state.status == StrategyStatus.PROBATION:
                thetas[name] = PROBATION_ALLOCATION
            elif state.status == StrategyStatus.RECOVERY:
                thetas[name] = RECOVERY_ALLOCATION  # v2.0: reduced allocation during grace period
            elif state.status == StrategyStatus.FROZEN:
                thetas[name] = 0.0  # sin capital
            else:
                alpha = BETA_PRIOR_A + state.successes
                beta_param = BETA_PRIOR_B + state.failures
                thetas[name] = random.betavariate(alpha, beta_param)

        # ── BANDIT_CHECK: loggear peso de cada estrategia ──
        from src.adaptive_strategy_engine import (
            PAPER_TRADING_HYPERACTIVE, _PT_WEIGHT_CUTOFF,
        )
        _weight_cutoff = _PT_WEIGHT_CUTOFF if PAPER_TRADING_HYPERACTIVE else 0.15

        # Normalizar y asignar
        total_theta = sum(thetas.values())
        if total_theta <= 0:
            # Sin estrategias activas → distribuir uniformemente
            active = [n for n, s in self._strategies.items()
                      if s.status not in (StrategyStatus.RETIRED, StrategyStatus.FROZEN)]
            n_active = max(1, len(active))
            uniform = 1.0 / n_active
            return [
                Allocation(
                    strategy=name,
                    fraction=uniform if name in active else 0.0,
                    amount=equity * (uniform if name in active else 0.0),
                    status=self._strategies[name].status.value,
                )
                for name in self._strategies
            ]

        allocations = []
        for name, theta in thetas.items():
            fraction = theta / total_theta
            state = self._strategies[name]
            status = "BLOCKED" if fraction < _weight_cutoff else "READY"
            _log.info(
                "[BANDIT_CHECK] Strategy=%s | Weight=%.4f | "
                "Threshold=%.4f | Status=%s | "
                "successes=%d failures=%d state=%s",
                name, fraction, _weight_cutoff, status,
                state.successes, state.failures, state.status.value,
            )
            allocations.append(Allocation(
                strategy=name,
                fraction=fraction,
                amount=equity * fraction,
                status=self._strategies[name].status.value,
            ))

        return allocations

    def epoch_tick(self) -> None:
        """Avanza una época del bandit."""
        self._epoch_counter += 1
        self._last_epoch_time = time.time()

    # ── Position Sizing (Kelly Fraccional) ────────────────────────

    def position_size(
        self,
        strategy_name: str,
        edge: float,
        price: float,
        equity: float,
        liquidity: float = 50000.0,
        time_multiplier: float = 1.0,
        max_correlation: float = 0.0,
        min_position: float = MIN_POSITION_SIZE,
        max_position: float = MAX_POSITION_SIZE,
    ) -> PositionSize:
        """Calcula el tamaño de posición usando Kelly Fraccional Dinámico.

        Parameters
        ----------
        strategy_name : str
            Nombre de la estrategia.
        edge : float
            Ventaja estimada: p_true - P_market.
        price : float
            Precio actual del mercado (YES).
        equity : float
            Capital total disponible.
        liquidity : float
            Liquidez del mercado en USD.
        time_multiplier : float
            Multiplicador de time-decay (de TimeDecayManager).
        max_correlation : float
            Correlación máxima con posiciones existentes [0, 1].
        min_position : float
            Tamaño mínimo de posición en USD.
        max_position : float
            Tamaño máximo de posición en USD.

        Returns
        -------
        PositionSize
        """
        p_true = price + edge

        # ── 1. Calcular f_kelly ─────────────────────────────
        if edge <= 0:
            return PositionSize(
                f_kelly=0.0, k_dynamic=0.0, f_fractional=0.0,
                size_raw=0.0, size_final=0.0,
                restricted_by="negative_edge", sortino=0.0,
            )

        # f_kelly = p_true - (1-p_true)*P/(1-P)
        if price >= 1.0:
            f_kelly = 0.0
        else:
            f_kelly = p_true - (1.0 - p_true) * price / (1.0 - price)
            f_kelly = max(0.0, f_kelly)

        # ── 2. Calcular k_dynamic ───────────────────────────
        sortino = self.get_sortino(strategy_name)
        liquidity_scalar = min(1.0, liquidity / 50000.0)
        liquidity_scalar = max(0.1, liquidity_scalar)

        sortino_scalar = max(0.25, min(1.5, sortino / 2.0)) if sortino > 0 else 0.25

        corr_scalar = 1.0 - max_correlation * 0.5

        k_dynamic = self.k_base * sortino_scalar * liquidity_scalar * time_multiplier * corr_scalar

        # ── 3. Fracción fraccional ────────────────────────
        f_fractional = f_kelly * k_dynamic

        # ── 4. Tamaño raw ────────────────────────────────
        size_raw = equity * f_fractional
        restricted_by = "none"

        # ── 5. Ruin Gate ─────────────────────────────────
        max_loss = size_raw * price  # pérdida máxima = precio pagado
        ruin_limit_usd = self.ruin_limit * equity

        if max_loss > ruin_limit_usd and price > 0:
            size_raw = ruin_limit_usd / price
            restricted_by = "ruin_gate"

        # ── 6. Correlation Penalty ────────────────────────
        if max_correlation > 0.5:
            size_raw *= (1.0 - max_correlation * 0.5)
            if restricted_by == "none":
                restricted_by = "correlation"

        # ── 7. Clamp ──────────────────────────────────────
        if size_raw < min_position and f_fractional > 0:
            size_raw = 0.0  # demasiado pequeño, no operar
            restricted_by = "min"
        size_final = min(size_raw, max_position)
        if size_raw > max_position:
            restricted_by = "max"

        return PositionSize(
            f_kelly=f_kelly,
            k_dynamic=k_dynamic,
            f_fractional=f_fractional,
            size_raw=size_raw,
            size_final=size_final,
            restricted_by=restricted_by,
            sortino=sortino,
        )

    # ── Sortino Ratio ─────────────────────────────────────────────

    def get_sortino(self, strategy_name: str) -> float:
        """Retorna el Sortino Ratio actual de una estrategia."""
        state = self._strategies.get(strategy_name)
        if state is None or not state.sortino_history:
            return 0.0
        return state.sortino_history[-1]

    @staticmethod
    def _calculate_sortino(trades: list[dict]) -> float:
        """Calcula el Sortino Ratio para una lista de trades.

        Sortino = (R_p - MAR) / σ_downside

        Parameters
        ----------
        trades : list[dict]
            Trades con campos: pnl (float), amount_invested (float).

        Returns
        -------
        float
            Sortino Ratio.
        """
        if not trades:
            return 0.0

        # Retornos por trade
        returns = []
        for t in trades:
            pnl = float(t.get("pnl", 0))
            invested = float(t.get("amount_invested", 1))
            if invested > 0:
                returns.append(pnl / invested)

        if not returns:
            return 0.0

        n = len(returns)

        # Media geométrica
        product = 1.0
        for r in returns:
            product *= (1.0 + r)
        geo_mean = product ** (1.0 / n) - 1.0

        # Downside deviation
        downside = [min(0.0, r - MAR) ** 2 for r in returns]
        downside_var = sum(downside) / n
        downside_std = math.sqrt(downside_var)

        if downside_std <= 0:
            return 0.0 if geo_mean <= 0 else 10.0  # sin downside risk → Sortino alto

        return geo_mean / downside_std

    @staticmethod
    def estimate_p_true(
        ensemble_weights: dict,
        model_score: float = 0.0,
        whale_signal: float = 0.0,
        momentum_adj: float = 0.0,
        base_market: float = 0.5,
    ) -> float:
        """Estima p_true usando ensemble ponderado (§4.2).

        Si algún componente no está disponible (valor 0), su peso se redistribuye
        al base_market para mantener la estimación anclada al precio real.
        """
        w_model = ensemble_weights.get("model_score", 0.35)
        w_whale = ensemble_weights.get("whale_signal", 0.15)
        w_momentum = ensemble_weights.get("momentum_adj", 0.20)
        w_base = ensemble_weights.get("base_market", 0.30)

        # Redistribuir pesos de señales no disponibles al base_market
        effective_w_base = w_base
        if model_score <= 0:
            effective_w_base += w_model
            w_model = 0.0
        if whale_signal <= 0:
            effective_w_base += w_whale
            w_whale = 0.0
        if momentum_adj <= 0:
            effective_w_base += w_momentum
            w_momentum = 0.0

        total_w = w_model + w_whale + w_momentum + effective_w_base
        if total_w <= 0:
            return base_market

        p_est = (
            w_model * model_score
            + w_whale * whale_signal
            + w_momentum * momentum_adj
            + effective_w_base * base_market
        ) / total_w

        return max(0.01, min(0.99, p_est))

    # ── Query ─────────────────────────────────────────────────────

    def get_strategy_state(self, name: str) -> Optional[StrategyState]:
        """Retorna el estado completo de una estrategia."""
        return self._strategies.get(name)

    def get_all_strategies(self) -> dict[str, StrategyState]:
        """Retorna todas las estrategias y sus estados."""
        return dict(self._strategies)

    def get_active_strategies(self) -> list[str]:
        """Retorna estrategias activas (no RETIRED ni FROZEN).

        v2.0: RECOVERY se considera activa (recibe asignación reducida)."""
        return [
            name for name, s in self._strategies.items()
            if s.status not in (StrategyStatus.RETIRED, StrategyStatus.FROZEN)
        ]

    def reset_strategy(self, name: str) -> bool:
        """Resetea una estrategia FROZEN/RETIRED de vuelta a PROBATION.

        Limpia los contadores de pérdidas consecutivas y épocas congeladas
        para que el Bandit vuelva a asignarle capital en el próximo ciclo.

        Parameters
        ----------
        name : str
            Nombre de la estrategia a resetear.

        Returns
        -------
        bool
            True si la estrategia existía y fue reseteada; False si no existía.
        """
        state = self._strategies.get(name)
        if state is None:
            return False

        prev_status = state.status.value
        state.status = StrategyStatus.PROBATION
        state.consecutive_losses = 0
        state.frozen_epochs = 0
        state.probation_epochs = 0
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "PortfolioManager: estrategia '%s' reseteada de [%s] → [PROBATION] "
            "(successes=%d, failures=%d, sortino_hist=%d entries)",
            name, prev_status, state.successes, state.failures, len(state.sortino_history),
        )
        return True

    def reset_all_strategies(self) -> dict[str, str]:
        """Resetea TODAS las estrategias a PROBATION con contadores de bandit limpios.

        Usar tras detectar que el Bandit fue corrompido por datos falsos (p.ej.
        P&L irreal por bug en MTM).  Los pesos de Sortino históricos se borran
        para que el Bandit parta de cero con la nueva lógica de simulación.

        Returns
        -------
        dict[str, str]
            Mapa {nombre_estrategia: status_anterior} para auditoría.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        audit: dict[str, str] = {}

        for name, state in self._strategies.items():
            prev = state.status.value
            audit[name] = prev
            state.status = StrategyStatus.PROBATION
            state.consecutive_losses = 0
            state.frozen_epochs = 0
            state.probation_epochs = 0
            state.successes = 0
            state.failures = 0
            state.sortino_history.clear()
            # v2.0: limpiar recovery tracking
            state.recovery_trades_count = 0
            state.recovery_pnl_cumulative = 0.0
            # Resetear también métricas de paper trading acumuladas
            state.total_trades = 0
            state.winning_trades = 0
            state.cumulative_pnl = 0.0
            state.max_drawdown = 0.0
            state.peak_equity = 0.0

        _log.warning(
            "🔴 BANDIT NUCLEAR RESET: %d estrategias → PROBATION (%.0f%% alloc cada una). "
            "Estado anterior: %s",
            len(audit),
            PROBATION_ALLOCATION * 100,
            audit,
        )
        return audit


    @property
    def strategy_count(self) -> int:
        return len(self._strategies)

    @property
    def current_epoch(self) -> int:
        return self._epoch_counter

    # ── Paper Trading Integration ───────────────────────────────────

    def record_trade(self, strategy_name: str, pnl: float, equity_before: float) -> None:
        """Registra un trade cerrado para una estrategia (paper trading).

        v2.0: En estado RECOVERY, acumula PnL y cuenta trades para evaluar salida.

        Parameters
        ----------
        strategy_name : str
            Nombre de la estrategia.
        pnl : float
            P&L del trade en USD.
        equity_before : float
            Equity total antes del trade (para calcular drawdown).
        """
        state = self._strategies.get(strategy_name)
        if state is None:
            return

        state.total_trades += 1
        if pnl > 0:
            state.winning_trades += 1

        state.cumulative_pnl += pnl

        # v2.0: RECOVERY tracking
        if state.status == StrategyStatus.RECOVERY:
            state.recovery_trades_count += 1
            state.recovery_pnl_cumulative += pnl
            # Evaluar salida inmediatamente si se alcanza el threshold
            self._update_strategy_status(state)

        # Actualizar peak equity y drawdown
        current_equity = equity_before + pnl
        if current_equity > state.peak_equity:
            state.peak_equity = current_equity

        dd = (state.peak_equity - current_equity) / state.peak_equity if state.peak_equity > 0 else 0.0
        if dd > state.max_drawdown:
            state.max_drawdown = dd

    def get_strategy_rankings(self, equity: float = 10000.0) -> list[dict]:
        """Retorna rankings formateados para el dashboard.

        Parameters
        ----------
        equity : float
            Capital total para calcular alloc_pct.

        Returns
        -------
        list[dict]
            Lista de dicts con keys: name, sortino, state, alloc_pct, trades, win_rate, sharpe.
        """
        allocations = self.allocate(equity)
        alloc_map = {a.strategy: a for a in allocations}

        rankings = []
        for name, state in self._strategies.items():
            alloc = alloc_map.get(name)
            alloc_pct = round(alloc.fraction * 100) if alloc else 0
            sortino = self.get_sortino(name)
            win_rate = state.winning_trades / state.total_trades if state.total_trades > 0 else 0.0
            # Sharpe aproximado usando sortino_history
            sharpe = sortino * 0.85 if sortino != 0 else 0.0

            rankings.append({
                "name": name,
                "sortino": round(sortino, 2),
                "state": state.status.value,
                "alloc_pct": alloc_pct,
                "trades": state.total_trades,
                "win_rate": round(win_rate, 2),
                "sharpe": round(sharpe, 2),
                "cumulative_pnl": round(state.cumulative_pnl, 2),
            })

        # Ordenar por sortino descendente
        rankings.sort(key=lambda x: x["sortino"], reverse=True)
        return rankings

    def get_allocation(self, equity: float = 10000.0) -> dict:
        """Retorna resumen de asignación de capital para el dashboard.

        Parameters
        ----------
        equity : float
            Capital total.

        Returns
        -------
        dict
            Keys: active, frozen, retired, total_equity, pnl_24h, pnl_24h_pct, max_drawdown, max_drawdown_pct.
        """
        allocations = self.allocate(equity)

        active = sum(a.amount for a in allocations if a.status not in ("frozen", "retired"))
        frozen = sum(a.amount for a in allocations if a.status == "frozen")
        retired = sum(a.amount for a in allocations if a.status == "retired")

        # P&L 24h: usar cumulative_pnl como proxy (en Fase 2 se puede mejorar con ventana temporal)
        total_pnl = sum(s.cumulative_pnl for s in self._strategies.values())
        max_dd = max((s.max_drawdown for s in self._strategies.values()), default=0.0)

        return {
            "active": round(active, 2),
            "frozen": round(frozen, 2),
            "retired": round(retired, 2),
            "total_equity": round(equity, 2),
            "pnl_24h": round(total_pnl, 2),
            "pnl_24h_pct": round((total_pnl / equity) * 100, 2) if equity > 0 else 0.0,
            "max_drawdown": round(-max_dd * equity, 2),
            "max_drawdown_pct": round(-max_dd * 100, 2),
        }

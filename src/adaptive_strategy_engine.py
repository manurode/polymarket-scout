"""
Adaptive Strategy Engine — Sistema de aprendizaje y adaptación para trading.

Este módulo implementa:
1. Regime Detection (trending vs ranging)
2. Dynamic Parameter Adaptation
3. Ensemble Signal Weighting
4. Auto-disable de estrategias perdedoras

Uso:
    engine = AdaptiveStrategyEngine()
    signals = engine.generate_adaptive_signals(snapshots, trade_history)
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.signal_pipeline import SignalPipeline, Signal, MarketHistory

logger = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────

REGIME_WINDOW = 20          # períodos para detectar régimen
ADAPTATION_WINDOW = 50      # trades para adaptar parámetros
MIN_TRADES_FOR_ADAPTATION = 10
CONFIDENCE_DECAY = 0.95     # decaimiento de confianza histórica

# ── FIX 3: Modo Paper Trading — umbrales hiperactivos ─────────────────────────
# Activar para observar cómo compiten todas las estrategias direccionales
# contra market_making en los logs del Bandit. Cambiar a False para live trading.
PAPER_TRADING_HYPERACTIVE = True

# Umbrales en modo HIPERACTIVO (Paper Trading)
_PT_MOMENTUM_THRESHOLD = 0.002       # 0.2%  (vs 2% en prod)
_PT_MEAN_REV_DEVIATION = 0.01        # 1%    (vs 5% en prod)
_PT_VOLUME_SPIKE_RATIO = 1.1         # 1.1x  (vs 2.5x en prod)
_PT_MIN_CONFIDENCE     = 0.05        # 5%    (vs 30% en prod)
_PT_WEIGHT_CUTOFF      = 0.05        # 5%    (vs 15% en prod)
_PT_SIGNAL_COOLDOWN    = 30          # 30s   (vs 120s en prod)

# ── M2: Tick-Size Dinámico — Paradoja del centavo ──────────────────────
# En tokens con precio < $0.10, un umbral fijo del 0.2% es matemáticamente
# imposible: 1 tick ($0.001) ya representa ~3.3%. En lugar de porcentaje,
# exigimos CONFLUENCIA: volumen anómalo + movimiento real de al menos 1 tick.
TICK_SIZE_PRICE_CUTOFF = 0.10        # Precio bajo este umbral → usar confluencia
TICK_SIZE = 0.001                    # Tick size de Polymarket (mínimo movimiento)
TICK_CONFLUENCE_VOL_RATIO = 2.5      # Vol_3min / MA_1h debe exceder este ratio
TICK_CONFLUENCE_MIN_TICKS = 1.0      # Mínimo de ticks de movimiento direccional


# ── Tipos ──────────────────────────────────────────────────────────────────

class MarketRegime:
    TRENDING = "trending"      # mercado con tendencia clara
    RANGING = "ranging"        # mercado lateral
    UNKNOWN = "unknown"        # sin datos suficientes


@dataclass
class StrategyPerformance:
    """Rendimiento histórico de una estrategia."""
    name: str
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    confidence: float = 0.5    # 0-1, confianza en la estrategia
    last_updated: float = field(default_factory=time.time)

    def update(self, pnl: float) -> None:
        """Actualiza métricas con un nuevo trade."""
        if pnl > 0:
            self.wins += 1
            self.avg_win = (self.avg_win * (self.wins - 1) + pnl) / self.wins if self.wins > 1 else pnl
        else:
            self.losses += 1
            self.avg_loss = (self.avg_loss * (self.losses - 1) + abs(pnl)) / self.losses if self.losses > 1 else abs(pnl)
        
        self.total_pnl += pnl
        total = self.wins + self.losses
        self.win_rate = self.wins / total if total > 0 else 0.0
        
        # Actualizar confianza: más trades = más confianza, win_rate positivo aumenta confianza
        target_confidence = 0.3 + (self.win_rate * 0.7)  # base 0.3 + win_rate * 0.7
        # Suavizar con histórico
        self.confidence = (self.confidence * CONFIDENCE_DECAY) + (target_confidence * (1 - CONFIDENCE_DECAY))
        self.last_updated = time.time()


@dataclass
class AdaptiveThresholds:
    """Umbrales adaptativos para una estrategia."""
    strategy: str
    momentum_threshold: float = 0.02      # 2% base
    mean_rev_deviation: float = 0.05      # 5% base
    volume_spike_ratio: float = 2.5       # 2.5x base
    min_confidence: float = 0.3
    enabled: bool = True


@dataclass
class WeightedSignal:
    """Señal con peso calculado del ensemble."""
    signal: Signal
    strategy_confidence: float           # confianza de la estrategia (0-1)
    regime_fit: float                    # qué tan bien encaja con el régimen (0-1)
    final_weight: float                  # peso final combinado


# ── Adaptive Strategy Engine ────────────────────────────────────────────────

class AdaptiveStrategyEngine:
    """
    Motor de estrategias adaptativo que:
    - Detecta el régimen del mercado (trending/ranging)
    - Ajusta parámetros según rendimiento histórico
    - Pondera señales según confianza
    - Desactiva estrategias perdedoras temporalmente
    """

    def __init__(self, state_file: str = "data/adaptive_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Componentes base
        self.pipeline = SignalPipeline()
        
        # Estado de aprendizaje
        self.strategy_perf: dict[str, StrategyPerformance] = {}
        self.adaptive_thresholds: dict[str, AdaptiveThresholds] = {}
        self.market_regimes: dict[str, str] = {}  # condition_id -> regime
        
        # Contexto externo (inyectado por el orchestrator)
        self._correlation_graph: Optional[object] = None
        self._whale_tracker: Optional[object] = None
        
        # Cargar estado previo
        self._load_state()
        
        # Inicializar umbrales adaptativos
        self._init_thresholds()

    def set_external_context(
        self,
        correlation_graph: Optional[object] = None,
        whale_tracker: Optional[object] = None,
    ) -> None:
        """Inyecta componentes externos necesarios para correlation_arb y whale_follow."""
        if correlation_graph is not None:
            self._correlation_graph = correlation_graph
        if whale_tracker is not None:
            self._whale_tracker = whale_tracker

    def _init_thresholds(self) -> None:
        """Inicializa umbrales para cada estrategia."""
        if PAPER_TRADING_HYPERACTIVE:
            # FIX 3: umbrales ultra-bajos para activar todas las estrategias en Paper Trading
            defaults = {
                "momentum": AdaptiveThresholds(
                    "momentum",
                    momentum_threshold=_PT_MOMENTUM_THRESHOLD,
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
                "mean_reversion": AdaptiveThresholds(
                    "mean_reversion",
                    mean_rev_deviation=_PT_MEAN_REV_DEVIATION,
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
                "volume_breakout": AdaptiveThresholds(
                    "volume_breakout",
                    volume_spike_ratio=_PT_VOLUME_SPIKE_RATIO,
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
                "consensus_breakout": AdaptiveThresholds(
                    "consensus_breakout",
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
                "correlation_arb": AdaptiveThresholds(
                    "correlation_arb",
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
                "whale_follow": AdaptiveThresholds(
                    "whale_follow",
                    min_confidence=_PT_MIN_CONFIDENCE,
                ),
            }
            logger.info(
                "AdaptiveEngine: modo HIPERACTIVO activado — "
                "mom_thr=%.3f  mean_rev_dev=%.3f  vol_spike=%.1fx  min_conf=%.2f",
                _PT_MOMENTUM_THRESHOLD, _PT_MEAN_REV_DEVIATION,
                _PT_VOLUME_SPIKE_RATIO, _PT_MIN_CONFIDENCE,
            )
        else:
            defaults = {
                "momentum": AdaptiveThresholds("momentum", momentum_threshold=0.02),
                "mean_reversion": AdaptiveThresholds("mean_reversion", mean_rev_deviation=0.05),
                "volume_breakout": AdaptiveThresholds("volume_breakout", volume_spike_ratio=2.5),
                "consensus_breakout": AdaptiveThresholds("consensus_breakout"),
                "correlation_arb": AdaptiveThresholds("correlation_arb"),
                "whale_follow": AdaptiveThresholds("whale_follow"),
            }
        
        for name, thresh in defaults.items():
            if PAPER_TRADING_HYPERACTIVE:
                # Modo hiperactivo: sobreescribir SIEMPRE los umbrales desde
                # el JSON persistente. Así el estado guardado es el canónico
                # y _adapt_parameters() puede modificarlo en runtime.
                self.adaptive_thresholds[name] = thresh
            elif name not in self.adaptive_thresholds:
                self.adaptive_thresholds[name] = thresh

        # Guardar inmediatamente para que adaptive_state.json refleje
        # los umbrales hiperactivos desde el primer ciclo.
        if PAPER_TRADING_HYPERACTIVE:
            self._save_state()

    def _load_state(self) -> None:
        """Carga estado previo de disco."""
        if not self.state_file.exists():
            return
        
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
            
            # Cargar performance de estrategias
            for name, perf_data in data.get('strategy_perf', {}).items():
                self.strategy_perf[name] = StrategyPerformance(**perf_data)
            
            # Cargar umbrales adaptativos
            for name, thresh_data in data.get('thresholds', {}).items():
                self.adaptive_thresholds[name] = AdaptiveThresholds(**thresh_data)
                
            logger.info(f"AdaptiveEngine: estado cargado con {len(self.strategy_perf)} estrategias")
        except Exception as e:
            logger.error(f"Error cargando estado adaptativo: {e}")

    def _save_state(self) -> None:
        """Guarda estado actual a disco."""
        try:
            data = {
                'strategy_perf': {
                    name: {
                        'name': p.name, 'wins': p.wins, 'losses': p.losses,
                        'total_pnl': p.total_pnl, 'avg_win': p.avg_win,
                        'avg_loss': p.avg_loss, 'win_rate': p.win_rate,
                        'confidence': p.confidence, 'last_updated': p.last_updated
                    }
                    for name, p in self.strategy_perf.items()
                },
                'thresholds': {
                    name: {
                        'strategy': t.strategy, 'momentum_threshold': t.momentum_threshold,
                        'mean_rev_deviation': t.mean_rev_deviation,
                        'volume_spike_ratio': t.volume_spike_ratio,
                        'min_confidence': t.min_confidence, 'enabled': t.enabled
                    }
                    for name, t in self.adaptive_thresholds.items()
                }
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error guardando estado adaptativo: {e}")

    def detect_regime(self, history: MarketHistory) -> str:
        """
        Detecta si el mercado está en tendencia o en rango.
        
        Usa el indicador ADX (Average Directional Index) simplificado:
        - ADX > 25: trending
        - ADX < 20: ranging
        """
        if len(history.prices) < REGIME_WINDOW:
            return MarketRegime.UNKNOWN
        
        prices = history.prices[-REGIME_WINDOW:]
        
        # Calcular DM+ y DM- (Directional Movement)
        dm_plus = []
        dm_minus = []
        
        for i in range(1, len(prices)):
            up_move = prices[i] - prices[i-1]
            down_move = prices[i-1] - prices[i]
            
            if up_move > down_move and up_move > 0:
                dm_plus.append(up_move)
                dm_minus.append(0)
            elif down_move > up_move and down_move > 0:
                dm_plus.append(0)
                dm_minus.append(down_move)
            else:
                dm_plus.append(0)
                dm_minus.append(0)
        
        # Calcular ATR (Average True Range) simplificado
        tr_list = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        atr = sum(tr_list) / len(tr_list) if tr_list else 0.001
        
        # Calcular DI+ y DI-
        di_plus = (sum(dm_plus) / len(dm_plus)) / atr * 100 if dm_plus else 0
        di_minus = (sum(dm_minus) / len(dm_minus)) / atr * 100 if dm_minus else 0
        
        # Calcular DX y ADX
        dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
        
        # Simplificación: usar volatilidad direccional
        total_move = abs(prices[-1] - prices[0])
        volatility = sum(tr_list) / len(tr_list) if tr_list else 0.001
        
        # Trending si el movimiento neto es significativo vs la volatilidad
        trending_score = total_move / volatility if volatility > 0 else 0
        
        if trending_score > 2.0:
            return MarketRegime.TRENDING
        elif trending_score < 1.0:
            return MarketRegime.RANGING
        else:
            return MarketRegime.UNKNOWN

    def update_from_trade(self, strategy: str, pnl: float) -> None:
        """Actualiza el sistema con el resultado de un trade."""
        if strategy not in self.strategy_perf:
            self.strategy_perf[strategy] = StrategyPerformance(name=strategy)
        
        self.strategy_perf[strategy].update(pnl)
        
        # Adaptar parámetros si tenemos suficientes trades
        self._adapt_parameters(strategy)
        
        # Guardar estado
        self._save_state()

    def _adapt_parameters(self, strategy: str) -> None:
        """Adapta los parámetros de una estrategia basándose en su rendimiento."""
        perf = self.strategy_perf.get(strategy)
        if not perf or (perf.wins + perf.losses) < MIN_TRADES_FOR_ADAPTATION:
            return
        
        thresh = self.adaptive_thresholds.get(strategy)
        if not thresh:
            return
        
        # Si win_rate < 30%, ajustar parámetros para ser más selectivo
        if perf.win_rate < 0.30:
            if strategy == "momentum":
                thresh.momentum_threshold = min(0.05, thresh.momentum_threshold * 1.1)
            elif strategy == "mean_reversion":
                thresh.mean_rev_deviation = min(0.10, thresh.mean_rev_deviation * 1.1)
            elif strategy == "volume_breakout":
                thresh.volume_spike_ratio = min(4.0, thresh.volume_spike_ratio * 1.1)
            thresh.min_confidence = min(0.6, thresh.min_confidence * 1.1)
            logger.info(f"AdaptiveEngine: {strategy} ajustado a parámetros más estrictos (win_rate={perf.win_rate:.1%})")
        
        # Si win_rate > 60%, relajar parámetros para capturar más oportunidades
        elif perf.win_rate > 0.60:
            if strategy == "momentum":
                thresh.momentum_threshold = max(0.01, thresh.momentum_threshold * 0.95)
            elif strategy == "mean_reversion":
                thresh.mean_rev_deviation = max(0.03, thresh.mean_rev_deviation * 0.95)
            elif strategy == "volume_breakout":
                thresh.volume_spike_ratio = max(1.5, thresh.volume_spike_ratio * 0.95)
            thresh.min_confidence = max(0.2, thresh.min_confidence * 0.95)
            logger.info(f"AdaptiveEngine: {strategy} ajustado a parámetros más permisivos (win_rate={perf.win_rate:.1%})")
        
        # Desactivar estrategia si está perdiendo mucho
        if perf.win_rate < 0.20 and (perf.wins + perf.losses) > 20:
            thresh.enabled = False
            logger.warning(f"AdaptiveEngine: {strategy} DESACTIVADA por bajo rendimiento")

    def should_trade(self, history: MarketHistory, signal: Signal) -> tuple[bool, str]:
        """
        Determina si se debe ejecutar una señal o no.
        
        Returns:
            (should_trade: bool, reason: str)
        """
        # 1. Verificar si la estrategia está habilitada
        thresh = self.adaptive_thresholds.get(signal.strategy)
        if thresh and not thresh.enabled:
            reason = f"Estrategia {signal.strategy} desactivada por bajo rendimiento"
            logger.warning("[DISCARD] %s | Signal=%s conf=%.2f", reason, signal.strategy, signal.confidence)
            return False, reason
        
        # 2. Verificar confianza mínima adaptativa
        if thresh and signal.confidence < thresh.min_confidence:
            reason = f"Confianza {signal.confidence:.2f} < mínimo adaptativo {thresh.min_confidence:.2f}"
            logger.info(
                "[DISCARD] LOW_CONF | Strategy=%s Token=%s | conf=%.4f < min_conf=%.4f",
                signal.strategy, signal.condition_id[:16],
                signal.confidence, thresh.min_confidence,
            )
            return False, reason
        
        # 3. Verificar fit con régimen del mercado
        regime = self.detect_regime(history)
        
        if regime == MarketRegime.TRENDING and signal.strategy == "mean_reversion":
            reason = "Mean reversion desactivado en mercado trending"
            logger.info(
                "[DISCARD] REGIME_MISMATCH | Strategy=mean_reversion Token=%s | "
                "Regime=TRENDING → mean_reversion bloqueado",
                signal.condition_id[:16],
            )
            return False, reason
        
        if regime == MarketRegime.RANGING and signal.strategy == "momentum":
            reason = "Momentum desactivado en mercado ranging"
            logger.info(
                "[DISCARD] REGIME_MISMATCH | Strategy=momentum Token=%s | "
                "Regime=RANGING → momentum bloqueado",
                signal.condition_id[:16],
            )
            return False, reason
        
        # 4. Verificar confianza histórica de la estrategia
        perf = self.strategy_perf.get(signal.strategy)
        if perf and perf.confidence < 0.2 and (perf.wins + perf.losses) > 10:
            reason = f"Confianza histórica baja ({perf.confidence:.2f})"
            logger.warning(
                "[DISCARD] LOW_HISTORICAL_CONF | Strategy=%s | "
                "historical_conf=%.2f trades=%d win_rate=%.1f%%",
                signal.strategy, perf.confidence,
                perf.wins + perf.losses, perf.win_rate * 100,
            )
            return False, reason
        
        return True, "OK"

    def calculate_signal_weight(self, signal: Signal, regime: str) -> WeightedSignal:
        """Calcula el peso final de una señal para el ensemble.

        Para estrategias en cold-start (menos de MIN_TRADES_FOR_ADAPTATION trades),
        usa un prior Bayesiano generoso de 0.75 en vez de castigarlas con 0.5.
        Esto evita el círculo vicioso donde una estrategia sin historial nunca
        opera porque su peso base es demasiado bajo."""
        # Confianza de la estrategia
        perf = self.strategy_perf.get(signal.strategy)
        total_trades = (perf.wins + perf.losses) if perf else 0
        if total_trades < MIN_TRADES_FOR_ADAPTATION:
            strategy_conf = 0.75  # prior generoso para cold-start
        else:
            strategy_conf = perf.confidence
        
        # Fit con el régimen
        regime_fit = 0.5
        if regime == MarketRegime.TRENDING and signal.strategy == "momentum":
            regime_fit = 1.0
        elif regime == MarketRegime.RANGING and signal.strategy == "mean_reversion":
            regime_fit = 1.0
        elif regime == MarketRegime.UNKNOWN:
            regime_fit = 0.7  # neutral
        else:
            regime_fit = 0.3  # mala combinación
        
        # Peso final: combinación de confianza, confianza histórica y fit de régimen
        final_weight = signal.confidence * strategy_conf * regime_fit
        
        return WeightedSignal(
            signal=signal,
            strategy_confidence=strategy_conf,
            regime_fit=regime_fit,
            final_weight=final_weight
        )

    def generate_adaptive_signals(
        self,
        snapshots: list[dict],
        cooldown_s: float | None = None,
        max_signals: int = 5
    ) -> list[Signal]:
        """
        Genera señales usando el pipeline base pero con filtrado adaptativo.
        """
        # Cooldown: en modo hiperactivo usar el PT cooldown si no se especifica
        if cooldown_s is None:
            cooldown_s = _PT_SIGNAL_COOLDOWN if PAPER_TRADING_HYPERACTIVE else 120
        
        # Actualizar historial primero
        self.pipeline.update_history(snapshots)
        
        all_weighted_signals: list[WeightedSignal] = []
        
        for snap in snapshots:
            cid = snap.get("condition_id", "")
            if not cid:
                continue
            
            history = self.pipeline._history.get(cid)
            if not history or len(history.prices) < 4:
                continue
            
            # Detectar régimen
            regime = self.detect_regime(history)
            self.market_regimes[cid] = regime
            
            # Generar señales crudas usando el pipeline base
            raw_signals = self._generate_raw_signals(history, snap, cid)
            
            for signal in raw_signals:
                # Verificar si deberíamos trade
                should_trade, reason = self.should_trade(history, signal)
                
                if not should_trade:
                    logger.debug(
                        "DEBUG: Strategy [%s] generated signal for [%s] but was ignored due to [%s]",
                        signal.strategy, signal.market[:45], reason,
                    )
                    continue
                
                # Calcular peso ponderado
                weighted = self.calculate_signal_weight(signal, regime)
                
                # Solo incluir si el peso final es suficiente
                # Usar el cutoff correcto según modo (no hardcodear 0.15)
                _signal_weight_cutoff = _PT_WEIGHT_CUTOFF if PAPER_TRADING_HYPERACTIVE else 0.15
                if weighted.final_weight > _signal_weight_cutoff:
                    all_weighted_signals.append(weighted)
                else:
                    logger.debug(
                        "DEBUG: [WEIGHT_DISCARD] Strategy=%s Token=%s | final_weight=%.4f < cutoff=%.4f | "
                        "conf=%.2f strategy_conf=%.2f regime_fit=%.2f",
                        signal.strategy, cid[:16],
                        weighted.final_weight, _signal_weight_cutoff,
                        signal.confidence, weighted.strategy_confidence, weighted.regime_fit,
                    )
        
        # ── 5. Correlation Arbitrage: una vez por ciclo, no por snapshot ──
        t = self.adaptive_thresholds.get("correlation_arb")
        if t and t.enabled and self._correlation_graph is not None:
            try:
                # MAINNET ALIGNMENT: hurdle 8% (realista para Polymarket)
                # En vez de 20% que solo cazaba unicornios.
                arb_opps = self._correlation_graph.find_arbitrage_opportunities(
                    hurdle_rate=0.08
                )
                for opp in arb_opps:
                    if not opp.meets_hurdle:
                        continue
                    for cid in opp.markets:
                        history = self.pipeline._history.get(cid)
                        if not history or len(history.prices) < 4:
                            continue
                        conf = min(opp.gross_profit_pct / 10.0, 0.85)
                        if conf <= t.min_confidence:
                            continue
                        signal = Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="correlation_arb",
                            side="YES" if opp.gross_profit_pct > 0 else "NO",
                            entry_price=history.current_price or 0.5,
                            confidence=round(conf, 2),
                            reason=f"Arb {opp.type} profit={opp.gross_profit_pct:.1%} annual={opp.annualized_return:.1%}",
                        )
                        ok, reason = self.should_trade(history, signal)
                        if ok:
                            regime = self.market_regimes.get(cid, MarketRegime.UNKNOWN)
                            weighted = self.calculate_signal_weight(signal, regime)
                            if weighted.final_weight > _signal_weight_cutoff:
                                all_weighted_signals.append(weighted)
            except Exception as e:
                logger.debug("Correlation arb signal error: %s", e)

        # ── 6. Whale Follow: una vez por ciclo ──
        t = self.adaptive_thresholds.get("whale_follow")
        if t and t.enabled and self._whale_tracker is not None:
            for snap in snapshots:
                cid = snap.get("condition_id", "")
                if not cid:
                    continue
                history = self.pipeline._history.get(cid)
                if not history or len(history.prices) < 4:
                    continue
                try:
                    flow = self._whale_tracker.get_whale_flow(cid)
                    if not flow or flow.timestamp <= 0:
                        continue
                    if flow.active_whales < 2 or flow.whale_consensus <= 0.6:
                        continue
                    side = "YES" if flow.bullish_whales > flow.bearish_whales else "NO"
                    zscore_norm = min(abs(flow.whale_zscore) / 3.0, 1.0)
                    conf = min(
                        zscore_norm * 0.4 + flow.whale_consensus * 0.4 + min(flow.active_whales / 10.0, 1.0) * 0.2,
                        0.85,
                    )
                    if conf <= t.min_confidence:
                        continue
                    signal = Signal(
                        market=history.question[:60],
                        question=history.question,
                        condition_id=cid,
                        strategy="whale_follow",
                        side=side,
                        entry_price=history.current_price or 0.5,
                        confidence=round(conf, 2),
                        reason=(
                            f"Whales: {flow.active_whales} activas "
                            f"({flow.bullish_whales}B/{flow.bearish_whales}S) "
                            f"consenso={flow.whale_consensus:.0%} z={flow.whale_zscore:.1f}"
                        ),
                    )
                    ok, reason = self.should_trade(history, signal)
                    if ok:
                        regime = self.market_regimes.get(cid, MarketRegime.UNKNOWN)
                        weighted = self.calculate_signal_weight(signal, regime)
                        if weighted.final_weight > _signal_weight_cutoff:
                            all_weighted_signals.append(weighted)
                except Exception as e:
                    logger.debug("Whale follow signal error for %s: %s", cid[:16], e)

        # Ordenar por peso final y limitar
        all_weighted_signals.sort(key=lambda w: w.final_weight, reverse=True)
        
        # Peso mínimo — reducido en modo hiperactivo para dejar pasar más señales
        weight_cutoff = _PT_WEIGHT_CUTOFF if PAPER_TRADING_HYPERACTIVE else 0.15
        all_weighted_signals = [w for w in all_weighted_signals if w.final_weight > weight_cutoff]
        
        # Aplicar cooldown
        now = time.time()
        result = []
        for ws in all_weighted_signals[:max_signals]:
            cid = ws.signal.condition_id
            if cid in self.pipeline._last_signal_time:
                elapsed = now - self.pipeline._last_signal_time[cid]
                if elapsed < cooldown_s:
                    remaining = cooldown_s - elapsed
                    logger.info(
                        "[COOLDOWN_BLOCK] Token=%s Strategy=%s | "
                        "elapsed=%.1fs remaining=%.1fs cooldown=%.0fs",
                        cid[:16], ws.signal.strategy,
                        elapsed, remaining, cooldown_s,
                    )
                    continue
            self.pipeline._last_signal_time[cid] = now
            result.append(ws.signal)
        
        return result[:max_signals]

    def _generate_raw_signals(
        self,
        history: MarketHistory,
        snap: dict,
        cid: str
    ) -> list[Signal]:
        """Genera señales usando los umbrales adaptativos actuales."""
        signals = []
        thresh = self.adaptive_thresholds
        
        # 1. Momentum con umbral adaptativo (o confluencia para precios < $0.10)
        mom = history.momentum
        t = thresh.get("momentum")
        if t and t.enabled and mom is not None:
            threshold = t.momentum_threshold
            current_price = history.current_price or 0.5
            min_tick_impact = (TICK_SIZE / current_price) if current_price > 0 else float('inf')

            # ── M2: Tick-Size Dinámico — Confluencia para precios < TICK_SIZE_PRICE_CUTOFF ──
            use_confluence = current_price > 0 and current_price < TICK_SIZE_PRICE_CUTOFF

            if use_confluence:
                # Condición A (Volumen): vol_3min > 2.5× MA_1h
                vol_ma = history.recent_volume_ma
                vol_3min = history.recent_volume_3min
                cond_vol = (
                    vol_ma is not None and vol_ma > 0
                    and (vol_3min / vol_ma) >= TICK_CONFLUENCE_VOL_RATIO
                )
                # Condición B (Precio): movimiento ≥ 1 tick en la dirección de la tendencia
                price_move_tickworthy = abs(mom) >= min_tick_impact

                if cond_vol and price_move_tickworthy:
                    side = "YES" if mom > 0 else "NO"
                    conf = min(abs(mom) / (min_tick_impact * 3), 0.9)
                    if conf > t.min_confidence:
                        signals.append(Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="momentum",
                            side=side,
                            entry_price=current_price,
                            confidence=round(conf, 2),
                            reason=(
                                f"TickConfluence mom={mom:+.1%} tick_impact={min_tick_impact:.1%} "
                                f"vol={vol_3min:.0f}/{vol_ma:.0f}={vol_3min/vol_ma:.1f}x"
                            ),
                        ))
                    tick_status = "VALID (CONFLUENCE)"
                else:
                    reason_parts = []
                    if not cond_vol:
                        reason_parts.append(f"volume={vol_3min/vol_ma:.1f}x < {TICK_CONFLUENCE_VOL_RATIO}x" if vol_ma and vol_ma > 0 else "vol_ma=None")
                    if not price_move_tickworthy:
                        reason_parts.append(f"mom={abs(mom):.6f} < tick_impact={min_tick_impact:.6f}")
                    tick_status = f"INSIGNIFICANT (confluence failed: {'; '.join(reason_parts)})"
                    logger.debug(
                        "[SKIP_DIR] Token=%s | Reason=Tick_Size_Conflict "
                        "(Threshold=%.6f < Min_Tick=%.6f, Price=$%.4f) | "
                        "vol_3min=%.0f vol_MA=%.0f cond_vol=%s cond_price=%s",
                        cid[:16], abs(mom), min_tick_impact, current_price,
                        vol_3min, vol_ma or 0, cond_vol, price_move_tickworthy,
                    )

                logger.debug(
                    "[STRATEGY_SIG] Momentum Token=%s | mom=%.6f | "
                    "threshold=%.6f | min_tick_impact=%.6f (1/price=%.3f) | "
                    "confluence=%s | Status=%s",
                    cid[:16], mom, threshold,
                    min_tick_impact, current_price,
                    "ON" if use_confluence else "OFF",
                    tick_status,
                )
            else:
                # ── Ruta clásica (precio ≥ $0.10): umbral porcentual fijo ──
                if abs(mom) > threshold:
                    side = "YES" if mom > 0 else "NO"
                    conf = min(abs(mom) / (threshold * 3), 0.9)
                    if conf > t.min_confidence:
                        signals.append(Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="momentum",
                            side=side,
                            entry_price=history.current_price or 0,
                            confidence=round(conf, 2),
                            reason=f"Momentum {mom:+.1%} (umbral: {threshold:.1%})",
                        ))
                tick_status = "INSIGNIFICANT" if abs(mom) < min_tick_impact else "VALID"
                logger.debug(
                    "[STRATEGY_SIG] Momentum Token=%s | mom=%.6f | "
                    "threshold=%.6f | min_tick_impact=%.6f (1/price=%.3f) | "
                    "confluence=OFF | Status=%s",
                    cid[:16], mom, threshold,
                    min_tick_impact, current_price,
                    tick_status,
                )
        
        # 2. Mean Reversion con umbral adaptativo (o confluencia para precios < $0.10)
        t = thresh.get("mean_reversion")
        if t and t.enabled and history.ma and history.current_price and len(history.prices) >= 8:
            deviation = (history.current_price - history.ma) / history.ma if history.ma > 0 else 0
            threshold = t.mean_rev_deviation
            # ── Tick Size Check ──
            current_price = history.current_price
            min_tick_dev = (TICK_SIZE / current_price) if current_price > 0 else float('inf')
            use_confluence = current_price > 0 and current_price < TICK_SIZE_PRICE_CUTOFF

            if use_confluence:
                # Condición A (Volumen): vol_3min > 2.5× MA_1h
                vol_ma = history.recent_volume_ma
                vol_3min = history.recent_volume_3min
                cond_vol = (
                    vol_ma is not None and vol_ma > 0
                    and (vol_3min / vol_ma) >= TICK_CONFLUENCE_VOL_RATIO
                )
                # Condición B (Precio): desviación ≥ 1 tick
                price_dev_tickworthy = abs(deviation) >= min_tick_dev

                if cond_vol and price_dev_tickworthy:
                    side = "NO" if deviation > 0 else "YES"
                    conf = min(abs(deviation) / (min_tick_dev * 2), 0.8)
                    if conf > t.min_confidence:
                        signals.append(Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="mean_reversion",
                            side=side,
                            entry_price=current_price,
                            confidence=round(conf, 2),
                            reason=(
                                f"TickConfluence dev={deviation:+.1%} tick_dev={min_tick_dev:.1%} "
                                f"vol={vol_3min:.0f}/{vol_ma:.0f}={vol_3min/vol_ma:.1f}x"
                            ),
                        ))
                    tick_status_mr = "VALID (CONFLUENCE)"
                else:
                    reason_parts = []
                    if not cond_vol:
                        reason_parts.append(f"volume insensitive")
                    if not price_dev_tickworthy:
                        reason_parts.append(f"dev={abs(deviation):.6f} < tick_dev={min_tick_dev:.6f}")
                    tick_status_mr = f"INSIGNIFICANT (confluence failed)"
                    logger.debug(
                        "[SKIP_DIR] Token=%s | Reason=Tick_Size_Conflict_MR "
                        "(Deviation=%.6f < Min_Tick=%.6f, Price=$%.4f) | "
                        "vol_3min=%.0f vol_MA=%.0f",
                        cid[:16], abs(deviation), min_tick_dev, current_price,
                        vol_3min, vol_ma or 0,
                    )

                logger.debug(
                    "[STRATEGY_SIG] MeanReversion Token=%s | dev=%.6f | "
                    "threshold=%.6f | min_tick_impact=%.6f | "
                    "price=%.4f MA=%.4f | confluence=%s | Status=%s",
                    cid[:16], deviation, threshold, min_tick_dev,
                    current_price, history.ma,
                    "ON" if use_confluence else "OFF",
                    tick_status_mr,
                )
            else:
                # ── Ruta clásica (precio ≥ $0.10): umbral porcentual fijo ──
                if abs(deviation) > threshold:
                    side = "NO" if deviation > 0 else "YES"
                    conf = min(abs(deviation) / (threshold * 2), 0.8)
                    if conf > t.min_confidence:
                        signals.append(Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="mean_reversion",
                            side=side,
                            entry_price=current_price,
                            confidence=round(conf, 2),
                            reason=f"Reversion {deviation:+.1%} de MA (umbral: {threshold:.1%})",
                        ))
                tick_status_mr = "INSIGNIFICANT" if abs(deviation) < min_tick_dev else "VALID"
                logger.debug(
                    "[STRATEGY_SIG] MeanReversion Token=%s | dev=%.6f | "
                    "threshold=%.6f | min_tick_impact=%.6f | "
                    "price=%.4f MA=%.4f | confluence=OFF | Status=%s",
                    cid[:16], deviation, threshold, min_tick_dev,
                    current_price, history.ma,
                    tick_status_mr,
                )
        
        # 3. Volume Breakout con umbral adaptativo
        t = thresh.get("volume_breakout")
        if t and t.enabled:
            vol_spike = history.volume_spike
            if vol_spike is not None:
                threshold = t.volume_spike_ratio
                if vol_spike > threshold:
                    mom_val = history.momentum
                    side = "YES"
                    if mom_val is not None and mom_val < -0.01:
                        side = "NO"
                    conf = min((vol_spike - threshold) / (threshold * 2), 0.7)
                    if conf > t.min_confidence:
                        signals.append(Signal(
                            market=history.question[:60],
                            question=history.question,
                            condition_id=cid,
                            strategy="volume_breakout",
                            side=side,
                            entry_price=history.current_price or 0,
                            confidence=round(conf, 2),
                            reason=f"Volumen {vol_spike:.1f}x (umbral: {threshold:.1f}x)",
                        ))

        # 4. Consensus Breakout — ensemble de las 3 estrategias base
        # Si 2+ estrategias generan señal en la MISMA dirección para este mercado,
        # emitimos una señal de consenso con confianza potenciada.
        t = thresh.get("consensus_breakout")
        if t and t.enabled and len(signals) >= 2:
            sides = [s.side for s in signals if s.strategy in ("momentum", "mean_reversion", "volume_breakout")]
            if len(sides) >= 2 and len(set(sides)) == 1:
                # Todas coinciden en dirección → consenso
                best_conf = max(s.confidence for s in signals if s.strategy in ("momentum", "mean_reversion", "volume_breakout"))
                consensus_conf = min(best_conf * 1.3, 0.95)  # boost del 30%, cap 0.95
                if consensus_conf > t.min_confidence:
                    active_strats = [s.strategy for s in signals if s.strategy in ("momentum", "mean_reversion", "volume_breakout")]
                    signals.append(Signal(
                        market=history.question[:60],
                        question=history.question,
                        condition_id=cid,
                        strategy="consensus_breakout",
                        side=sides[0],
                        entry_price=history.current_price or 0,
                        confidence=round(consensus_conf, 2),
                        reason=f"Consenso {len(sides)}/3 estrategias ({', '.join(active_strats)}) → {sides[0]}",
                    ))

        # 5-6: correlation_arb y whale_follow se generan en generate_adaptive_signals()
        #       (una vez por ciclo, no por snapshot) para evitar bloquear el event loop.

        return signals

    def get_status_report(self) -> dict:
        """Genera un reporte del estado del sistema adaptativo."""
        return {
            "strategies": {
                name: {
                    "enabled": t.enabled,
                    "thresholds": {
                        "momentum": t.momentum_threshold,
                        "mean_rev": t.mean_rev_deviation,
                        "volume": t.volume_spike_ratio,
                    },
                    "min_confidence": t.min_confidence,
                }
                for name, t in self.adaptive_thresholds.items()
            },
            "performance": {
                name: {
                    "trades": p.wins + p.losses,
                    "win_rate": round(p.win_rate * 100, 1),
                    "total_pnl": round(p.total_pnl, 2),
                    "confidence": round(p.confidence, 2),
                }
                for name, p in self.strategy_perf.items()
            },
            "regime_distribution": {
                "trending": sum(1 for r in self.market_regimes.values() if r == MarketRegime.TRENDING),
                "ranging": sum(1 for r in self.market_regimes.values() if r == MarketRegime.RANGING),
                "unknown": sum(1 for r in self.market_regimes.values() if r == MarketRegime.UNKNOWN),
            }
        }

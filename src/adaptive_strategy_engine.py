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
        
        # Cargar estado previo
        self._load_state()
        
        # Inicializar umbrales adaptativos
        self._init_thresholds()

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
            }
        
        for name, thresh in defaults.items():
            if name not in self.adaptive_thresholds:
                self.adaptive_thresholds[name] = thresh

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
            return False, f"Estrategia {signal.strategy} desactivada por bajo rendimiento"
        
        # 2. Verificar confianza mínima adaptativa
        if thresh and signal.confidence < thresh.min_confidence:
            return False, f"Confianza {signal.confidence:.2f} < mínimo adaptativo {thresh.min_confidence:.2f}"
        
        # 3. Verificar fit con régimen del mercado
        regime = self.detect_regime(history)
        
        if regime == MarketRegime.TRENDING and signal.strategy == "mean_reversion":
            return False, "Mean reversion desactivado en mercado trending"
        
        if regime == MarketRegime.RANGING and signal.strategy == "momentum":
            return False, "Momentum desactivado en mercado ranging"
        
        # 4. Verificar confianza histórica de la estrategia
        perf = self.strategy_perf.get(signal.strategy)
        if perf and perf.confidence < 0.2 and (perf.wins + perf.losses) > 10:
            return False, f"Confianza histórica baja ({perf.confidence:.2f})"
        
        return True, "OK"

    def calculate_signal_weight(self, signal: Signal, regime: str) -> WeightedSignal:
        """Calcula el peso final de una señal para el ensemble."""
        # Confianza de la estrategia
        perf = self.strategy_perf.get(signal.strategy)
        strategy_conf = perf.confidence if perf else 0.5
        
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
                    logger.debug(f"Señal filtrada: {signal.strategy} en {signal.market[:30]} - {reason}")
                    continue
                
                # Calcular peso ponderado
                weighted = self.calculate_signal_weight(signal, regime)
                
                # Solo incluir si el peso final es suficiente
                if weighted.final_weight > 0.15:
                    all_weighted_signals.append(weighted)
        
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
                if now - self.pipeline._last_signal_time[cid] < cooldown_s:
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
        
        # 1. Momentum con umbral adaptativo
        mom = history.momentum
        t = thresh.get("momentum")
        if t and t.enabled and mom is not None:
            threshold = t.momentum_threshold
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
        
        # 2. Mean Reversion con umbral adaptativo
        t = thresh.get("mean_reversion")
        if t and t.enabled and history.ma and history.current_price and len(history.prices) >= 8:
            deviation = (history.current_price - history.ma) / history.ma if history.ma > 0 else 0
            threshold = t.mean_rev_deviation
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
                        entry_price=history.current_price,
                        confidence=round(conf, 2),
                        reason=f"Reversion {deviation:+.1%} de MA (umbral: {threshold:.1%})",
                    ))
        
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

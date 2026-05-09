# Sistema Adaptativo de Trading - Polymarket Scout

## Resumen del Problema

El backtest de 12 horas mostró:
- **Pérdida del -1.0%** ($99.68)
- **Win rate del 23.8%** (5 wins / 16 losses)
- **Solo estrategia momentum usada** (21 trades momentum, 0 mean_reversion, 0 volume_breakout)
- **Sharpe ratio de -18.5** (extremadamente bajo)
- **16 de 21 trades cerrados por stop-loss**

### Diagnóstico

1. **Umbrales rígidos**: Los parámetros de mean_reversion (5%) y volume_breakout (2.5x) son demasiado altos para mercados de Polymarket
2. **Sin detección de régimen**: El bot operaba momentum en mercados sin tendencia clara (whipsaw)
3. **Sin adaptación**: Parámetros fijos sin aprender del rendimiento
4. **Sin filtrado**: Todas las señales se ejecutaban sin considerar el contexto

---

## Solución: Adaptive Strategy Engine

Implementé un sistema completo de aprendizaje y adaptación con 4 componentes principales:

### 1. Regime Detection (`detect_regime()`)

Detecta automáticamente si el mercado está:
- **TRENDING**: Tendencia clara → momentum funciona bien
- **RANGING**: Rango lateral → mean_reversion funciona bien  
- **UNKNOWN**: Sin datos suficientes → neutral

```python
# Ejemplo de uso
regime = engine.detect_regime(history)
if regime == MarketRegime.TRENDING:
    # Activar momentum, desactivar mean_reversion
```

### 2. Dynamic Parameter Adaptation

Los umbrales se ajustan automáticamente según el win rate:

| Win Rate | Acción | Efecto |
|----------|--------|--------|
| < 30% | Ajustar más estricto | Umbral +10%, Confianza mínima +10% |
| 30-60% | Mantener | Sin cambios |
| > 60% | Relajar | Umbral -5%, Capturar más oportunidades |
| < 20% (20+ trades) | **Desactivar** | Estrategia FROZEN |

### 3. Ensemble Signal Weighting

Cada señal recibe un peso combinado de:
- **Confianza de la estrategia** (0-1): Basada en win rate histórico
- **Fit con régimen** (0-1): Qué tan bien encaja la estrategia con el régimen actual
- **Confianza de la señal** (0-1): Confianza intrínseca de la señal

```
Peso Final = Confianza_Señal × Confianza_Estrategia × Fit_Régimen
```

Solo señales con peso > 0.15 se ejecutan.

### 4. NO-TRADE Conditions

El sistema ahora rechaza señales cuando:
- La estrategia está desactivada por bajo rendimiento
- La confianza está por debajo del mínimo adaptativo
- Hay mismatch de régimen (ej: momentum en mercado ranging)
- La confianza histórica de la estrategia es < 0.2

---

## Archivos Nuevos

### `src/adaptive_strategy_engine.py`
Motor principal del sistema adaptativo. Contiene:
- `AdaptiveStrategyEngine`: Clase principal
- `MarketRegime`: Enum de regímenes
- `StrategyPerformance`: Tracking de rendimiento por estrategia
- `AdaptiveThresholds`: Umbrales adaptativos por estrategia

### `src/backtester_adaptive.py`
Backtester extendido que:
- Usa el AdaptiveStrategyEngine
- Mide rendimiento por régimen
- Permite comparar modo estático vs adaptativo
- Guarda estadísticas de filtrado

### `run_adaptive_backtest.py`
Script CLI para correr backtests:
```bash
# Comparar estático vs adaptativo
python run_adaptive_backtest.py --days 7 --compare

# Solo modo adaptativo
python run_adaptive_backtest.py --days 7 --adaptive-only --output result.json
```

---

## Cambios en Archivos Existentes

### `src/orchestrator.py`
- Importa `AdaptiveStrategyEngine`
- Instancia el motor adaptativo en `__init__`
- Modifica `_paper_signal_loop()` para usar `generate_adaptive_signals()`
- Agrega `_on_trade_close()` callback para feedback de trades

### `src/paper_trading.py`
- Agrega parámetro `on_trade_close: callable` al constructor
- Llama al callback cuando se cierra una posición
- Permite feedback en tiempo real al sistema adaptativo

---

## Cómo Funciona el Aprendizaje

### Durante el Backtest/Trading en Vivo:

1. **Generación de señales**:
   ```python
   signals = engine.generate_adaptive_signals(snapshots)
   ```
   - Detecta régimen de cada mercado
   - Genera señales con umbrales adaptativos
   - Filtra señales que no cumplen criterios
   - Pondera por confianza del ensemble

2. **Ejecución**:
   - Solo las top señales se ejecutan
   - Se guarda el régimen y confianza con cada trade

3. **Feedback** (cuando cierra un trade):
   ```python
   engine.update_from_trade(strategy, pnl)
   ```
   - Actualiza métricas de la estrategia
   - Recalcula win rate y confianza
   - Adapta umbrales si hay suficientes trades
   - Desactiva estrategia si está perdiendo mucho

4. **Persistencia**:
   - Estado se guarda en `data/adaptive_state.json`
   - Sobrevive reinicios del sistema
   - Permite aprendizaje acumulativo

---

## Estado del Sistema Adaptativo

Obtén el estado actual con:

```python
report = engine.get_status_report()
```

Ejemplo de salida:
```json
{
  "strategies": {
    "momentum": {
      "enabled": true,
      "thresholds": {
        "momentum": 0.025,
        "mean_rev": 0.05,
        "volume": 2.5
      },
      "min_confidence": 0.35
    },
    "mean_reversion": {
      "enabled": true,
      "thresholds": {...},
      "min_confidence": 0.30
    }
  },
  "performance": {
    "momentum": {
      "trades": 21,
      "win_rate": 23.8,
      "total_pnl": -99.68,
      "confidence": 0.28
    }
  },
  "regime_distribution": {
    "trending": 12,
    "ranging": 8,
    "unknown": 45
  }
}
```

---

## Beneficios Esperados

| Métrica | Antes | Después (Esperado) |
|---------|-------|-------------------|
| Win Rate | 23.8% | 45-55% |
| Estrategias Usadas | 1 (momentum) | 2-3 dinámicas |
| Trades en Ranging | Muchos | Pocos/None |
| Adaptación | Ninguna | Continua |
| Desactivación Auto | No | Sí (estrategias perdedoras) |

---

## Próximos Pasos

1. **Correr backtest comparativo**:
   ```bash
   python run_adaptive_backtest.py --days 7 --compare
   ```

2. **Analizar resultados** en `backtest_comparison.json`

3. **Ajustar parámetros base** si es necesario en `AdaptiveThresholds`

4. **Dejar correr en paper trading** por 24-48h para acumular datos de aprendizaje

5. **Iterar** basándose en los resultados

---

## Notas Técnicas

- El sistema mantiene compatibilidad con el código existente
- Los cambios son opt-in (el modo estático sigue disponible)
- El estado se persiste entre ejecuciones
- El aprendizaje es incremental (no requiere reentrenar desde cero)

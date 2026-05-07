# Fase 2: Paper Trading + Backtesting

**Goal:** Simular apuestas y evaluar estrategias contra datos históricos para saber qué patrones de señales predicen buenas oportunidades.

**Architecture:**
```
Datos históricos (SQLite) → Backtester → Strategy → PaperTrader → Métricas (ROI, Sharpe, win rate)
                                                         ↓
                                                  Portfolio tracker
```

**Nuevos módulos:**
- `src/paper_trader.py` — engine de paper trading (balance virtual, posiciones, P&L)
- `src/strategies.py` — definiciones de estrategias de apuesta
- `src/backtester.py` — replay histórico + evaluación de estrategias

---

## DB Schema (añadir a tracker.py)

```sql
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    question TEXT,
    side TEXT NOT NULL,           -- 'YES' or 'NO'
    amount REAL NOT NULL,         -- USDC amount
    price REAL NOT NULL,          -- price at entry
    shares REAL NOT NULL,         -- amount / price
    signal_type TEXT,             -- what triggered this trade
    score INTEGER,                -- score at entry
    status TEXT DEFAULT 'open',   -- open, closed
    entry_timestamp INTEGER NOT NULL,
    close_price REAL,
    close_timestamp INTEGER,
    pnl REAL,                     -- profit/loss in USDC
    strategy TEXT                 -- which strategy placed this trade
);
```

## Estrategias iniciales

| Estrategia | Regla | Lógica |
|-----------|-------|--------|
| `momentum_yes` | momentum_up + score ≥ 40 | Si el precio sube rápido, apostar YES (seguir tendencia) |
| `momentum_no` | momentum_down + score ≥ 40 | Si el precio baja rápido, apostar NO |
| `contrarian` | momentum_down + volume_spike + score ≥ 50 | Si baja con mucho volumen, apostar YES (comprar el miedo) |
| `consensus_yes` | spread_tight + momentum_up + score ≥ 45 | Consenso fuerte + precio subiendo → YES |
| `breakout` | volume_spike + spread_wide + score ≥ 50 | Mucho volumen + desacuerdo → apostar YES (entrada temprana) |

## Paper Trader

- Balance inicial: $1000 USDC
- Tamaño de posición: 5% del balance por trade (configurable)
- Cierre automático: cuando el mercado resuelve o tras N días
- Tracking: P&L, posiciones abiertas, historial

## Backtester

- Itera snapshots históricos en orden cronológico
- Para cada snapshot: ejecuta todas las estrategias
- Si estrategia dispara → simula trade (paper_trader.place_bet)
- Al resolver mercado → cierra posición con resultado
- Genera informe: ROI total, win rate, profit factor, Sharpe, max drawdown, mejores/peores estrategias

## CLI

```
python -m src.cli backtest          # backtest todas las estrategias
python -m src.cli backtest --strategy momentum_yes  # una estrategia
python -m src.cli portfolio          # ver balance y posiciones
python -m src.cli paper-trade --market <slug> --side YES --amount 50  # trade manual
```

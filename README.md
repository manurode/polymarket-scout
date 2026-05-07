# Polymarket Scout 🎯

Bot de monitorización de Polymarket. Escanea mercados de predicción cada 5 minutos, detecta oportunidades mediante análisis de señales (momentum, volumen, spread) y envía alertas por Telegram cuando encuentra algo con alto score de confianza.

**Repo:** https://github.com/manurode/polymarket-scout

---

## 🏗️ Arquitectura

```
CRON (cada 5 min) → Scanner → Tracker → Signals → Scorer → Alerter → Telegram
```

| Módulo | Archivo | Función |
|--------|---------|---------|
| Scanner | `src/scanner.py` | Cliente HTTP para APIs de Polymarket (Gamma, CLOB) |
| Tracker | `src/tracker.py` | Persistencia SQLite de snapshots y señales |
| Signals | `src/signals.py` | Detectores de anomalías (momentum, volumen, spread) |
| Scorer | `src/scorer.py` | Score de confianza 0–100 basado en intensidad de señales |
| Alerter | `src/alerter.py` | Formateo de alertas para Telegram |
| CLI | `src/cli.py` | Orquestador del pipeline completo |

---

## 🔍 Señales detectadas

| Señal | Descripción | Peso |
|-------|-------------|------|
| 🚀 `momentum_up` | Precio YES subiendo >5% | 20 |
| 📉 `momentum_down` | Precio YES bajando >5% | 20 |
| 📊 `volume_spike` | Volumen 3× por encima de la media | 20 |
| 🤝 `spread_tight` | Spread <3% (consenso fuerte) | 15 |
| 🌈 `spread_wide` | Spread >10% (desacuerdo/oportunidad) | 10 |
| 🆕 `new_interest` | Mercado nuevo con volumen inicial alto | 10 |

---

## 🚀 Uso

```bash
# Instalar dependencias
pip install -r requirements.txt

# Scan completo (detectar + alertar)
python -m src.cli scan

# Scan con config personalizada
python -m src.cli scan --config mi_config.yaml

# Tests
python -m pytest tests/ -v
```

---

## ⚙️ Configuración

Editar `config.yaml`:

```yaml
scanner:
  events_limit: 10        # cuántos eventos consultar
  markets_per_event: 5    # máximo mercados por evento
  min_volume: 5000        # ignorar mercados con <$5K

signals:
  momentum:
    threshold: 0.05       # 5% de cambio mínimo
    window_hours: 1       # ventana de comparación
  volume_spike:
    threshold: 3.0        # 3× la media
  spread:
    tight_threshold: 0.03 # spread <3%
    wide_threshold: 0.10  # spread >10%

scorer:
  alert_threshold: 60     # score mínimo para disparar alerta
  cooldown_minutes: 30    # no repetir alerta mismo mercado en 30min
```

---

## 📊 Datos

- ~200 mercados monitorizados por scan
- Datos históricos en `data/polymarket.db` (SQLite)
- Retención: 90 días (configurable)

---

## 🗺️ Roadmap

- [x] **Fase 1** — Scout automático con alertas (actual)
- [ ] **Fase 2** — Paper trading + backtesting
- [ ] **Fase 3** — Trading real con wallet Polygon + USDC

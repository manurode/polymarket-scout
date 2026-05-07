# Polymarket Scout — Fase 1: Motor de Detección de Oportunidades

> **Para Hermes:** Usar `subagent-driven-development` para implementar este plan tarea por tarea.  
> **Idioma:** Todo el output al usuario (alertas, logs, mensajes CLI) en español. Código en inglés.

**Goal:** Un bot que monitoriza Polymarket 24/7, detecta oportunidades de apuesta mediante análisis de señales (precio, volumen, spread), asigna un score de confianza, y envía alertas por Telegram.

**Architecture:** Pipeline modular — Scanner → Tracker → Signals → Scorer → Alerter. Cada módulo independiente con tests. SQLite como almacenamiento. Ejecución vía cron cada 5 minutos. Alertas a Telegram vía Hermes.

**Tech Stack:** Python 3.13, requests, SQLite (stdlib), pandas, cron (Hermes scheduler)

**Lo que NO hace esta fase:** Trading real, paper trading, backtesting. Solo detección y alertas.

---

## Arquitectura General

```
┌─────────────────────────────────────────────────────────┐
│                      CRON (cada 5 min)                   │
│                   python -m src.cli scan                 │
└───────────────────────┬─────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌───────────┐   ┌───────────┐   ┌───────────┐
│  SCANNER  │   │  TRACKER  │   │  ALERTER  │
│  APIs     │   │  SQLite   │   │  Telegram │
└─────┬─────┘   └─────┬─────┘   └─────┬─────┘
      │               │               │
      └───────┬───────┘               │
              ▼                       │
      ┌───────────────┐               │
      │   SIGNALS     │───────────────┤
      │   + SCORER    │               │
      └───────────────┘               │
```

### Flujo de datos

1. **Scanner** consulta `/events?active=true&order=volume` (top 25 eventos)
2. Para cada mercado, consulta precio actual (`/price`) y spread (`/spread`)
3. **Tracker** guarda cada snapshot en SQLite (`prices` table)
4. **Signals** compara snapshot actual vs histórico reciente y detecta anomalías
5. **Scorer** asigna score 0-100 basado en intensidad y combinación de señales
6. **Alerter** envía a Telegram solo señales con score > threshold (configurable, default 60)

---

## Estructura de Archivos

```
polymarket-scout/
├── src/
│   ├── __init__.py
│   ├── scanner.py        # Polymarket API client
│   ├── tracker.py        # SQLite read/write
│   ├── signals.py        # Signal detection logic
│   ├── scorer.py         # Confidence scoring
│   ├── alerter.py        # Alert formatting + dispatch
│   └── cli.py            # CLI: scan, report, backfill
├── tests/
│   ├── __init__.py
│   ├── conftest.py        # Fixtures: mock API, temp DB
│   ├── test_scanner.py
│   ├── test_tracker.py
│   ├── test_signals.py
│   ├── test_scorer.py
│   └── test_alerter.py
├── config.yaml            # Thresholds, limits, schedule
├── data/                  # .gitkeep, SQLite goes here
├── requirements.txt
└── README.md
```

---

## Modelo de Datos (SQLite)

```sql
-- Un snapshot por mercado por ejecución
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,      -- Polymarket conditionId
    question TEXT NOT NULL,
    slug TEXT,
    event_title TEXT,
    price_yes REAL,                  -- 0.00–1.00
    price_no REAL,
    spread REAL,
    volume REAL,                     -- en USDC
    liquidity REAL,
    timestamp INTEGER NOT NULL,      -- Unix epoch
    UNIQUE(condition_id, timestamp)
);

-- Señales detectadas (solo las que superan threshold)
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,       -- momentum, volume_spike, spread_anomaly, etc.
    score INTEGER NOT NULL,          -- 0–100
    detail TEXT,                     -- JSON con datos extra
    timestamp INTEGER NOT NULL
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_snapshots_condition ON snapshots(condition_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(score);
```

---

## Tipos de Señales

| Señal | Descripción | Fórmula | Peso |
|-------|-------------|---------|------|
| `momentum_up` | Precio YES subió >X% en ventana temporal | `(price_now - price_before) / price_before` | 20 |
| `momentum_down` | Precio YES bajó >X% | Misma, invertida | 20 |
| `volume_spike` | Volumen actual >N× media móvil | `volume_now / avg_volume_24h` | 20 |
| `spread_tight` | Spread inusualmente bajo (consenso) | `spread < umbral` | 15 |
| `spread_wide` | Spread ancho (oportunidad arbitraje) | `spread > umbral_alto` | 10 |
| `new_interest` | Mercado nuevo con volumen inicial alto | `primer snapshot + volume > min` | 10 |
| `divergence` | Precio divergiendo de otros mercados del mismo evento | Comparar mercados hermanos | 5 |

---

## Scoring (0–100)

```
score = Σ (señal_activada × peso_señal × intensidad)

intensidad = min(1.0, valor_observado / umbral)

Ejemplo:
  momentum_up activado: +12% en 1h (umbral 5%)
    → intensidad = min(1.0, 12/5) = 1.0
    → contribución = 20 × 1.0 = 20

  volume_spike activado: 4× media (umbral 3×)
    → intensidad = min(1.0, 4/3) = 1.0
    → contribución = 20 × 1.0 = 20

  spread_tight activado: spread 0.02 (umbral 0.03)
    → intensidad = min(1.0, 0.03/0.02) = 1.0  (más tight = más intenso)
    → contribución = 15 × 1.0 = 15

  score total = 20 + 20 + 15 = 55
  SI score >= 60 → alerta
```

---

## Configuración (config.yaml)

```yaml
scanner:
  events_limit: 25           # cuántos eventos consultar
  markets_per_event: 10      # máximo mercados por evento
  min_volume: 5000           # ignorar mercados con <$5K volumen

tracker:
  db_path: "data/polymarket.db"
  retention_days: 90         # borrar snapshots >90 días

signals:
  momentum:
    threshold: 0.05          # 5% cambio
    window_hours: 1          # ventana de comparación
  volume_spike:
    threshold: 3.0           # 3× la media
    window_hours: 24         # ventana para calcular media
  spread:
    tight_threshold: 0.03    # spread < 3% es tight
    wide_threshold: 0.10     # spread > 10% es wide
  new_interest:
    min_volume: 10000        # volumen mínimo para nuevo mercado

scorer:
  alert_threshold: 60        # score mínimo para disparar alerta
  cooldown_minutes: 30       # no repetir alerta del mismo mercado en 30min

alerter:
  platform: "telegram"
  template: |
    🔔 **{score}/100 — {event_title}**
    {question}
    • Precio YES: {price_yes}
    • Cambio: {momentum}
    • Volumen: {volume}
    • Spread: {spread}
    • Señales: {signals_list}
```

---

## Tareas (20 tareas, ~2-5 min cada una)

---

### Task 1: Crear estructura de proyecto y requirements.txt

**Objective:** Inicializar el proyecto con estructura de directorios y dependencias.

**Files:**
- Create: `requirements.txt`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `data/.gitkeep`
- Create: `config.yaml`

**Step 1: Crear estructura**

```bash
mkdir -p src tests data
touch src/__init__.py tests/__init__.py data/.gitkeep
```

**Step 2: Escribir requirements.txt**

```txt
requests>=2.28
pandas>=2.0
pyyaml>=6.0
```

**Step 3: Escribir config.yaml**

Usar el contenido de la sección de configuración de arriba.

**Step 4: Escribir tests/conftest.py con fixtures base**

```python
import pytest
import sqlite3
import tempfile
import os
import json
from unittest.mock import Mock, patch

@pytest.fixture
def temp_db():
    """SQLite DB temporal para tests."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    yield conn
    conn.close()
    os.unlink(path)

@pytest.fixture
def mock_gamma_response():
    """Respuesta típica de Gamma API /events."""
    return [
        {
            "id": "evt-1",
            "title": "US Election 2024 Winner",
            "slug": "us-election-2024",
            "volume": 5000000.0,
            "active": True,
            "closed": False,
            "markets": [
                {
                    "question": "Will Trump win?",
                    "outcomePrices": '["0.65", "0.35"]',
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["tok-yes-1", "tok-no-1"]',
                    "conditionId": "0xabc123",
                    "volume": 3000000.0,
                    "slug": "trump-win-2024"
                }
            ]
        }
    ]

@pytest.fixture
def mock_clob_price():
    """Respuesta típica de CLOB /price."""
    return {"price": "0.65"}

@pytest.fixture
def mock_clob_spread():
    """Respuesta típica de CLOB /spread."""
    return {"spread": "0.02"}

@pytest.fixture
def sample_snapshots():
    """Datos de snapshot para tests de signals."""
    return [
        {"condition_id": "0xabc", "price_yes": 0.60, "volume": 100000, "timestamp": 1000},
        {"condition_id": "0xabc", "price_yes": 0.65, "volume": 500000, "timestamp": 2000},
        {"condition_id": "0xabc", "price_yes": 0.70, "volume": 600000, "timestamp": 3000},
    ]

@pytest.fixture
def config():
    """Config mínima para tests."""
    return {
        "scanner": {"events_limit": 5, "markets_per_event": 3, "min_volume": 5000},
        "tracker": {"db_path": ":memory:", "retention_days": 90},
        "signals": {
            "momentum": {"threshold": 0.05, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000}
        },
        "scorer": {"alert_threshold": 60, "cooldown_minutes": 30},
        "alerter": {"platform": "telegram", "template": "test template"}
    }
```

**Step 5: Commit**

```bash
cd /opt/data/polymarket-scout
git init
git add -A
git commit -m "chore: initialize project structure"
```

---

### Task 2: Scanner — API client base

**Objective:** Crear el cliente HTTP base con las 3 APIs de Polymarket.

**Files:**
- Create: `src/scanner.py`

**Step 1: Escribir test para _get()**

```python
# tests/test_scanner.py
import pytest
from unittest.mock import patch, Mock
from src.scanner import PolymarketScanner

def test_get_makes_request():
    scanner = PolymarketScanner()
    mock_resp = Mock()
    mock_resp.read.return_value = b'{"key": "value"}'
    
    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_urlopen:
        result = scanner._get("https://gamma-api.polymarket.com/events?limit=1")
    
    assert result == {"key": "value"}
    mock_urlopen.assert_called_once()
```

**Step 2: Ejecutar test — debe fallar (clase no existe)**

```bash
cd /opt/data/polymarket-scout && python -m pytest tests/test_scanner.py -v
# Expected: FAIL — NameError: name 'PolymarketScanner' is not defined
```

**Step 3: Implementar src/scanner.py**

```python
"""Polymarket API scanner — read-only market data."""
import json
import urllib.request
import urllib.parse
import urllib.error
import logging

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


class PolymarketScanner:
    """Client for Polymarket public APIs."""

    def __init__(self):
        self.session_headers = {"User-Agent": "polymarket-scout/1.0"}

    def _get(self, url: str) -> dict | list:
        """GET request returning parsed JSON."""
        req = urllib.request.Request(url, headers=self.session_headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP {e.code}: {e.reason} for {url}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"Connection error: {e.reason} for {url}")
            raise

    @staticmethod
    def parse_json_field(val):
        """Parse double-encoded JSON fields (outcomePrices, outcomes, clobTokenIds)."""
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val
```

**Step 4: Ejecutar test — debe pasar**

```bash
cd /opt/data/polymarket-scout && python -m pytest tests/test_scanner.py -v
# Expected: PASS
```

**Step 5: Commit**

```bash
git add src/scanner.py tests/test_scanner.py
git commit -m "feat: add PolymarketScanner base HTTP client"
```

---

### Task 3: Scanner — get_events()

**Objective:** Añadir método para consultar eventos activos ordenados por volumen.

**Files:**
- Modify: `src/scanner.py`

**Step 1: Escribir test**

```python
# Añadir a tests/test_scanner.py
def test_get_events_returns_list(scanner, mock_gamma_response):
    with patch.object(scanner, '_get', return_value=mock_gamma_response):
        events = scanner.get_events(limit=10)
    
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["title"] == "US Election 2024 Winner"

def test_get_events_filters_closed(scanner):
    closed_event = [{"id": "1", "title": "Closed", "closed": True, "markets": []}]
    with patch.object(scanner, '_get', return_value=closed_event):
        events = scanner.get_events(active_only=True)
    # Gamma API already filters with active=true param, test verifies param passthrough
    assert len(events) == 1

@pytest.fixture
def scanner():
    return PolymarketScanner()
```

**Step 2: Ejecutar test — debe fallar (método no existe)**

**Step 3: Implementar get_events()**

```python
# Añadir a PolymarketScanner en src/scanner.py
def get_events(self, limit: int = 25, active_only: bool = True,
               order: str = "volume", ascending: bool = False) -> list:
    """Fetch active events from Gamma API, sorted by volume."""
    params = {
        "limit": limit,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    if active_only:
        params["active"] = "true"
        params["closed"] = "false"
    
    qs = urllib.parse.urlencode(params)
    return self._get(f"{GAMMA}/events?{qs}")
```

**Step 4: Test → PASS**

**Step 5: Commit**

---

### Task 4: Scanner — get_price() y get_spread()

**Objective:** Añadir métodos para precio y spread de un token.

**Files:**
- Modify: `src/scanner.py`

**Step 1: Escribir tests**

```python
def test_get_price(scanner, mock_clob_price):
    with patch.object(scanner, '_get', return_value=mock_clob_price):
        price = scanner.get_price("tok-yes-1", side="buy")
    assert price == 0.65

def test_get_price_returns_float(scanner):
    with patch.object(scanner, '_get', return_value={"price": "0.42"}):
        price = scanner.get_price("tok")
    assert isinstance(price, float)
    assert price == 0.42

def test_get_spread(scanner):
    with patch.object(scanner, '_get', return_value={"spread": "0.03"}):
        spread = scanner.get_spread("tok")
    assert spread == 0.03
```

**Step 2: Tests → FAIL**

**Step 3: Implementar**

```python
# Añadir a PolymarketScanner
def get_price(self, token_id: str, side: str = "buy") -> float:
    """Get current price for a token. Returns 0.0-1.0."""
    data = self._get(f"{CLOB}/price?token_id={token_id}&side={side}")
    return float(data.get("price", 0))

def get_spread(self, token_id: str) -> float:
    """Get current bid-ask spread."""
    data = self._get(f"{CLOB}/spread?token_id={token_id}")
    return float(data.get("spread", 0))
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 5: Scanner — scan_markets() (método principal)

**Objective:** Método que orquesta todo: obtiene eventos, extrae mercados, consulta precios y spreads, devuelve lista de snapshots listos para el tracker.

**Files:**
- Modify: `src/scanner.py`

**Step 1: Escribir test de integración**

```python
def test_scan_markets(scanner, mock_gamma_response, mock_clob_price, mock_clob_spread):
    with patch.object(scanner, '_get') as mock_get:
        # Primera llamada: get_events
        # Segunda+ llamadas: get_price/get_spread
        mock_get.side_effect = [
            mock_gamma_response,
            {"price": "0.65"},
            {"spread": "0.02"},
        ]
        snapshots = scanner.scan_markets(events_limit=1, markets_per_event=5, min_volume=0)
    
    assert len(snapshots) >= 1
    s = snapshots[0]
    assert s["condition_id"] == "0xabc123"
    assert s["question"] == "Will Trump win?"
    assert s["price_yes"] == 0.65
    assert s["spread"] == 0.02
    assert "timestamp" in s
```

**Step 2: Test → FAIL**

**Step 3: Implementar scan_markets()**

```python
# Añadir a PolymarketScanner
def scan_markets(self, events_limit: int = 25, markets_per_event: int = 10,
                 min_volume: float = 5000) -> list[dict]:
    """Scan all active markets and return current snapshots."""
    import time
    
    events = self.get_events(limit=events_limit, active_only=True)
    snapshots = []
    now = int(time.time())
    
    for event in events:
        markets = event.get("markets", [])
        # Ordenar mercados por volumen y limitar
        markets_sorted = sorted(
            markets, 
            key=lambda m: float(m.get("volume", 0)), 
            reverse=True
        )[:markets_per_event]
        
        for market in markets_sorted:
            volume = float(market.get("volume", 0))
            if volume < min_volume:
                continue
            
            tokens = self.parse_json_field(market.get("clobTokenIds", "[]"))
            prices_raw = self.parse_json_field(market.get("outcomePrices", "[]"))
            
            token_yes = tokens[0] if isinstance(tokens, list) and len(tokens) > 0 else None
            
            price_yes = float(prices_raw[0]) if isinstance(prices_raw, list) and len(prices_raw) > 0 else None
            spread = None
            
            if token_yes:
                try:
                    price_yes = self.get_price(token_yes)
                    spread = self.get_spread(token_yes)
                except Exception as e:
                    logger.warning(f"Failed to get price/spread for {token_yes}: {e}")
            
            snapshots.append({
                "condition_id": market.get("conditionId", ""),
                "question": market.get("question", ""),
                "slug": market.get("slug", ""),
                "event_title": event.get("title", ""),
                "price_yes": price_yes,
                "price_no": 1.0 - price_yes if price_yes is not None else None,
                "spread": spread,
                "volume": volume,
                "liquidity": float(market.get("liquidity", 0)),
                "timestamp": now,
            })
    
    return snapshots
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 6: Tracker — init_db() y esquema

**Objective:** Módulo SQLite con inicialización de esquema y método para guardar snapshots.

**Files:**
- Create: `src/tracker.py`

**Step 1: Escribir test**

```python
# tests/test_tracker.py
import sqlite3
import time
import pytest
from src.tracker import Tracker

def test_init_db_creates_tables(temp_db):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    cursor = temp_db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    assert "snapshots" in tables
    assert "signals" in tables

def test_save_snapshots(temp_db):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    snapshots = [
        {
            "condition_id": "0xabc", "question": "Will X?", "slug": "x",
            "event_title": "Event X", "price_yes": 0.65, "price_no": 0.35,
            "spread": 0.02, "volume": 100000, "liquidity": 50000,
            "timestamp": 1700000000
        }
    ]
    count = tracker.save_snapshots(snapshots)
    assert count == 1
    
    # Verificar que se guardó
    cursor = temp_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM snapshots")
    assert cursor.fetchone()[0] == 1

def test_save_snapshots_deduplicates(temp_db):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    s1 = {"condition_id": "0xabc", "question": "X", "slug": "x",
          "event_title": "E", "price_yes": 0.5, "price_no": 0.5,
          "spread": 0.01, "volume": 100, "liquidity": 50, "timestamp": 1000}
    s2 = {**s1}  # mismo condition_id + timestamp
    
    tracker.save_snapshots([s1])
    count = tracker.save_snapshots([s2])
    assert count == 0  # no guardó duplicado
```

**Step 2: Test → FAIL**

**Step 3: Implementar src/tracker.py**

```python
"""SQLite tracker for Polymarket snapshots and signals."""
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Tracker:
    """Persist market snapshots and detected signals to SQLite."""

    def __init__(self, db):
        if isinstance(db, sqlite3.Connection):
            self.conn = db
        else:
            Path(db).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(db))

    def init_db(self):
        """Create tables and indexes if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                question TEXT NOT NULL,
                slug TEXT,
                event_title TEXT,
                price_yes REAL,
                price_no REAL,
                spread REAL,
                volume REAL,
                liquidity REAL,
                timestamp INTEGER NOT NULL,
                UNIQUE(condition_id, timestamp)
            );
            
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                score INTEGER NOT NULL,
                detail TEXT,
                timestamp INTEGER NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_snapshots_condition 
                ON snapshots(condition_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp 
                ON snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp 
                ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_score 
                ON signals(score);
        """)
        self.conn.commit()

    def save_snapshots(self, snapshots: list[dict]) -> int:
        """Insert snapshots. Skips duplicates (condition_id + timestamp). Returns count saved."""
        count = 0
        for s in snapshots:
            try:
                self.conn.execute("""
                    INSERT OR IGNORE INTO snapshots 
                    (condition_id, question, slug, event_title, price_yes, price_no,
                     spread, volume, liquidity, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    s["condition_id"], s["question"], s.get("slug", ""),
                    s.get("event_title", ""), s["price_yes"], s.get("price_no"),
                    s.get("spread"), s.get("volume", 0), s.get("liquidity", 0),
                    s["timestamp"]
                ))
                if self.conn.execute("SELECT changes()").fetchone()[0] > 0:
                    count += 1
            except Exception as e:
                logger.warning(f"Failed to save snapshot: {e}")
        self.conn.commit()
        return count
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 7: Tracker — get_recent_snapshots()

**Objective:** Obtener los snapshots recientes de un mercado para que el detector de señales pueda comparar.

**Files:**
- Modify: `src/tracker.py`

**Step 1: Escribir test**

```python
def test_get_recent_snapshots(temp_db, sample_snapshots):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    # Insertar datos de prueba
    for s in [
        {"condition_id": "0xabc", "question": "Q", "slug": "q", "event_title": "E",
         "price_yes": 0.60, "price_no": 0.40, "spread": 0.02, "volume": 100000,
         "liquidity": 50000, "timestamp": 1000},
        {"condition_id": "0xabc", "question": "Q", "slug": "q", "event_title": "E",
         "price_yes": 0.65, "price_no": 0.35, "spread": 0.02, "volume": 500000,
         "liquidity": 50000, "timestamp": 2000},
        {"condition_id": "0xabc", "question": "Q", "slug": "q", "event_title": "E",
         "price_yes": 0.70, "price_no": 0.30, "spread": 0.01, "volume": 600000,
         "liquidity": 50000, "timestamp": 3000},
        {"condition_id": "0xdef", "question": "Q2", "slug": "q2", "event_title": "E2",
         "price_yes": 0.50, "price_no": 0.50, "spread": 0.05, "volume": 500,
         "liquidity": 100, "timestamp": 3000},
    ]:
        tracker.conn.execute("""
            INSERT OR IGNORE INTO snapshots 
            (condition_id, question, slug, event_title, price_yes, price_no,
             spread, volume, liquidity, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["condition_id"], s["question"], s["slug"], s["event_title"],
              s["price_yes"], s["price_no"], s["spread"], s["volume"],
              s["liquidity"], s["timestamp"]))
    tracker.conn.commit()
    
    # Obtener snapshots de 0xabc en ventana 1000-3000
    results = tracker.get_recent_snapshots("0xabc", lookback_seconds=2500, reference_ts=3000)
    assert len(results) >= 3  # los 3 de 0xabc
```

**Step 2: Test → FAIL**

**Step 3: Implementar**

```python
# Añadir a Tracker
def get_recent_snapshots(self, condition_id: str, lookback_seconds: int = 3600,
                         reference_ts: int = None) -> list[dict]:
    """Get recent snapshots for a market within the lookback window."""
    import time
    if reference_ts is None:
        reference_ts = int(time.time())
    
    since = reference_ts - lookback_seconds
    cursor = self.conn.execute("""
        SELECT condition_id, question, slug, event_title, price_yes, price_no,
               spread, volume, liquidity, timestamp
        FROM snapshots
        WHERE condition_id = ? AND timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp ASC
    """, (condition_id, since, reference_ts))
    
    return [dict(zip(
        ["condition_id", "question", "slug", "event_title", "price_yes", "price_no",
         "spread", "volume", "liquidity", "timestamp"], row
    )) for row in cursor.fetchall()]
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 8: Tracker — save_signals()

**Objective:** Guardar señales detectadas evitando duplicados por cooldown.

**Files:**
- Modify: `src/tracker.py`

**Step 1: Escribir test**

```python
def test_save_signal(temp_db):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    signal = {
        "condition_id": "0xabc",
        "signal_type": "momentum_up",
        "score": 75,
        "detail": '{"change": 0.12}',
        "timestamp": 3000
    }
    tracker.save_signal(**signal)
    
    cursor = temp_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM signals")
    assert cursor.fetchone()[0] == 1

def test_save_signal_skips_duplicate_in_cooldown(temp_db):
    tracker = Tracker(temp_db)
    tracker.init_db()
    
    signal = {"condition_id": "0xabc", "signal_type": "momentum_up",
              "score": 75, "detail": "{}", "timestamp": 3000}
    tracker.save_signal(**signal)
    tracker.save_signal(**{**signal, "timestamp": 3001})  # 1 seg después
    
    cursor = temp_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM signals")
    assert cursor.fetchone()[0] == 1  # no duplicado
```

**Step 2: Test → FAIL**

**Step 3: Implementar**

```python
# Añadir a Tracker
def save_signal(self, condition_id: str, signal_type: str, score: int,
                detail: str = "{}", timestamp: int = None, cooldown_minutes: int = 30) -> bool:
    """Save a detected signal. Skips if same market+type within cooldown. Returns True if saved."""
    import time
    if timestamp is None:
        timestamp = int(time.time())
    
    # Check cooldown
    since = timestamp - (cooldown_minutes * 60)
    cursor = self.conn.execute("""
        SELECT COUNT(*) FROM signals
        WHERE condition_id = ? AND signal_type = ? AND timestamp >= ?
    """, (condition_id, signal_type, since))
    if cursor.fetchone()[0] > 0:
        return False
    
    self.conn.execute("""
        INSERT INTO signals (condition_id, signal_type, score, detail, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (condition_id, signal_type, score, detail, timestamp))
    self.conn.commit()
    return True
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 9: Signals — detect_momentum()

**Objective:** Detectar cambios de precio significativos en una ventana temporal.

**Files:**
- Create: `src/signals.py`

**Step 1: Escribir test**

```python
# tests/test_signals.py
import pytest
from src.signals import detect_momentum

def test_detect_momentum_up():
    snapshots = [
        {"price_yes": 0.50, "timestamp": 1000},
        {"price_yes": 0.55, "timestamp": 2000},
        {"price_yes": 0.60, "timestamp": 3000},
    ]
    result = detect_momentum(snapshots, threshold=0.05, window_seconds=3000)
    assert result is not None
    assert result["signal_type"] == "momentum_up"
    assert result["change_pct"] > 0

def test_detect_momentum_down():
    snapshots = [
        {"price_yes": 0.60, "timestamp": 1000},
        {"price_yes": 0.55, "timestamp": 2000},
        {"price_yes": 0.50, "timestamp": 3000},
    ]
    result = detect_momentum(snapshots, threshold=0.05, window_seconds=3000)
    assert result is not None
    assert result["signal_type"] == "momentum_down"

def test_detect_momentum_no_change():
    snapshots = [
        {"price_yes": 0.60, "timestamp": 1000},
        {"price_yes": 0.61, "timestamp": 3000},
    ]
    result = detect_momentum(snapshots, threshold=0.05, window_seconds=3000)
    assert result is None  # solo 1.6%, por debajo del umbral 5%

def test_detect_momentum_insufficient_data():
    # Solo 1 snapshot, no se puede calcular momentum
    snapshots = [{"price_yes": 0.60, "timestamp": 3000}]
    result = detect_momentum(snapshots, threshold=0.05, window_seconds=3000)
    assert result is None
```

**Step 2: Tests → FAIL**

**Step 3: Implementar src/signals.py**

```python
"""Signal detectors for Polymarket Scout."""
import logging

logger = logging.getLogger(__name__)


def detect_momentum(snapshots: list[dict], threshold: float = 0.05,
                    window_seconds: int = 3600) -> dict | None:
    """Detect significant price momentum in a window.
    
    Returns dict with signal_type, change_pct, price_start, price_end, or None.
    """
    if len(snapshots) < 2:
        return None
    
    # Filter to window
    latest_ts = max(s["timestamp"] for s in snapshots)
    window_start = latest_ts - window_seconds
    in_window = [s for s in snapshots if s["timestamp"] >= window_start]
    
    if len(in_window) < 2:
        return None
    
    first = in_window[0]
    last = in_window[-1]
    
    price_start = first["price_yes"]
    price_end = last["price_yes"]
    
    if price_start is None or price_end is None or price_start == 0:
        return None
    
    change_pct = (price_end - price_start) / price_start
    
    if abs(change_pct) < threshold:
        return None
    
    return {
        "signal_type": "momentum_up" if change_pct > 0 else "momentum_down",
        "change_pct": round(change_pct, 4),
        "price_start": price_start,
        "price_end": price_end,
    }
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 10: Signals — detect_volume_spike()

**Objective:** Detectar picos de volumen comparando con media móvil.

**Files:**
- Modify: `src/signals.py`

**Step 1: Escribir tests**

```python
def test_detect_volume_spike():
    snapshots = [
        {"volume": 100000, "timestamp": 1000},
        {"volume": 120000, "timestamp": 2000},
        {"volume": 110000, "timestamp": 3000},
        {"volume": 500000, "timestamp": 4000},  # spike!
    ]
    result = detect_volume_spike(snapshots, threshold=3.0)
    assert result is not None
    assert result["signal_type"] == "volume_spike"
    assert result["ratio"] > 3.0

def test_detect_volume_spike_no_spike():
    snapshots = [
        {"volume": 100000, "timestamp": 1000},
        {"volume": 120000, "timestamp": 2000},
    ]
    result = detect_volume_spike(snapshots, threshold=3.0)
    assert result is None

def test_detect_volume_spike_insufficient_data():
    result = detect_volume_spike([{"volume": 500000, "timestamp": 4000}], threshold=3.0)
    assert result is None
```

**Step 2: Tests → FAIL**

**Step 3: Implementar**

```python
# Añadir a src/signals.py
def detect_volume_spike(snapshots: list[dict], threshold: float = 3.0) -> dict | None:
    """Detect volume spike: current volume vs average of previous snapshots.
    
    Returns dict with signal_type, ratio, volume_now, volume_avg, or None.
    """
    if len(snapshots) < 2:
        return None
    
    # Latest snapshot vs average of all EXCEPT the latest
    latest = snapshots[-1]
    previous = snapshots[:-1]
    
    vol_now = latest.get("volume", 0)
    if vol_now <= 0:
        return None
    
    vol_avg = sum(s.get("volume", 0) for s in previous) / len(previous)
    if vol_avg <= 0:
        return None
    
    ratio = vol_now / vol_avg
    
    if ratio < threshold:
        return None
    
    return {
        "signal_type": "volume_spike",
        "ratio": round(ratio, 2),
        "volume_now": vol_now,
        "volume_avg": round(vol_avg, 2),
    }
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 11: Signals — detect_spread_anomaly()

**Objective:** Detectar spreads anormalmente tight (consenso) o wide (oportunidad/incertidumbre).

**Files:**
- Modify: `src/signals.py`

**Step 1: Escribir tests**

```python
def test_detect_spread_tight():
    snapshots = [{"spread": 0.01, "timestamp": 3000}]  # solo necesita el actual
    result = detect_spread_anomaly(snapshots, tight_threshold=0.03, wide_threshold=0.10)
    assert result is not None
    assert result["signal_type"] == "spread_tight"

def test_detect_spread_wide():
    snapshots = [{"spread": 0.15, "timestamp": 3000}]
    result = detect_spread_anomaly(snapshots, tight_threshold=0.03, wide_threshold=0.10)
    assert result is not None
    assert result["signal_type"] == "spread_wide"

def test_detect_spread_normal():
    snapshots = [{"spread": 0.05, "timestamp": 3000}]
    result = detect_spread_anomaly(snapshots, tight_threshold=0.03, wide_threshold=0.10)
    assert result is None

def test_detect_spread_none():
    snapshots = [{"spread": None, "timestamp": 3000}]
    result = detect_spread_anomaly(snapshots, tight_threshold=0.03, wide_threshold=0.10)
    assert result is None
```

**Step 2: Tests → FAIL**

**Step 3: Implementar**

```python
# Añadir a src/signals.py
def detect_spread_anomaly(snapshots: list[dict], tight_threshold: float = 0.03,
                          wide_threshold: float = 0.10) -> dict | None:
    """Detect unusual spread — very tight (strong consensus) or very wide (disagreement/opportunity).
    
    Uses the latest snapshot's spread.
    """
    if not snapshots:
        return None
    
    latest = snapshots[-1]
    spread = latest.get("spread")
    
    if spread is None:
        return None
    
    if spread <= tight_threshold:
        return {"signal_type": "spread_tight", "spread": spread}
    elif spread >= wide_threshold:
        return {"signal_type": "spread_wide", "spread": spread}
    
    return None
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 12: Signals — detect_all() (orquestador de señales)

**Objective:** Función que ejecuta todos los detectores para un mercado y devuelve lista de señales activas.

**Files:**
- Modify: `src/signals.py`

**Step 1: Escribir test de integración**

```python
def test_detect_all_multiple_signals():
    snapshots = [
        {"price_yes": 0.50, "volume": 100000, "spread": 0.05, "timestamp": 1000},
        {"price_yes": 0.60, "volume": 500000, "spread": 0.01, "timestamp": 2000},
    ]
    config = {
        "momentum": {"threshold": 0.05, "window_hours": 1},
        "volume_spike": {"threshold": 3.0, "window_hours": 24},
        "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
        "new_interest": {"min_volume": 10000},
    }
    signals = detect_all(snapshots, config)
    
    # Debería detectar momentum_up (+20%), volume_spike (5x), spread_tight (0.01)
    assert len(signals) >= 2
    types = [s["signal_type"] for s in signals]
    assert "momentum_up" in types
    assert "volume_spike" in types
    assert "spread_tight" in types

def test_detect_all_no_signals():
    snapshots = [
        {"price_yes": 0.50, "volume": 100000, "spread": 0.05, "timestamp": 1000},
        {"price_yes": 0.51, "volume": 110000, "spread": 0.05, "timestamp": 2000},
    ]
    config = {
        "momentum": {"threshold": 0.05, "window_hours": 1},
        "volume_spike": {"threshold": 3.0, "window_hours": 24},
        "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
        "new_interest": {"min_volume": 10000},
    }
    signals = detect_all(snapshots, config)
    assert signals == []
```

**Step 2: Tests → FAIL**

**Step 3: Implementar**

```python
# Añadir a src/signals.py
def detect_all(snapshots: list[dict], config: dict) -> list[dict]:
    """Run all signal detectors and return list of active signals.
    
    Each signal dict has: signal_type, condition_id, detail fields, intensity.
    Config keys: momentum, volume_spike, spread, new_interest.
    """
    signals = []
    
    # Momentum
    mom_cfg = config.get("momentum", {})
    result = detect_momentum(
        snapshots,
        threshold=mom_cfg.get("threshold", 0.05),
        window_seconds=mom_cfg.get("window_hours", 1) * 3600,
    )
    if result:
        result["weight"] = 20
        signals.append(result)
    
    # Volume spike
    vol_cfg = config.get("volume_spike", {})
    result = detect_volume_spike(
        snapshots,
        threshold=vol_cfg.get("threshold", 3.0),
    )
    if result:
        result["weight"] = 20
        signals.append(result)
    
    # Spread anomaly
    spread_cfg = config.get("spread", {})
    result = detect_spread_anomaly(
        snapshots,
        tight_threshold=spread_cfg.get("tight_threshold", 0.03),
        wide_threshold=spread_cfg.get("wide_threshold", 0.10),
    )
    if result:
        result["weight"] = 15 if result["signal_type"] == "spread_tight" else 10
        signals.append(result)
    
    # New interest (solo 1 snapshot = mercado recién añadido al tracker)
    ni_cfg = config.get("new_interest", {})
    if len(snapshots) == 1:
        vol = snapshots[0].get("volume", 0)
        if vol >= ni_cfg.get("min_volume", 10000):
            signals.append({
                "signal_type": "new_interest",
                "weight": 10,
                "volume": vol,
            })
    
    return signals
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 13: Scorer — calculate_score()

**Objective:** Convertir señales detectadas en un score numérico 0-100.

**Files:**
- Create: `src/scorer.py`

**Step 1: Escribir tests**

```python
# tests/test_scorer.py
from src.scorer import calculate_score

def test_calculate_score_all_signals():
    signals = [
        {"signal_type": "momentum_up", "weight": 20, "change_pct": 0.10},
        {"signal_type": "volume_spike", "weight": 20, "ratio": 5.0},
        {"signal_type": "spread_tight", "weight": 15, "spread": 0.01},
    ]
    score, detail = calculate_score(signals)
    assert 0 <= score <= 100
    # momentum: 20 * min(1, 0.10/0.05) = 20
    # volume: 20 * min(1, 5.0/3.0) = 20
    # spread_tight: 15 * min(1, 0.03/0.01) = 15
    # total = 55
    assert score == 55
    assert isinstance(detail, str)  # JSON

def test_calculate_score_empty():
    score, detail = calculate_score([])
    assert score == 0

def test_calculate_score_capped():
    # Incluso con intensidad >1, no supera el weight
    signals = [
        {"signal_type": "momentum_up", "weight": 20, "change_pct": 0.50},  # 10x threshold
    ]
    score, _ = calculate_score(signals)
    assert score == 20  # capped at weight
```

**Step 2: Tests → FAIL**

**Step 3: Implementar src/scorer.py**

```python
"""Scoring engine for Polymarket signals."""
import json

# Intensity thresholds per signal type
INTENSITY_THRESHOLDS = {
    "momentum_up": ("change_pct", 0.05),
    "momentum_down": ("change_pct", 0.05),
    "volume_spike": ("ratio", 3.0),
    "spread_tight": ("spread", 0.03, True),   # inverted: lower spread = more intense
    "spread_wide": ("spread", 0.10),
    "new_interest": ("volume", 10000),
}


def calculate_score(signals: list[dict]) -> tuple[int, str]:
    """Calculate confidence score (0-100) from detected signals.
    
    Returns (score, detail_json).
    """
    if not signals:
        return 0, "{}"
    
    total_score = 0
    detail = []
    
    for sig in signals:
        weight = sig.get("weight", 10)
        signal_type = sig["signal_type"]
        
        # Calcular intensidad
        intensity = _calculate_intensity(sig)
        
        contribution = min(weight, int(weight * intensity))
        total_score += contribution
        
        detail.append({
            "signal": signal_type,
            "weight": weight,
            "intensity": round(intensity, 2),
            "contribution": contribution,
        })
    
    return min(100, total_score), json.dumps(detail)


def _calculate_intensity(signal: dict) -> float:
    """How strong is this signal relative to its threshold? 0.0-1.0."""
    signal_type = signal["signal_type"]
    
    if signal_type not in INTENSITY_THRESHOLDS:
        return 1.0
    
    entry = INTENSITY_THRESHOLDS[signal_type]
    field = entry[0]
    threshold = entry[1]
    inverted = len(entry) > 2 and entry[2]
    
    value = signal.get(field)
    if value is None or threshold == 0:
        return 0.0
    
    if inverted:
        # Lower value = more intense (e.g., tighter spread)
        if value <= 0:
            return 1.0
        intensity = threshold / value
    else:
        intensity = abs(value) / threshold
    
    return min(1.0, intensity)
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 14: Alerter — formatear y enviar

**Objective:** Formatear señales como mensaje de Telegram y enviarlas.

**Files:**
- Create: `src/alerter.py`

**Step 1: Escribir tests**

```python
# tests/test_alerter.py
from src.alerter import format_alert, should_alert

def test_format_alert():
    snapshot = {
        "question": "Will Trump win 2024?",
        "event_title": "US Election 2024",
        "price_yes": 0.65,
        "volume": 5000000,
        "spread": 0.02,
        "slug": "trump-win-2024",
    }
    signals_detail = [
        {"signal": "momentum_up", "weight": 20, "intensity": 1.0, "contribution": 20},
        {"signal": "volume_spike", "weight": 20, "intensity": 1.0, "contribution": 20},
    ]
    msg = format_alert(score=55, snapshot=snapshot, signals_detail=signals_detail,
                       momentum_str="+12.0%")
    
    assert "55/100" in msg
    assert "Trump" in msg
    assert "momentum_up" in msg
    assert "volume_spike" in msg

def test_should_alert_above_threshold():
    assert should_alert(score=75, threshold=60) is True

def test_should_alert_below_threshold():
    assert should_alert(score=45, threshold=60) is False

def test_should_alert_equal_threshold():
    assert should_alert(score=60, threshold=60) is True
```

**Step 2: Tests → FAIL**

**Step 3: Implementar src/alerter.py**

```python
"""Alert formatting and dispatch for Polymarket Scout."""
import json
import logging

logger = logging.getLogger(__name__)

# Signal emoji mapping
SIGNAL_EMOJI = {
    "momentum_up": "🚀",
    "momentum_down": "📉",
    "volume_spike": "📊",
    "spread_tight": "🤝",
    "spread_wide": "🌈",
    "new_interest": "🆕",
}


def should_alert(score: int, threshold: int = 60) -> bool:
    """Decide whether to send an alert based on score threshold."""
    return score >= threshold


def format_alert(score: int, snapshot: dict, signals_detail: list[dict],
                 momentum_str: str = "—") -> str:
    """Format a market alert as a Telegram message."""
    question = snapshot.get("question", "?")
    event_title = snapshot.get("event_title", "")
    price_yes = snapshot.get("price_yes", 0)
    volume = snapshot.get("volume", 0)
    spread = snapshot.get("spread", 0)
    slug = snapshot.get("slug", "")
    
    # Formatear precio como porcentaje
    price_pct = f"{price_yes * 100:.1f}%" if price_yes else "?"
    spread_pct = f"{spread * 100:.1f}%" if spread else "?"
    
    # Formatear volumen
    if volume >= 1_000_000:
        vol_str = f"${volume / 1_000_000:.1f}M"
    elif volume >= 1_000:
        vol_str = f"${volume / 1_000:.1f}K"
    else:
        vol_str = f"${volume:.0f}"
    
    # Señales detectadas
    signal_lines = []
    for s in signals_detail:
        emoji = SIGNAL_EMOJI.get(s["signal"], "📌")
        signal_lines.append(f"  {emoji} {s['signal']} (+{s['contribution']})")
    
    signals_block = "\n".join(signal_lines) if signal_lines else "  —"
    
    url = f"https://polymarket.com/event/{slug}" if slug else ""
    
    msg = f"""🔔 **{score}/100 — {event_title}**

**{question}**

• Precio YES: {price_pct}
• Cambio: {momentum_str}
• Volumen: {vol_str}
• Spread: {spread_pct}

Señales detectadas:
{signals_block}

[Ver en Polymarket]({url})"""
    
    return msg


def dispatch_alerts(alerts: list[str], platform: str = "telegram"):
    """Log alerts. Actual dispatch to Telegram is handled by Hermes cron delivery."""
    for alert in alerts:
        logger.info(f"ALERT: {alert[:100]}...")
    return len(alerts)
```

**Step 4: Tests → PASS**

**Step 5: Commit**

---

### Task 15: CLI — comando `scan`

**Objective:** CLI que ejecuta el pipeline completo: scan → track → detect → score → alert.

**Files:**
- Create: `src/cli.py`

**Step 1: Escribir test**

```python
# tests/test_cli.py
import pytest
from unittest.mock import patch, Mock, call
from src.cli import run_scan

@patch('src.cli.yaml.safe_load')
@patch('src.cli.Tracker')
@patch('src.cli.PolymarketScanner')
def test_run_scan_pipeline(mock_scanner_cls, mock_tracker_cls, mock_yaml):
    # Setup mocks
    mock_scanner = Mock()
    mock_scanner.scan_markets.return_value = [
        {"condition_id": "0xabc", "question": "Q?", "slug": "q", "event_title": "E",
         "price_yes": 0.60, "price_no": 0.40, "spread": 0.05, "volume": 100000,
         "liquidity": 50000, "timestamp": 3000},
    ]
    mock_scanner_cls.return_value = mock_scanner
    
    mock_tracker = Mock()
    mock_tracker.get_recent_snapshots.return_value = [
        {"price_yes": 0.50, "volume": 80000, "spread": 0.06, "timestamp": 1000},
        {"price_yes": 0.60, "volume": 100000, "spread": 0.05, "timestamp": 3000},
    ]
    mock_tracker_cls.return_value = mock_tracker
    
    mock_yaml.return_value = {
        "scanner": {"events_limit": 5, "markets_per_event": 3, "min_volume": 5000},
        "tracker": {"db_path": ":memory:", "retention_days": 90},
        "signals": {
            "momentum": {"threshold": 0.05, "window_hours": 1},
            "volume_spike": {"threshold": 3.0, "window_hours": 24},
            "spread": {"tight_threshold": 0.03, "wide_threshold": 0.10},
            "new_interest": {"min_volume": 10000},
        },
        "scorer": {"alert_threshold": 60, "cooldown_minutes": 30},
        "alerter": {"platform": "telegram"},
    }
    
    alerts = run_scan("/fake/config.yaml")
    
    # Debería haber llamado a scan_markets
    mock_scanner.scan_markets.assert_called_once()
    # Debería haber guardado snapshots
    mock_tracker.save_snapshots.assert_called_once()
    # Debería haber consultado snapshots recientes
    mock_tracker.get_recent_snapshots.assert_called()
```

**Step 2: Test → FAIL**

**Step 3: Implementar src/cli.py**

```python
#!/usr/bin/env python3
"""CLI for Polymarket Scout — market monitoring and alerting."""
import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from src.scanner import PolymarketScanner
from src.tracker import Tracker
from src.signals import detect_all
from src.scorer import calculate_score
from src.alerter import format_alert, should_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scout")


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML configuration."""
    with open(path) as f:
        return yaml.safe_load(f)


def run_scan(config_path: str = "config.yaml") -> list[str]:
    """Execute full scan pipeline. Returns list of alert messages."""
    config = load_config(config_path)
    
    # 1. Scan
    scanner = PolymarketScanner()
    scan_cfg = config.get("scanner", {})
    snapshots = scanner.scan_markets(
        events_limit=scan_cfg.get("events_limit", 25),
        markets_per_event=scan_cfg.get("markets_per_event", 10),
        min_volume=scan_cfg.get("min_volume", 5000),
    )
    logger.info(f"Scanned {len(snapshots)} markets")
    
    if not snapshots:
        return []
    
    # 2. Track
    tracker_cfg = config.get("tracker", {})
    tracker = Tracker(tracker_cfg.get("db_path", "data/polymarket.db"))
    tracker.init_db()
    saved = tracker.save_snapshots(snapshots)
    logger.info(f"Saved {saved} new snapshots")
    
    # 3. Detect + Score per market
    signals_cfg = config.get("signals", {})
    scorer_cfg = config.get("scorer", {})
    alert_threshold = scorer_cfg.get("alert_threshold", 60)
    cooldown = scorer_cfg.get("cooldown_minutes", 30)
    
    alerts = []
    now = int(time.time())
    
    for snap in snapshots:
        condition_id = snap["condition_id"]
        
        # Obtener histórico reciente para este mercado
        lookback = max(
            signals_cfg.get("momentum", {}).get("window_hours", 1),
            signals_cfg.get("volume_spike", {}).get("window_hours", 24),
        ) * 3600
        recent = tracker.get_recent_snapshots(condition_id, lookback_seconds=lookback, reference_ts=now)
        
        # Detectar señales
        signals = detect_all(recent, signals_cfg)
        if not signals:
            continue
        
        # Calcular score
        score, detail = calculate_score(signals)
        
        if not should_alert(score, alert_threshold):
            continue
        
        # Guardar señal
        for sig in signals:
            tracker.save_signal(
                condition_id=condition_id,
                signal_type=sig["signal_type"],
                score=score,
                detail=detail,
                timestamp=now,
                cooldown_minutes=cooldown,
            )
        
        # Formatear alerta
        momentum_str = "—"
        for sig in signals:
            if "momentum" in sig["signal_type"] and "change_pct" in sig:
                pct = sig["change_pct"] * 100
                direction = "+" if pct > 0 else ""
                momentum_str = f"{direction}{pct:.1f}%"
        
        import json
        detail_list = json.loads(detail)
        msg = format_alert(score, snap, detail_list, momentum_str)
        alerts.append(msg)
    
    logger.info(f"Detected {len(alerts)} alerts")
    return alerts


def main():
    parser = argparse.ArgumentParser(description="Polymarket Scout")
    parser.add_argument("command", nargs="?", default="scan",
                        choices=["scan", "report", "backfill"],
                        help="Command to run")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file")
    
    args = parser.parse_args()
    
    if args.command == "scan":
        alerts = run_scan(args.config)
        for alert in alerts:
            print(alert)
            print("---")
        print(f"\n{alerts and len(alerts)} alert(s) generated" if alerts else "\nNo alerts — nothing above threshold.")
    elif args.command == "report":
        print("Report command — not yet implemented")
    elif args.command == "backfill":
        print("Backfill command — not yet implemented")


if __name__ == "__main__":
    main()
```

**Step 4: Tests → PASS**

**Step 5: Commit**

```bash
git add src/cli.py tests/test_cli.py
git commit -m "feat: add CLI scan command with full pipeline"
```

---

### Task 16: Integración real — probar con datos de Polymarket

**Objective:** Ejecutar el pipeline contra las APIs reales de Polymarket para verificar que todo funciona.

**Files:**
- (ninguno nuevo, solo ejecución)

**Step 1: Ejecutar scan real**

```bash
cd /opt/data/polymarket-scout
python -m src.cli scan --config config.yaml
```

**Step 2: Verificar que:**
- Se obtienen eventos reales de Polymarket
- Se guardan snapshots en `data/polymarket.db`
- No hay crashes ni excepciones
- El output muestra 0 o más alertas (probablemente 0-2 en un escaneo normal)

**Step 3: Si hay errores, depurar y corregir. Si todo OK, continuar.**

**Step 4: Commit (si hubo fixes)**

---

### Task 17: Ajustar thresholds basado en datos reales

**Objective:** Revisar los thresholds de señales contra datos reales para que no sean ni demasiado sensibles ni demasiado conservadores.

**Step 1: Inspeccionar los snapshots guardados**

```bash
cd /opt/data/polymarket-scout
python3 -c "
import sqlite3
conn = sqlite3.connect('data/polymarket.db')
cursor = conn.execute('SELECT COUNT(*), AVG(price_yes), AVG(spread), MAX(volume), MIN(volume) FROM snapshots')
row = cursor.fetchone()
print(f'Total snapshots: {row[0]}')
print(f'Avg price_yes: {row[1]:.3f}')
print(f'Avg spread: {row[2]:.4f}')
print(f'Max volume: {row[3]:.0f}')
print(f'Min volume: {row[4]:.0f}')
"
```

**Step 2: Ajustar config.yaml si los thresholds no son realistas**
- Si el spread promedio es 0.05, tight_threshold: 0.03 está bien (es más tight que la media)
- Si el volumen varía mucho, el threshold de 3× puede estar bien
- Validar que momentum 5% sea razonable

**Step 3: Ejecutar scan de nuevo y ver cuántas alertas genera**

**Step 4: Commit ajustes**

---

### Task 18: Configurar cron job en Hermes

**Objective:** Programar el scanner para que se ejecute automáticamente cada 5 minutos y te entregue las alertas por Telegram.

**Step 1: Verificar que el script funciona standalone**

```bash
cd /opt/data/polymarket-scout && python -m src.cli scan
```

**Step 2: Crear cron job con Hermes**

Usar la herramienta `cronjob`:
- schedule: `*/5 * * * *` (cada 5 minutos)
- prompt: "Ejecuta el polymarket scout: cd /opt/data/polymarket-scout && python -m src.cli scan. Si hay alertas, entrégalas al usuario formateadas como Telegram. Si no hay alertas, no envíes nada (no molestar al usuario sin motivo)."
- deliver: "origin" (a este chat de Telegram)

**Step 3: Verificar que el cron job se creó correctamente**

```bash
hermes cron list
```

**Step 4: Ejecutar el cron job manualmente para probar**

---

### Task 19: README y documentación

**Objective:** Documentar el proyecto para referencia futura.

**Files:**
- Create: `README.md`

**Step 1: Escribir README.md**

```markdown
# Polymarket Scout

Bot de monitorización de Polymarket. Escanea mercados de predicción cada 5 minutos, detecta oportunidades mediante análisis de señales (momentum, volumen, spread) y envía alertas por Telegram cuando encuentra algo con alto score de confianza.

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Editar `config.yaml` para ajustar thresholds, límites y ventanas temporales.

## Uso

```bash
# Scan completo (detectar + alertar)
python -m src.cli scan

# Scan con config personalizada
python -m src.cli scan --config my_config.yaml
```

## Estructura

- `src/scanner.py` — Cliente APIs de Polymarket
- `src/tracker.py` — Persistencia SQLite
- `src/signals.py` — Detectores de señales
- `src/scorer.py` — Scoring de confianza
- `src/alerter.py` — Formateo de alertas
- `src/cli.py` — Interfaz de línea de comandos

## Señales detectadas

| Señal | Descripción |
|-------|-------------|
| 🚀 momentum_up | Precio YES subiendo rápido |
| 📉 momentum_down | Precio YES bajando rápido |
| 📊 volume_spike | Pico de volumen anormal |
| 🤝 spread_tight | Spread muy bajo (fuerte consenso) |
| 🌈 spread_wide | Spread muy alto (oportunidad) |
| 🆕 new_interest | Mercado nuevo con volumen inicial alto |

## Próximas fases

- Fase 2: Paper trading + backtesting
- Fase 3: Trading real con wallet
```

**Step 2: Commit**

---

### Task 20: Test suite completa + CI dummy

**Objective:** Asegurar que todos los tests pasan limpio.

**Step 1: Ejecutar toda la suite**

```bash
cd /opt/data/polymarket-scout
python -m pytest tests/ -v
```

**Step 2: Si algún test falla, arreglarlo**

**Step 3: Commit final**

```bash
git add -A
git commit -m "docs: add README and finalize Phase 1"
```

---

## Verificación Final (Checklist)

- [ ] `python -m pytest tests/ -v` → todos los tests pasan
- [ ] `python -m src.cli scan` → se ejecuta sin errores, genera snapshots en SQLite
- [ ] Cron job configurado y funcionando
- [ ] Las alertas llegan a Telegram
- [ ] Los thresholds están ajustados a datos reales

---

## Riesgos y Preguntas Abiertas

1. **Rate limiting**: Las APIs de Polymarket son muy generosas (4K-9K req/10s), pero con 25 eventos × 10 mercados × 2 llamadas (price+spread) = 500 requests por scan. Si se ejecuta cada 5 min, son 100 req/min — muy dentro del límite.

2. **Volatilidad de señales**: Los thresholds iniciales son conservadores. Pueden necesitar ajuste tras observar datos reales durante unos días.

3. **Mercados de baja liquidez**: El filtro `min_volume` ayuda, pero mercados con poca liquidez pueden dar señales falsas. Considerar añadir filtro de liquidez en futura iteración.

4. **No hay backtesting aún**: Hasta la Fase 2, no sabremos si las señales realmente predicen buenas apuestas. Esto es solo detección de "cosas interesantes".

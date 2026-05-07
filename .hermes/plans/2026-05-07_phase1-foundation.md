# Fase 1 — Fundación: Migración a Arquitectura Asíncrona

**Plan de implementación** | 7 Mayo 2026  
**Branch:** `feat/v2-phase1-foundation`

## Objetivo

Transformar Scout Lab de un script secuencial síncrono (`urllib.request`) a una arquitectura asíncrona basada en `asyncio` + `aiohttp`, implementando la capa Radar, el Selection Engine, el Rate-Limit Budget Manager y SQLite WAL.

## Estrategia de Migración

**Coexistencia, no reescritura total.** Los nuevos módulos asíncronos se crean en paralelo a los existentes. El código legacy (v1.0) sigue funcionando sin cambios. La migración se completa cuando el nuevo `cli_async.py` reemplaza al antiguo.

## Tareas (en orden)

### 1. Instalar dependencias asíncronas
- Añadir `aiohttp>=3.9`, `aiosqlite>=0.20` a `requirements.txt`
- Añadir `pytest-asyncio` para tests asíncronos
- Instalar en el venv del proyecto

### 2. SQLite → WAL mode
- Modificar `tracker.py`: añadir `PRAGMA journal_mode=WAL;` en `init_db()`
- Esto permite lecturas concurrentes sin bloquear escrituras (crítico para multi-proceso futuro)
- Verificar que todos los tests existentes pasan

### 3. Rate-Limit Budget Manager
- Nuevo: `src/rate_limiter.py`
- Implementar Token Bucket Algorithm con 3 presupuestos:
  - `reconciliation` (70%): para snapshots REST del CLOB
  - `onboarding` (20%): para nuevos mercados que entran al Top 50
  - `ad_hoc` (10%): para debug/dashboard
- Tests unitarios: `tests/test_rate_limiter.py`

### 4. Async Scanner (Radar Layer)
- Nuevo: `src/async_scanner.py`
- Clase `AsyncPolymarketScanner` con `aiohttp.ClientSession`
- Métodos async:
  - `get_events_async()` — polling Gamma API
  - `get_price_async()` / `get_spread_async()` — CLOB queries con rate-limit awareness
  - `scan_markets_async()` — versión async del scan actual
- Tests: `tests/test_async_scanner.py`

### 5. Selection Engine
- Nuevo: `src/selection_engine.py`
- Clase `SelectionEngine` que rankea mercados por score compuesto
- Fórmula: `score = log10(vol_24h)*0.4 + log10(liq)*0.3 + recency*0.2 + spread_penalty*0.1`
- Mantiene Top 50 en Redis (o en memoria con dict si Redis no está disponible)
- Emite eventos de entrada/salida del Top 50
- Tests: `tests/test_selection_engine.py`

### 6. Integración: Async CLI
- Nuevo: `src/cli_async.py`
- Función `run_scan_async()` que orquesta el pipeline asíncrono
- Comando CLI: `python -m src.cli_async scan`
- Coexiste con `src/cli.py` (no se toca)

### 7. Verificación End-to-End
- Ejecutar `python -m src.cli_async scan` contra Polymarket real
- Verificar que produce snapshots similares al pipeline síncrono
- Confirmar que el Rate-Limiter funciona (no bloqueos del CLOB)

## Archivos a modificar/crear

| Archivo | Acción |
|---------|--------|
| `requirements.txt` | Añadir aiohttp, aiosqlite, pytest-asyncio |
| `src/tracker.py` | Añadir PRAGMA WAL en init_db() |
| `src/rate_limiter.py` | NUEVO — Token Bucket |
| `src/async_scanner.py` | NUEVO — Scanner asíncrono |
| `src/selection_engine.py` | NUEVO — Ranking Top 50 |
| `src/cli_async.py` | NUEVO — CLI asíncrono |
| `tests/test_rate_limiter.py` | NUEVO |
| `tests/test_async_scanner.py` | NUEVO |
| `tests/test_selection_engine.py` | NUEVO |
| `config.yaml` | Añadir sección `rate_limiter` y `selection` |

## Verificación

1. `pytest tests/ -v` — todos los tests existentes + nuevos pasan
2. `python -m src.cli_async scan` — ejecuta sin errores contra API real
3. `python -m src.cli scan` — el pipeline legacy sigue funcionando
4. `git diff --stat` — solo cambios intencionados

## Riesgos

- **CLOB rate-limiting**: El async scanner podría hacer demasiadas peticiones muy rápido. Mitigación: el Rate-Limiter se aplica desde el primer momento.
- **aiohttp vs urllib**: Diferente manejo de errores. Mitigación: wrappers consistentes.
- **Compatibilidad SQLite WAL**: Requiere SQLite 3.7+. El contenedor Docker tiene 3.40+. Sin riesgo.

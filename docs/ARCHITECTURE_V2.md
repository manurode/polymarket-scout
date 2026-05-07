# Scout Lab v2.0 — Documento de Arquitectura Lógica

**Autor:** Arquitecto de Sistemas — Quant Research  
**Fecha:** 7 de Mayo, 2026  
**Versión:** 2.0-draft  
**Stack:** Python 3.12+ / asyncio / Redis / PostgreSQL + SQLite / NumPy

---

## Resumen Ejecutivo

Scout Lab v1.0 opera como un bot secuencial de sondeo cada 5 minutos: escanea la Gamma API, detecta señales básicas (momentum, volumen, spread) y ejecuta 5 estrategias deterministas con un tamaño de posición fijo del 5%.

La v2.0 transforma este sistema en una **plataforma institucional de trading algorítmico** con cuatro pilares arquitectónicos:

| Módulo | Propósito | Diferencial Clave |
|--------|-----------|-------------------|
| **Ingestión de Datos** | Resiliencia y baja latencia | WebSocket L2 + Snapshot/Delta Reconciliation |
| **Estrategias Core** | Alfa puro | Market Making con Time-Decay Risk + Arbitraje con Coste de Capital |
| **Oráculos Anti-Manipulación** | Integridad de señal | Detección de Spoofing (OBI vs TFI) + Whale Tracking On-chain |
| **Portfolio Manager** | Asignación dinámica de capital | Multi-Armed Bandit + Sortino + Kelly Fraccional |

---

## Índice

1. [Arquitectura de Ingestión de Datos](#1-arquitectura-de-ingestión-de-datos)
2. [Módulos de Estrategia Core](#2-módulos-de-estrategia-core)
3. [Oráculos de Señales Anti-Manipulación](#3-oráculos-de-señales-anti-manipulación)
4. [Portfolio Manager Dinámico](#4-portfolio-manager-dinámico)
5. [Arquitectura de Sistema](#5-arquitectura-de-sistema)
6. [Gestión de Cuellos de Botella](#6-gestión-de-cuellos-de-botella)

---

## 1. Arquitectura de Ingestión de Datos

### 1.1 Framework Híbrido: Radar Gamma + Deep-Dive CLOB

El cuello de botella crítico de v1.0 es que trata ambas APIs como iguales, quemando el rate-limit del CLOB en mercados irrelevantes. La v2.0 implementa una **arquitectura de dos capas asíncronas**:

```
┌─────────────────────────────────────────────────────────────────┐
│                    INGESTIÓN DE DATOS v2.0                       │
│                                                                  │
│  ┌──────────────────────┐     ┌──────────────────────────────┐  │
│  │   CAPA RADAR (L1)    │     │    CAPA DEEP-DIVE (L2)       │  │
│  │                      │     │                              │  │
│  │ Gamma API            │     │ CLOB WebSocket               │  │
│  │ Polling: 30-60s      │     │ L2 Order Book Streams        │  │
│  │ ~200 mercados        │     │ Top 50 mercados              │  │
│  │                      │     │                              │  │
│  │ • Descubrimiento     │     │ • Order book completo        │  │
│  │ • Precios Gamma      │     │ • Deltas en tiempo real      │  │
│  │ • Volumen/Liquidez   │     │ • Spread real                │  │
│  │ • Nuevos mercados    │     │ • Trade prints (ejecuciones)  │  │
│  │                      │     │                              │  │
│  └──────────┬───────────┘     └──────────────┬───────────────┘  │
│             │                                │                   │
│             ▼                                ▼                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              SELECTION ENGINE (Top 50)                    │   │
│  │                                                          │   │
│  │  Score = f(volume_24h, liquidity, time_to_expiry,         │   │
│  │            spread_width, is_active)                       │   │
│  │                                                          │   │
│  │  Mercados que ENTRAN al Top 50 → onboarded al CLOB       │   │
│  │  Mercados que SALEN del Top 50 → desconectados del CLOB  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │           RATE-LIMIT BUDGET MANAGER                       │   │
│  │           (Token Bucket Algorithm)                        │   │
│  │                                                          │   │
│  │  Presupuesto total: ~4 CLOB REST calls / 10s             │   │
│  │  ┌────────────┬──────────────┬──────────────────┐        │   │
│  │  │ 70%        │ 20%          │ 10%              │        │   │
│  │  │ Reconcil.  │ Onboarding   │ Ad-hoc queries   │        │   │
│  │  │ (resync)   │ (new mkts)   │ (debug/dashboard) │        │   │
│  │  └────────────┴──────────────┴──────────────────┘        │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

#### Radar Layer (L1 — Gamma API)

Implementado como un **bucle asíncrono independiente** (`asyncio.Task`) que:

1. **Sondea** `GET /events?limit=100&active=true&closed=false&order=volume` cada 30-60 segundos.
2. **Extrae** todos los mercados anidados con sus `outcomePrices`, `volume`, `liquidity`, `clobTokenIds` y `endDate`.
3. **Persiste** en SQLite local (tabla `radar_snapshots`, con TTL de 24h) para backfill.
4. **Publica** en Redis Pub/Sub el evento `radar:update` con el payload JSON de los top 200 mercados.

**Ventaja crítica:** La Gamma API tolera ~4000 peticiones cada 10 segundos. Nunca seremos bloqueados en esta capa. La capa Radar puede escalar a cientos de mercados sin riesgo.

#### Selection Engine

Un **servicio independiente** suscrito al canal `radar:update` que:

1. **Calcula un ranking score** para cada mercado:
   ```
   score = log10(volume_24h) × 0.4
         + log10(liquidity)   × 0.3
         + recency_factor     × 0.2
         + spread_penalty     × 0.1
   ```
   Donde `recency_factor` decae para mercados cerca de expiración (`endDate < 24h → 0`) y `spread_penalty` penaliza spreads > 15%.

2. **Mantiene dos conjuntos** en Redis:
   - `top50:active` — los 50 condition_id actualmente en el Top 50.
   - `top50:candidates` — mercados que rozan el umbral (siguientes 20).

3. **Emite eventos de ciclo de vida**:
   - `market:enter_top50 {condition_id, token_yes, token_no, ...}` → el WebSocket Manager onboarda el mercado.
   - `market:exit_top50 {condition_id}` → se cierra la conexión WebSocket para ese mercado.

#### CLOB Deep-Dive Layer (L2 — WebSocket)

**NO usamos REST para el Top 50.** El CLOB REST solo se usa para el snapshot inicial (bootstrap) y para reconciliación (resync). Toda la data de trading fluye por WebSocket.

El WebSocket Manager mantiene **una conexión persistente multiplexada** a `wss://ws-subscriptions-clob.polymarket.com` (o el endpoint equivalente), suscribiéndose a:
- Canal `book` para cada token_id del Top 50 (L2 order book).
- Canal `trades` para cada condition_id del Top 50 (últimas ejecuciones).
- Canal `price` para precios last-trade.

**Arquitectura de conexiones:**
- Una única conexión WebSocket con múltiples suscripciones (modelo `subscribe`/`unsubscribe`).
- Si la API de Polymarket no soporta suscripciones dinámicas, un pool de N conexiones (una por cada 10 mercados).
- Heartbeat cada 30 segundos. Timeout de reconexión: 5 segundos sin mensaje → reconnect.

### 1.2 Reconciliación de Estado (Anti-Desync)

Este es el subsistema **más crítico** de toda la arquitectura. Si el order book está corrupto, todas las estrategias operan a ciegas.

#### El Problema

Los WebSockets entregan **deltas** (cambios incrementales) sobre el order book, no snapshots completos. Cada delta tiene un número de secuencia (`seq_num`). Si se pierde un mensaje (latencia de red, reinicio del servidor, bug del cliente), el book local se desincroniza silenciosamente. Operar con un book corrupto es garantía de pérdida.

#### State Machine por Order Book

Cada mercado tiene su propia máquina de estados independiente:

```
                    ┌─────────┐
                    │  INIT   │
                    └────┬────┘
                         │ fetch_rest_snapshot()
                         ▼
          ┌──────────────────────────────┐
          │          CLEAN               │◄────────────┐
          │  (trading habilitado)        │             │
          │                              │             │
          │  • seq_num conocido          │             │
          │  • book íntegro              │             │
          └──────┬───────────────┬───────┘             │
                 │               │                      │
    delta(seq=N+1)│               │ delta(seq > N+1)     │
    (secuencial)  │               │ (gap detectado)       │
                 ▼               ▼                      │
          ┌──────────┐    ┌──────────────┐             │
          │  CLEAN   │    │ RECONCILING  │             │
          │(sin cambio)│   │              │             │
          └──────────┘    │ • trading    │             │
                          │   PAUSADO    │             │
                          │ • fetch      │             │
                          │   snapshot() │─────────────┘
                          │ • replay      │  snapshot OK
                          │   buffered    │  + deltas
                          │   deltas      │  aplicados
                          └──────────────┘
```

**Transiciones:**

| Estado Actual | Evento | Nuevo Estado | Acción |
|---------------|--------|--------------|--------|
| `INIT` | Startup | `CLEAN` | Fetch REST `/book?token_id=X`. Almacenar `seq_num` del snapshot. |
| `CLEAN` | `delta(seq=N+1)` | `CLEAN` | Aplicar delta. Actualizar `seq_num`. |
| `CLEAN` | `delta(seq>N+1)` → gap detectado | `RECONCILING` | **PAUSAR trading de INMEDIATO.** No esperar ni 1ms. El libro está desincronizado por pérdida de paquetes. Iniciar resync. |
| `CLEAN` | Heartbeat sin delta por 30s | `RECONCILING` | Health check (ping WS). Si no responde en 5s → reconnect + resync. |
| `RECONCILING` | `fetch_snapshot()` OK | `CLEAN` | Snapshot REST recibido. Replay de deltas bufferizados con seq > snapshot.seq. **REANUDAR trading.** |
| `RECONCILING` | `fetch_snapshot()` falla | `RECONCILING` | Retry con exponential backoff (1s, 2s, 4s, 8s...). |
| Cualquiera | WS disconnect | `INIT` | Reconectar. Fetch nuevo snapshot. |

#### Buffer de Deltas

Durante el período `RECONCILING`, todos los deltas entrantes se almacenan en un **buffer circular** (máximo 1000 deltas, ~100KB). Cuando llega el snapshot fresco, el sistema:

1. Descarta deltas con `seq_num <= snapshot.seq_num` (ya incluidos en el snapshot).
2. Aplica los deltas restantes en orden secuencial.
3. Si aún hay gaps después del replay → vuelve a `RECONCILING`.

#### Métricas de Salud

El sistema expone métricas en Redis para monitorización:
- `book:{condition_id}:state` → `CLEAN|RECONCILING`
- `book:{condition_id}:gap_count` → contador de gaps en última hora
- `book:{condition_id}:last_delta_age_ms` → ms desde el último delta

#### Estrategia de Degradación

Si un mercado pasa más del 20% del tiempo en `RECONCILING`:
- Se reduce automáticamente al Top 100-150 (pierde prioridad CLOB).
- Se notifica al operador vía Telegram.
- Las estrategias pueden seguir operando con **precios Gamma exclusivamente** (modo degradado), sin order book.

---

## 2. Módulos de Estrategia Core

### 2.1 Market Making Líquido (Captura Pasiva de Spread)

#### Lógica Base

El Market Maker coloca órdenes límite en ambos lados del libro para capturar el spread:

```
Para un mercado con fair_price P y spread S observable:

  Bid (compra):  P - (S/2) × quote_width_multiplier
  Ask (venta):   P + (S/2) × quote_width_multiplier

  Tamaño por orden: position_size_kelly / 2 (mitad en cada lado)
```

Donde `P` (fair price) se calcula como:
- **Primario:** midpoint del best bid/ask del CLOB L2 (vía WebSocket).
- **Fallback:** precio Gamma si el spread CLOB > 5% o el book está en RECONCILING.

#### Quote Width Dinámico

El multiplicador `quote_width_multiplier` se ajusta en tiempo real según tres factores:

```
quote_width_multiplier = base_multiplier
                       × volatility_scalar
                       × inventory_scalar
                       × time_decay_scalar
```

| Factor | Rango | Lógica |
|--------|-------|--------|
| `base_multiplier` | 1.0 | Constante de calibración |
| `volatility_scalar` | [0.8, 2.0] | `1 + (realized_vol_1h / avg_vol)`. Más volátil → spreads más anchos |
| `inventory_scalar` | [0.5, 2.0] | Si estamos long YES → spread más ancho en el lado YES (queremos vender, no comprar más). Si estamos short → spread más ancho en el lado NO |
| `time_decay_scalar` | [0.8, 3.0] | Ver sección siguiente |

#### Sistema de Límite de Riesgo Dinámico (Time-Decay Risk) ⭐

Este es el diferenciador crítico para mercados binarios con fecha de expiración. Los mercados de predicción NO son perpetuos — convergen a 0 o 1 en una fecha fija. Mantener inventario cerca de la expiración es **riesgo direccional puro**.

**Función de Decaimiento Temporal:**

```
Sea T = duración total del mercado (endDate - createdAt) en segundos
Sea t = tiempo transcurrido desde createdAt
Sea τ = t / T  (fracción de vida consumida, rango [0, 1])

risk_multiplier(τ) = max(
    RISK_FLOOR,                    // 0.05 — nunca riesgo cero absoluto
    1 - (τ - TRANSITION_POINT)²    // TRANSITION_POINT = 0.70
)
```

**Visualmente:**

```
risk_multiplier
  1.0 ┤████████████████████▌
      │                    ▐▌
  0.8 ┤                    ▐▌
      │                     ▐▌
  0.6 ┤                      ▐▌
      │                       ▐▌
  0.4 ┤                        ▐█▌
      │                          ▐█▌
  0.2 ┤                            ▐█▌
      │                              ▐███▌
  0.05┤                                   ▐██████████
      └────────────────────────────────────────────── τ
      0%         50%        70%    85%    95%   100%
                  ▲
           TRANSITION_POINT
```

**Interpretación:**
- Durante el 70% inicial de la vida del mercado: inventario sin restricciones (risk_multiplier ≈ 1.0).
- Del 70% al 85%: reducción suave de inventario (risk_multiplier → 0.8).
- Del 85% al 95%: contracción agresiva (risk_multiplier → 0.3).
- Último 5% de vida: liquidación forzosa (risk_multiplier → 0.05).

**Aplicación al Inventario:**

```
max_inventory_usd = BASE_INVENTORY_CAP × risk_multiplier(τ) × liquidity_factor
```

Donde:
- `BASE_INVENTORY_CAP` = $500 (configurable).
- `liquidity_factor` = `min(1.0, market_liquidity / MIN_LIQUIDITY)` — reduce inventario en mercados ilíquidos.

**Protocolo de Liquidación Forzosa (τ > 0.95):**

Cuando el mercado entra en la zona crítica (último 5% de vida, típicamente las últimas 12-24 horas):

1. **Cancelación inmediata** de todas las órdenes pasivas abiertas para este mercado.
2. **Liquidación agresiva**: el sistema coloca órdenes a mercado (taker) para aplanar el inventario.
3. **Aceptación de slippage**: dispuesto a cruzar hasta 2% del spread para salir.
4. **Modo solo-cierre**: no se abren nuevas posiciones en este mercado. Solo se permite reducir暴露.
5. **Notificación**: alerta al operador con el P&L final.

#### Protección Anti-Selección Adversa

Si cualquiera de estas condiciones se cumple, el Market Maker **cancela todas las órdenes y espera**:

1. **OBI extremo**: `|OBI| > 0.70` en una dirección (alguien está acumulando agresivamente).
2. **Whale detectado**: una transacción on-chain de un Alpha Whale (ver §3.2) en este mercado en los últimos 60 segundos.
3. **Flash crash/spike**: precio moviéndose > 5% en < 30 segundos.
4. **Book en RECONCILING**: datos no confiables.

Tiempo de reincorporación: 30 segundos después de que la condición desaparezca.

#### Protección contra Flujo Tóxico — Markout Analysis ⭐

**El problema del flujo informado:** Cuando una ballena sabe algo que el mercado aún no ha descontado (ej: una noticia de última hora en Twitter), comprará agresivamente todo el inventario del lado YES. El Market Maker captura el spread (ej: $2 de beneficio), pero un segundo después el precio real salta un 20% en su contra — y la posición acumulada pierde $50. El MM ha ganado cacahuetes y perdido elefantes.

**Detección mediante Markout Analysis:**

El bot trackea el P&L de sus propias órdenes pasivas en intervalos fijos después de ser ejecutadas:

```
MARKOUT MATRIX (por mercado, por hora):

                     t+1s    t+5s    t+10s   t+60s
  Trade #142 (YES)   +$0.05   -$0.30   -$1.20   -$3.50  ← TOXIC FLOW
  Trade #143 (NO)    +$0.10   +$0.12   +$0.08   +$0.15  ← Clean spread
  Trade #144 (YES)   +$0.03   -$0.05   -$0.10   -$0.80  ← Moderadamente tóxico

  Markout Score (t+10s) = Σ P&L_t+10s / n_trades
```

**Cálculo del Markout Score (MS):**

```
MS_short = media(P&L_t+1s) / spread_capturado   // impacto inmediato (1s)
MS_medium = media(P&L_t+5s) / spread_capturado   // impacto a medio plazo (5s)
MS_long = media(P&L_t+10s) / spread_capturado    // impacto estructural (10s)

Markout_Toxicity = -1 × min(0, MS_short, MS_medium, MS_long)
                 // Solo positivo si ALGÚN intervalo tiene P&L negativo
                 // Rango: [0, ∞). 0 = flujo limpio, > 1 = flujo muy tóxico
```

**Umbrales de respuesta:**

| Markout_Toxicity | Clasificación | Acción del Market Maker |
|------------------|---------------|------------------------|
| < 0.3 | Flujo limpio | Operar normal |
| 0.3 – 0.7 | Flujo mixto | Ampliar spread asimétricamente: +50% en el lado donde se pierde dinero. Reducir size al 75%. |
| 0.7 – 1.5 | Flujo tóxico | Ampliar spread al 300% del nominal en el lado afectado. Reducir size al 50%. Alerta al operador. |
| > 1.5 | Altamente tóxico | **Pausar Market Making** en este mercado por 30 minutos. Evaluar si hay evento noticioso no descontado. |

**Implementación:**

- Ventana de cálculo: últimos 20 trades ejecutados (o última hora, lo que sea mayor).
- Se mantiene un buffer circular en memoria con el `(trade_id, entry_price, size, timestamp, markout_prices[])`.
- Cada segundo, el sistema recalcula el P&L mark-to-market de los trades abiertos y actualiza la matriz.
- El Markout Score se recalcula cada 10 segundos y se publica en Redis (`market:{id}:markout_toxicity`).

**Relación con Whale Tracking (§3.2):** Si el Markout Score se dispara simultáneamente con actividad de una Alpha Whale en el mismo mercado, la toxicidad se confirma como flujo informado (no ruido). En ese caso, la pausa es inmediata y se extiende a 60 minutos.

---

### 2.2 Arbitraje de Correlación

#### Detección de Mercados Anidados

El **Correlation Graph Builder** escanea todos los mercados activos y construye un grafo dirigido de relaciones lógicas:

**Paso 1 — Embeddings de texto:**
- Para cada mercado activo, genera un embedding de su `question` usando `sentence-transformers` (modelo `all-MiniLM-L6-v2`, ~80MB, inferencia < 1ms por texto).
- Calcula similitud coseno entre todos los pares de mercados.

**Paso 2 — Clasificación de relaciones:**
- Pares con similitud > 0.75 pasan a un clasificador LLM ligero (modelo local pequeño como Llama-3B o API econ poor) que determina el tipo de relación:
  - **Implicación estricta (A ⊆ B):** "Si A gana, B necesariamente gana." Ej: "Trump gana 2028" ⊆ "Republicano gana 2028".
  - **Exclusión mutua (A ∩ B = ∅):** "A y B no pueden ocurrir simultáneamente." Ej: dos outcomes de un mismo evento multi-opción.
  - **Independencia condicional:** Mercados relacionados pero sin relación lógica estricta.

**Paso 3 — Detección de multi-outcome:**
- Mercados que pertenecen al mismo evento (mismo `event_id` en Gamma) y tienen 3+ outcomes.
- La suma de probabilidades debe ser ≤ 1.0 (idealmente = 1.0, pero el mercado puede ser ineficiente).

#### Señal de Arbitraje

**Tipo 1 — Implicación (A ⊆ B):**
```
si P(A) > P(B):
    Arbitraje: COMPRAR B (infravalorado), VENDER A (sobrevalorado)
    Beneficio garantizado si A ⊆ B: P(A) - P(B) por unidad de $1
```

**Tipo 2 — Multi-outcome (suma < 1):**
```
si Σ P(outcome_i) < 1.0 para todos los outcomes i de un evento:
    Arbitraje: COMPRAR una unidad de CADA outcome
    Beneficio garantizado: 1.0 - Σ P(outcome_i) por cada $1 de ciclo completo
```

**Tipo 3 — Exclusión mutua (suma > 1):**
```
si A ∩ B = ∅ y P(A) + P(B) > 1.0:
    Arbitraje: VENDER ambas (equivalente a comprar NO en ambas)
    Beneficio garantizado: P(A) + P(B) - 1.0
```

#### Fórmula de Coste de Capital (Capital Lock-Up) ⭐

**El problema:** No todo arbitraje merece la pena. Si el capital queda inmovilizado 9 meses para ganar un 2%, el coste de oportunidad destruye el retorno real.

**Cálculo de Coste de Capital:**

```python
# Para un arbitraje que requiere inmovilizar capital C durante D días
# con un beneficio bruto B:

días_hasta_resolución = max(
    mercado_A.endDate,
    mercado_B.endDate
) - now  # el capital queda bloqueado hasta que TODOS los mercados resuelvan

retorno_anualizado = (beneficio_bruto / capital_inmovilizado) * (365 / días_hasta_resolución)

# Penalización por coste de oportunidad:
retorno_ajustado = retorno_anualizado - TASA_LIBRE_RIESGO - PRIMA_RIESGO

# TASA_LIBRE_RIESGO = 0.05  (5% — bonos del tesoro USA)
# PRIMA_RIESGO = 0.15       (15% — compensación por riesgo de smart contract,
#                             riesgo de contraparte, incertidumbre de resolución)
# HURDLE_RATE = 0.20        (20% mínimo exigido para inmovilizar capital)
```

**Decisión:** Solo ejecutar si `retorno_anualizado > HURDLE_RATE` (20%).

**Ranking de Oportunidades:**

Entre múltiples arbitrajes disponibles, se ordenan por `retorno_ajustado` descendente, no por beneficio absoluto. Esto prioriza operaciones de alta rotación de capital sobre grandes inmovilizaciones de bajo rendimiento.

**Monitoreo Post-Ejecución:**

Una vez ejecutado un arbitraje:
- El sistema monitorea si la relación lógica se rompe (ej: aparece un tercer candidato que altera las implicaciones).
- Si la ineficiencia se cierra antes de la resolución (P(A) cae por debajo de P(B)), el sistema cierra el arbitraje anticipadamente para liberar capital.
- Las posiciones de arbitraje tienen **prioridad de cierre anticipado** sobre posiciones direccionales — liberar capital para nuevas oportunidades es más valioso que exprimir el último 0.1% de un arbitraje.

#### Legging Risk — Ejecución Fill-or-Kill (FOK) a Nivel de Aplicación ⭐

**El problema:** Polymarket no tiene un smart contract nativo que agrupe operaciones multi-mercado de forma atómica. Cuando el bot ejecuta un arbitraje de correlación (ej: comprar A, vender B), las dos órdenes se envían secuencialmente. Si la primera se ejecuta y la segunda falla (precio movido, liquidez evaporada), el bot queda atrapado en una **posición direccional no deseada** (solo tiene A, sin el hedge de B).

**Solución — Fill-or-Kill (FOK) simulado:**

```
ALGORITMO DE EJECUCIÓN ATÓMICA (Atomic Execution Emulator):

1. IDENTIFICAR el mercado con menor liquidez entre A y B.
   → Este es el "cuello de botella" — disparar PRIMERO aquí.

2. ORDEN 1: Enviar orden límite agresiva en el mercado ILÍQUIDO.
   → Monitorear confirmación vía WebSocket (trade print).
   → Timeout: 500ms. Si no se llena → ABORTAR todo.

3. CONFIRMACIÓN: ¿El WebSocket confirmó que la Orden 1 se ejecutó (fill)?
   ├── NO (timeout 500ms): Cancelar Orden 1 si aún está abierta. ABORTAR arbitraje.
   └── SÍ: Inmediatamente disparar Orden 2.

4. ORDEN 2: Enviar orden en el mercado LÍQUIDO para cubrir (hedge).
   → Si esta orden falla → ALERTA CRÍTICA. El bot tiene exposición direccional.
   → Emergency unwind: liquidar posición abierta a mercado inmediatamente
     (aceptando hasta 1% de slippage).

5. REGISTRAR resultado: arbitraje completado (éxito) o abortado (fallo sin pérdida
   neta, o fallo con pérdida de slippage en emergency unwind).
```

**Principio clave:** La orden en el mercado ilíquido se dispara **primero** porque es la restricción activa. Si no se puede llenar, el arbitraje simplemente no ocurre y no hay pérdida. La orden en el mercado líquido (hedge) solo se dispara **después de confirmación**, minimizando la ventana de Legging Risk.

**Timeout y reintentos:**
- Timeout de confirmación WebSocket: 500ms.
- Si la Orden 1 se llena parcialmente, la Orden 2 se ajusta proporcionalmente para mantener el ratio de cobertura.
- Máximo 3 reintentos por oportunidad de arbitraje antes de descartarla.

---

### 2.3 Execution & Gas Manager (Latencia Blockchain y MEV) ⭐

**El elefante en la habitación:** El documento especifica un Target Latency de < 25ms para el Critical Path interno del motor. Esto es correcto para la toma de decisiones, pero ignora la latencia real de ejecución en Polygon.

#### Realidad de Polygon

| Parámetro | Valor |
|-----------|-------|
| Tiempo de bloque | ~2 segundos |
| Tiempo de inclusión en mempool pública | Variable (1-30 segundos en congestión) |
| Riesgo MEV en mempool | **Crítico** — cualquier bot puede ver tu tx, copiarla, y front-runnearte con gas más alto |
| Coste de gas (bajo) | ~$0.01-0.05 por tx en condiciones normales |
| Priority fee competitiva | $0.50-2.00 para inclusión garantizada en siguiente bloque |

**Regla de oro:** Las órdenes Maker (límite) en Polymarket NO pagan gas — usan firmas EIP-712 off-chain. Solo las órdenes Taker (mercado) incurren en costes de gas en Polygon. Por tanto, el Market Making es inmune al problema de gas. Pero las liquidaciones forzosas y los arbitrajes (que requieren ejecución a mercado) **sí** están expuestos.

#### Módulo de Gestión de Gas

Un servicio independiente que evalúa dinámicamente el coste de ejecución:

```
┌──────────────────────────────────────────────────────────┐
│              EXECUTION & GAS MANAGER                     │
│                                                          │
│  ┌─────────────────────┐   ┌──────────────────────────┐ │
│  │ Gas Price Oracle    │   │ MEV Protection Strategy  │ │
│  │                     │   │                          │ │
│  │ • Polygon RPC       │   │ • Mempool privada        │ │
│  │   eth_gasPrice      │   │   (Flashbots / bloXroute)│ │
│  │ • Priority fee      │   │ • Envío directo a        │ │
│  │   histórico (7d)    │   │   validadores            │ │
│  │ • Gas token (POL)   │   │ • Simulación pre-flight  │ │
│  └──────────┬──────────┘   └──────────┬───────────────┘ │
│             │                          │                  │
│             ▼                          ▼                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │          COSTE DE EJECUCIÓN AJUSTADO                 │ │
│  │                                                      │ │
│  │  coste_total = gas_estimado × (base_fee + priority)  │ │
│  │              + slippage_esperado                     │ │
│  │                                                      │ │
│  │  retorno_ajustado = beneficio_bruto - coste_total    │ │
│  │                                                      │ │
│  │  Solo ejecutar si retorno_ajustado > HURDLE_RATE     │ │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

**Integración con el Capital Lock-Up (Arbitraje):**

La fórmula de `retorno_ajustado` del §2.2 se extiende:

```python
# Coste total de ejecución en Polygon
coste_gas_usd = (gas_estimado × (base_fee_gwei + priority_fee_gwei) × 1e-9) × precio_pol_usd

# Slippage estimado por cruzar el spread
slippage_usd = capital_inmovilizado × spread_mercado × 0.5  # asume peor caso: cruzar mitad del spread

coste_ejecucion_total = coste_gas_usd + slippage_usd

# Retorno ajustado FINAL (incorpora tanto coste de capital como coste de ejecución)
retorno_anualizado_final = ((beneficio_bruto - coste_ejecucion_total) / capital_inmovilizado) * (365 / días)
retorno_ajustado_final = retorno_anualizado_final - TASA_LIBRE_RIESGO
```

**Estrategia Anti-MEV:**

| Nivel de Riesgo | Beneficio Estimado | Estrategia de Envío |
|-----------------|-------------------|---------------------|
| Bajo (< $5 profit) | No rentable tras gas + MEV | No ejecutar |
| Medio ($5-$50) | Rentable pero vulnerable | Mempool privada (Flashbots/bloXroute) |
| Alto (> $50) | Muy rentable | Mempool privada + priority fee agresiva (95 percentil) |
| Crítico (> $200) | Arbitraje grande | Simulación pre-flight + bundle atomico + priority fee máxima |

**En Paper Trading (sin ejecución real):**
- El coste de gas y slippage se **simulan** para reflejar condiciones reales.
- Se usa el precio de POL y gas histórico para estimar costes.
- El P&L del paper trading refleja el retorno NETO después de costes simulados.

---

## 3. Oráculos de Señales Anti-Manipulación

### 3.1 Trade Flow vs. Order Book — Detección de Spoofing

#### Fundamento Teórico

El **spoofing** consiste en colocar órdenes límite grandes sin intención de ejecutarlas, para manipular la percepción del order book, y cancelarlas antes de que se ejecuten. Esto infla artificialmente el Order Book Imbalance (OBI) sin dejar huella en el Trade Flow Imbalance (TFI).

La **divergencia OBI–TFI** es la huella dactilar del spoofing.

#### Arquitectura de Cálculo

**Dos streams paralelos desde el WebSocket del CLOB:**

```
┌──────────────────────┐     ┌──────────────────────┐
│   ORDER BOOK STREAM  │     │   TRADE STREAM       │
│   (canal: book)      │     │   (canal: trades)    │
│                      │     │                      │
│ • Deltas L2          │     │ • Ejecuciones reales │
│ • Bids/Asks updates  │     │ • price, size, side  │
│ • Cancelaciones      │     │ • timestamp          │
└──────────┬───────────┘     └──────────┬───────────┘
           │                            │
           ▼                            ▼
┌──────────────────────┐     ┌──────────────────────┐
│   BookAnalyzer       │     │   TradeAggregator    │
│                      │     │                      │
│ • Mantiene L2 local  │     │ • Buckets temporales │
│ • Calcula OBI en     │     │   (30s, 1m, 5m)     │
│   tiempo real        │     │ • Calcula TFI por    │
│ • Tracking de        │     │   bucket             │
│   cancelaciones      │     │ • Volumen comprador  │
│   grandes (>$1K)     │     │   vs vendedor        │
└──────────┬───────────┘     └──────────┬───────────┘
           │                            │
           └──────────┬─────────────────┘
                      ▼
           ┌──────────────────────┐
           │   SpoofDetector      │
           │                      │
           │ D = |OBI - TFI|      │
           │ × cancel_rate_factor │
           │                      │
           │ → spoofing_score     │
           └──────────────────────┘
```

#### Fórmulas

**Order Book Imbalance (OBI):**

```
OBI_t = (Σ bid_volume_level_i - Σ ask_volume_level_j) /
        (Σ bid_volume_level_i + Σ ask_volume_level_j)

para los top N niveles del libro (N=10 por defecto)
Rango: [-1, +1]
```

**Trade Flow Imbalance (TFI):**

```
TFI_window = (volume_compras_window - volume_ventas_window) /
             (volume_compras_window + volume_ventas_window)

donde volumen_compras = trades donde el taker compró YES
      volumen_ventas  = trades donde el taker vendió YES
Rango: [-1, +1]
```

**Divergence Score (D):**

```
D_raw = |OBI_t - TFI_t|
D = D_raw × (1 + cancel_rate_factor)

donde cancel_rate_factor = min(1.0, large_cancellations_last_60s / AVG_CANCEL_RATE)
      large_cancellations = cancelaciones de órdenes > $1K en valor nocional
      AVG_CANCEL_RATE = media móvil de cancelaciones en este mercado
```

**Spoofing Score (S):**

```
S = D × confidence_weight

confidence_weight = min(1.0, n_observations / MIN_OBSERVATIONS)
                  × (1 - market_age_penalty)

// Mercados muy nuevos tienen poca historia → menos confianza
market_age_penalty = max(0, 1 - market_age_hours / 24)
```

#### Umbrales de Acción

| Spoofing Score | Clasificación | Acción |
|----------------|---------------|--------|
| S < 0.3 | Normal | Sin acción |
| 0.3 ≤ S < 0.5 | Sospechoso | Reducir position size al 75% |
| 0.5 ≤ S < 0.7 | Probable Spoofing | Ignorar OBI. Usar solo TFI para señales. Reducir size al 50%. |
| S ≥ 0.7 | Spoofing Confirmado | Pausar trading. Alerta al operador. No abrir nuevas posiciones. |

#### Aplicación en Señales

Cuando `S ≥ 0.5`, el sistema:

1. **Anula OBI como fuente de señal:** El momentum basado en order book NO se usa.
2. **Usa TFI como señal direccional autoritativa:** Si hay volumen comprador real, la dirección es genuina.
3. **Reduce el tamaño de posición:** Multiplica por `position_size_multiplier = max(0.25, 1 - S)`.
4. **Endurece los stops:** Stop-loss más cerrado (`-10%` en vez de `-20%`), take-profit más cercano (`+5%` en vez de `+10%`).

---

### 3.2 Whale Tracking On-Chain

#### Arquitectura

```
┌──────────────────────────────────────────────────────────────┐
│                   WHALE TRACKER DAEMON                        │
│                                                              │
│  ┌─────────────────────┐     ┌───────────────────────────┐  │
│  │ Polygon Indexer     │     │ Wallet Profiling Engine   │  │
│  │                     │     │                           │  │
│  │ • Alchemy/QuickNode │     │ • Historical P&L          │  │
│  │   WebSocket stream  │     │ • Win rate                │  │
│  │                     │     │ • Avg return per trade    │  │
│  │ Contratos CTF:      │     │ • Max drawdown            │  │
│  │ • Transfer (ERC1155)│     │ • Trades per week         │  │
│  │ • Split/Merge       │     │ • Mercados favoritos      │  │
│  │ • PayoutRedemption  │     │                           │  │
│  │                     │     │ → Alpha Whale Score       │  │
│  └──────────┬──────────┘     └───────────┬───────────────┘  │
│             │                            │                   │
│             ▼                            ▼                   │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              PostgreSQL (Wallet Profiles)                │ │
│  │                                                          │ │
│  │ wallets: address, alpha_score, total_pnl, win_rate, ...  │ │
│  │ whale_trades: tx_hash, wallet, condition_id, side, ...   │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              Redis Pub/Sub Interface                     │ │
│  │                                                          │ │
│  │ whale:flow {condition_id, net_flow_1h, net_flow_24h,     │ │
│  │             whale_consensus, top_whales_active}           │ │
│  └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

#### Contratos a Monitorear (Polygon)

| Contrato | Dirección | Eventos Relevantes |
|----------|-----------|-------------------|
| `ConditionalTokens` | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | `TransferSingle`, `TransferBatch` (ERC-1155) |
| `CTFExchange` | (verificar en docs de Polymarket) | `Split`, `Merge`, `Payout` |

**Evento `TransferSingle`** (el más importante):
```solidity
event TransferSingle(
    address indexed operator,
    address indexed from,
    address indexed to,
    uint256 id,        // token ID = condition_id + outcome hash
    uint256 value      // cantidad transferida
);
```

Interpretación:
- `from = 0x0` → mint (split de colateral → tokens de outcome). Posición ABIERTA.
- `to = 0x0` → burn (merge de tokens → colateral). Posición CERRADA con P&L realizable.
- `from/to = dirección de wallet` → transferencia entre wallets (posible OTC, se sigue la ballena).

#### Alpha Whale Score

Para cada wallet, se calcula un score compuesto cada 24 horas (o al detectar nueva actividad significativa):

```
AlphaWhaleScore = (
    pnl_percentile      × 0.40   // P&L total histórico normalizado [0,1]
  + win_rate            × 0.25   // % de trades ganadores
  + sortino_normalized  × 0.20   // Sortino ratio de sus trades, normalizado
  + consistency_score   × 0.15   // min(1.0, trades_per_week / 5)
)

// Umbral de Alpha Whale: top 5% del score (> 0.85 típicamente)
```

**Optimización de memoria:**
- Solo se perfilan wallets con > 50 transacciones históricas.
- Los perfiles se recalcular en lote cada 6 horas (no en tiempo real).
- Los eventos nuevos se acumulan en Redis y se procesan en batch.

#### Wallet Clustering — Defensa Anti-Sybil ⭐

**El problema de la ceguera por Sybil:** Los operadores profesionales en Web3 no usan una sola cartera. Dividen sus fondos en docenas de carteras pequeñas precisamente para evitar que bots como el nuestro sigan su rastro y saturen sus operaciones. Una ballena con $500K puede operar desde 20 wallets de $25K cada una — y nuestro sistema no la detectaría porque ninguna wallet individual supera el umbral de Alpha Whale.

**Solución: Clustering por patrones de financiación y comportamiento.**

El **Wallet Profiling Engine** se extiende con un módulo de clustering que agrupa wallets relacionadas:

```
ALGORITMO DE CLUSTERING (3 dimensiones):

1. CLUSTERING POR FINANCIACIÓN (Funding Cluster):
   - Agrupar wallets que recibieron fondos iniciales desde:
     a) La misma dirección de exchange (ej: misma hot wallet de Binance).
     b) En la misma ventana temporal (±2 horas).
     c) Con montos similares (±20% de tolerancia).
   → Estas wallets probablemente pertenecen al mismo operador humano
     que fondeó varias cuentas desde el mismo exchange.

2. CLUSTERING POR COMPORTAMIENTO (Behavioral Cluster):
   - Agrupar wallets que ejecutan las mismas operaciones en la misma
     dirección (mismo condition_id, mismo lado YES/NO) en ventanas de:
     a) < 500ms → operación coordinada (mismo bot).
     b) < 5s → misma decisión manual (mismo humano).
   → Si 5 wallets compran YES en el mismo mercado con diferencia
     de milisegundos, son el mismo operador.

3. CLUSTERING POR PATRÓN TEMPORAL (Temporal Cluster):
   - Agrupar wallets cuyos patrones de actividad (horas del día,
     días de la semana) tienen correlación > 0.85.
   → Si dos wallets solo operan de 14:00 a 22:00 UTC, probablemente
     son el mismo trader en su horario laboral.
```

**Score de Cluster (Cluster Score):**

```
Para cada cluster C identificado:

cluster_total_pnl = Σ wallet_pnl  para toda wallet en C
cluster_total_volume = Σ wallet_volume  para toda wallet en C
cluster_num_wallets = |C|

ClusterAlphaScore = (
    pnl_percentile_cluster   × 0.40
  + win_rate_cluster         × 0.25
  + sortino_cluster          × 0.20
  + consistency_cluster      × 0.15
) × cluster_cohesion_factor

donde cluster_cohesion_factor = min(1.0, 0.7 + 0.3 × log2(cluster_num_wallets))
  // Penaliza clusters poco cohesionados (pocas wallets = baja certeza de Sybil)
  // 1 wallet  → factor 0.70 (podría ser wallet normal, no Sybil)
  // 4 wallets → factor 1.00 (alta certeza: es un operador fragmentado)
  // 8 wallets → factor 1.10 (certeza total, bonus por detección)
```

**Uso en Conviction Multiplier:**

El `net_whale_flow` y `whale_consensus` se calculan ahora a nivel de **cluster**, no de wallet individual:

```
net_whale_flow_1h = Σ (cluster_buy_volume - cluster_sell_volume) en última hora
                   para clusters con ClusterAlphaScore > 0.85

// El volumen de un cluster es la SUMA del volumen de todas sus wallets
// Esto revela el verdadero tamaño de la ballena fragmentada
```

**Ejemplo concreto:**
- Sin clustering: 8 wallets de $25K cada una. Ninguna detectada como Alpha Whale (volumen individual bajo). `CM = 1.0` (neutro).
- Con clustering: 1 cluster de $200K total. ClusterAlphaScore = 0.92 (Alpha Whale confirmada). El sistema ve el volumen agregado real. `CM` se ajusta correctamente.

**Mantenimiento de clusters:**
- Reclustering completo cada 24 horas (proceso batch en PostgreSQL).
- Nuevas wallets se asignan a clusters existentes en tiempo real vía Redis si coinciden en funding + behavioral.
- Clusters inactivos > 30 días se archivan.



#### Whale Flow como Multiplicador de Convicción

Para cada mercado en el Top 50, se calcula en tiempo real:

```
net_whale_flow_1h = Σ (whale_buy_volume - whale_sell_volume) en última hora
                   solo para wallets con AlphaWhaleScore > 0.85

whale_consensus = |whales_bullish - whales_bearish| / total_active_whales
                // 1.0 = todas las ballenas en el mismo lado
                // 0.0 = ballenas divididas equitativamente

whale_zscore = net_whale_flow_1h / std_dev_whale_flow_30d
             // Cuán inusual es este flujo vs histórico
```

**Conviction Multiplier (CM):**
```
CM = 1.0 + (tanh(whale_zscore) × whale_consensus × WHALE_FACTOR)

WHALE_FACTOR = 0.40   // peso máximo que las ballenas pueden añadir

Rango efectivo: [0.60, 1.40]
  — CM < 1.0: ballenas están vendiendo → reducir convicción
  — CM > 1.0: ballenas están comprando → aumentar convicción
```

**Aplicación en el pipeline de trading:**
```
signal_strength_final = signal_strength_base × CM

position_size_final = position_size_kelly × CM
                     // pero nunca excede max_position_size
```

**Principio de seguridad:** CM **nunca** es la señal primaria. Si no hay señal base de la estrategia, la actividad de ballenas por sí sola NO dispara un trade. Las ballenas pueden estar equivocadas, estar haciendo hedging, o manipulando. CM solo amplifica o atenúa convicción existente.

---

## 4. Portfolio Manager Dinámico

### 4.1 Torneo de Estrategias (Multi-Armed Bandit)

#### Formulación MAB

El Portfolio Manager trata cada estrategia como un **brazo** en un problema de Multi-Armed Bandit:

- **Brazos:** momentum_follow, contrarian, consensus_breakout, volume_breakout, market_making, correlation_arb, whale_follow, new_market_yes, etc.
- **Recompensa por ronda:** Sortino Ratio de los trades ejecutados por esa estrategia en la última época (ventana de evaluación).
- **Época:** 6 horas (configurable). Cada época, el bandit reasigna capital.

#### Algoritmo: Thompson Sampling con Priors Beta

En lugar de Epsilon-Greedy (que explora uniformemente sin considerar incertidumbre), usamos **Thompson Sampling**, que es óptimo para entornos con retroalimentación binaria/continua y maneja naturalmente el trade-off exploración/explotación.

**¿Por qué Thompson Sampling y no Epsilon-Greedy?**

- Epsilon-Greedy explora aleatoriamente incluso estrategias que ya sabemos son malas. Thompson Sampling solo explora cuando la incertidumbre es alta.
- Las estrategias en mercados financieros tienen rendimientos ruidosos. Thompson Sampling modela esta incertidumbre explícitamente.
- Converge más rápido al brazo óptimo (menor *regret* acumulado).

**Modelo:**

```
Para cada estrategia i:
  - Éxitos S_i = número de épocas con Sortino > 0
  - Fallos  F_i = número de épocas con Sortino ≤ 0
  
  Prior: Beta(1, 1)  // distribución uniforme — no informativa
  
  Posterior: Beta(1 + S_i, 1 + F_i)
  
  En cada época:
    1. Muestrear θ_i ~ Beta(1 + S_i, 1 + F_i) para cada estrategia
    2. Asignar capital proporcional a θ_i:
       allocation_i = (θ_i / Σ θ_j) × total_capital
```

**Ventaja de la Beta:** El Sortino puede ser ruidoso. Pero al reducirlo a éxito/fracaso (Sortino > umbral), modelamos la probabilidad de que la estrategia *realmente* tenga alfa positivo, no la magnitud exacta del alfa.

#### Ciclo de Vida de Estrategias

```
┌─────────┐    rendimiento > umbral    ┌─────────┐
│ ACTIVE  │───────────────────────────▶│ ACTIVE  │
│ (normal)│◀───────────────────────────│ (élite) │
└────┬────┘    rendimiento consistente  └────┬────┘
     │                                       │
     │ 3 épocas consecutivas                 │ 5 épocas consecutivas
     │ con Sortino < 0                       │ con Sortino < 0
     ▼                                       ▼
┌─────────┐                            ┌─────────┐
│ FROZEN  │  2 épocas Sortino > 0      │ FROZEN  │
│         │────────────────────────────▶│         │
│ (0% cap)│                            │ (0% cap)│
└─────────┘                            └─────────┘
     │                                       │
     │ 10 épocas sin recuperar               │
     ▼                                       ▼
┌─────────┐                            ┌─────────┐
│ RETIRED │                            │ RETIRED │
└─────────┘                            └─────────┘
```

**Estrategias nuevas** entran en modo `PROBATION`: reciben 2% del capital durante 4 épocas para acumular historial antes de competir en igualdad de condiciones.

---

### 4.2 Métricas Adaptadas a Mercados Binarios

#### Eliminación del Ratio de Sharpe

**Problema del Sharpe en mercados binarios:**

El Ratio de Sharpe penaliza la volatilidad *total*: `Sharpe = (R_p - R_f) / σ_total`. En trading, la volatilidad al alza **es deseable** — solo queremos penalizar las caídas.

En mercados binarios, esto es aún más problemático porque:
- Los retornos son acotados (pérdida máxima = precio pagado, ganancia máxima = 1 - precio).
- La distribución de retornos es inherentemente bimodal (0% o gran ganancia, o -100%).
- Las estrategias ganadoras tienen alta volatilidad al alza → Sharpe castiga injustamente.

#### Ratio de Sortino (Métrica Oficial de Scout Lab v2.0)

```
Sortino = (R_p - MAR) / σ_downside

donde:
  R_p        = retorno anualizado de la estrategia
  MAR        = Minimum Acceptable Return (0% para estrategias absolutas, o tasa libre de riesgo)
  σ_downside = sqrt( (1/n) × Σ min(0, R_i - MAR)² )
             // solo penaliza desviaciones POR DEBAJO del MAR
```

**Cálculo para una estrategia:**

```python
# Para los N trades cerrados de una estrategia en la época:
returns = [trade.pnl / trade.amount_invested for trade in closed_trades]

MAR = 0.0  # no aceptamos pérdidas

downside_returns = [min(0, r - MAR) for r in returns]
σ_downside = sqrt(sum(r² for r in downside_returns) / len(downside_returns))

R_p = (product(1 + r for r in returns))^(1 / len(returns)) - 1  # media geométrica
R_p_annualized = (1 + R_p)^(365/days_in_epoch) - 1

Sortino = R_p_annualized / σ_downside if σ_downside > 0 else 0
```

**Interpretación para el Bandit:**

| Sortino | Calidad de Estrategia |
|---------|----------------------|
| > 2.0 | Excelente — alfa consistente |
| 1.0 – 2.0 | Buena — rentable con drawdowns controlados |
| 0.5 – 1.0 | Marginal — necesita optimización |
| < 0.5 | Pobre — probablemente sin alfa real |
| < 0 | Destructora de capital — congelar |

#### Criterio de Kelly Fraccional para Position Sizing

**Problema del Kelly Completo:**

El Criterio de Kelly original (`f*`) maximiza el crecimiento geométrico del capital a largo plazo, pero:
- Asume conocimiento perfecto de la probabilidad real (nunca es el caso).
- En colas gruesas (fat tails), el Kelly completo tiene alta probabilidad de ruina.
- Es extremadamente agresivo con estimaciones imperfectas de edge.

**Solución: Kelly Fraccional Dinámico**

```
f_final = f_kelly × k_dynamic × ruin_gate

donde:
  f_kelly = (p_true × b - (1 - p_true)) / b
  k_dynamic = k_base × sortino_scalar × liquidity_scalar × time_scalar × corr_scalar
  ruin_gate = 1.0 si max_loss ≤ RUIN_LIMIT × equity, else escala hacia abajo
```

#### Cálculo de f_kelly para Mercados Binarios

Para una apuesta YES a precio P con probabilidad real estimada `p_true`:

```
Edge = p_true - P   // ventaja sobre el mercado

Si Edge > 0:
  b = (1 - P) / P   // odds: si apuesto $P, gano $(1-P) si acierto
  q = 1 - p_true
  f_kelly = (p_true × b - q) / b
          = p_true - q / b
          = p_true - (1 - p_true) × P / (1 - P)
Si Edge ≤ 0:
  f_kelly = 0  // no apostar
```

**Estimación de p_true (la probabilidad real):**

Esta es LA pregunta fundamental. v2.0 usa un ensemble:

```
p_true = ensemble_weighted_average(
    model_score: 0.35,    // modelo de ML/ensamble de señales (si existe)
    whale_signal: 0.15,   // dirección de ballenas
    momentum_adj: 0.20,   // ajuste por momentum reciente
    base_market: 0.30     // precio actual del mercado (anclaje bayesiano)
)
```

En modo paper trading (sin modelo ML), se usa una heurística simplificada basada en la fuerza de señal.

#### k_dynamic — Modulador de Fracción Kelly

```
k_base = 0.25  // Quarter Kelly como punto de partida conservador

sortino_scalar = clamp(sortino_estrategia / 2.0, 0.25, 1.5)
  // Sortino 2.0 → scalar 1.0 (Kelly completo * 0.25)
  // Sortino 0.5 → scalar 0.25 (Kelly * 0.0625 — casi nada)
  // Sortino 4.0 → scalar 1.5 (Kelly * 0.375 — más agresivo)

liquidity_scalar = clamp(market_liquidity / 50000, 0.1, 1.0)
  // $50K liquidez → scalar 1.0
  // $5K liquidez → scalar 0.1 (difícil salir → posición pequeña)

time_scalar = risk_multiplier(τ)  // mismo que en §2.1 Time-Decay
  // Cerca de expiración → posiciones más pequeñas

corr_scalar = 1.0 - max_correlation × 0.5
  // Si el portfolio ya tiene 0.8 de correlación con esta apuesta:
  // scalar = 1.0 - 0.8 × 0.5 = 0.6
```

#### Restricción de Ruina (Ruin Gate)

```
max_loss_this_trade = position_size × P  // si apostamos YES, pérdida máxima = precio
ruin_limit = MAX_PORTFOLIO_DRAWDOWN × equity
           = 0.02 × equity  // ninguna operación puede perder > 2% del portfolio

si max_loss_this_trade > ruin_limit:
    position_size = ruin_limit / P  // reducir al máximo tolerable
```

#### Pipeline Completo de Position Sizing

```
ENTRADA: Señal de estrategia con Edge estimado
  │
  ▼
[1] Calcular f_kelly con p_true y P_market
  │
  ▼
[2] Recuperar Sortino de la estrategia (del Bandit)
  │
  ▼
[3] Calcular k_dynamic = k_base × ∏ scalares
  │
  ▼
[4] f_fractional = f_kelly × k_dynamic
  │
  ▼
[5] position_size_raw = equity × f_fractional
  │
  ▼
[6] Aplicar Ruin Gate:
    position_size = min(position_size_raw, RUIN_LIMIT × equity / P)
  │
  ▼
[7] Aplicar Correlation Penalty (si tiene posiciones existentes)
  │
  ▼
[8] Clampear a [MIN_POSITION_SIZE, MAX_POSITION_SIZE]
  │
  ▼
SALIDA: Tamaño de posición final en USD
```

---

## 5. Arquitectura de Sistema

### 5.1 Topología de Procesos

La v1.0 es un script monolítico secuencial. La v2.0 se despliega como **múltiples procesos especializados** comunicándose vía Redis Pub/Sub:

```
┌─────────────────────────────────────────────────────────────┐
│                     REDIS (Pub/Sub + Cache)                  │
│                                                             │
│  Canales:                                                   │
│  • radar:update       — snapshots Gamma cada 30s            │
│  • market:enter_top50 — nuevo mercado onboardeado al CLOB   │
│  • market:exit_top50  — mercado removido del CLOB           │
│  • book:delta         — deltas de order book                │
│  • trade:print        — ejecuciones reales                  │
│  • whale:flow         — flujo de ballenas por mercado       │
│  • signal:detected    — señales generadas                   │
│  • strategy:decision  — decisiones de trading               │
│  • risk:allocation    — asignaciones de capital del bandit  │
└─────────────────────────────────────────────────────────────┘
        ▲          ▲          ▲          ▲          ▲
        │          │          │          │          │
┌───────┴──┐ ┌────┴────┐ ┌───┴────┐ ┌───┴─────┐ ┌──┴───────┐
│  RADAR   │ │  CLOB   │ │ WHALE  │ │STRATEGY │ │PORTFOLIO │
│  DAEMON  │ │  DAEMON │ │TRACKER │ │ ENGINE  │ │ MANAGER  │
│          │ │         │ │        │ │         │ │          │
│ • Gamma  │ │ • WS L2 │ │•Polygon│ │ • Market │ │ • Bandit │
│   poll   │ │ • Recon │ │ events │ │   Making │ │ • Kelly  │
│ • Top200 │ │ • Books │ │•Wallet │ │ • Corr   │ │ • Sortino│
│   rank   │ │ • Trades│ │  P&L   │ │   Arb    │ │ • Ruin   │
│          │ │         │ │        │ │ • Spoof  │ │   Gate   │
└──────────┘ └─────────┘ └────────┘ └──────────┘ └──────────┘
                                             │
                                             ▼
                                    ┌────────────────┐
                                    │  RISK MANAGER  │
                                    │                │
                                    │ • Time-Decay   │
                                    │ • Inventory L. │
                                    │ • Markout Tox. │
                                    │ • Kill Switch  │
                                    │ • Max DD       │
                                    └────────────────┘

                           ┌──────────────────────┐
                           │ EXEC & GAS MANAGER   │
                           │                      │
                           │ • Gas Price Oracle   │
                           │ • MEV Protection     │
                           │ • Coste de Ejecución │
                           └──────────────────────┘

                           ┌──────────────────────┐
                           │  WALLET CLUSTERING   │
                           │  (en Whale Tracker)  │
                           │                      │
                           │ • Funding patterns   │
                           │ • Behavioral match   │
                           │ • Cluster scoring    │
                           └──────────────────────┘
```

### 5.2 Stack Tecnológico

| Componente | Tecnología | Justificación |
|------------|-----------|---------------|
| **Async Engine** | Python `asyncio` + `aiohttp` | Manejo nativo de WebSocket y concurrencia sin overhead de threads |
| **Message Bus** | Redis 7+ | Pub/Sub sub-ms, caché para rate-limit tokens, contadores atómicos |
| **Order Book Store** | In-memory NumPy arrays | O(1) acceso a niveles de precio, operaciones vectorizadas para OBI |
| **Time-Series DB** | SQLite (WAL mode) | Snapshots locales. Mismo rol que v1.0 pero con WAL para lecturas concurrentes |
| **Wallet Profiles** | PostgreSQL | Consultas analíticas complejas (rankings, agregaciones, joins) |
| **Config + State** | YAML + Redis Hashes | Configuración estática en YAML, estado dinámico en Redis |
| **Monitoring** | Prometheus + Grafana | Métricas de latencia, gaps, P&L, Sortino por estrategia |

### 5.3 Diagrama de Flujo de Trading (Critical Path)

```
CLOB WS ──delta──▶ BookAnalyzer ──OBI──▶┐
                                         ├──▶ SpoofDetector ──S──▶ Strategy Engine
CLOB WS ──trade─▶ TradeAggregator ─TFI──┘                          │
                                                                    │
Gamma API ──poll──▶ Selection Engine ──top50──▶┐                   │
                                                ├──▶ Signals ──────┤
Whale Tracker ──flow──▶ Conviction Multiplier ─┘                   │
                                                                    ▼
                                                          Strategy Decision
                                                                    │
                                                                    ▼
                                              Portfolio Manager ◄──┘
                                              (Kelly × Sortino × Bandit)
                                                                    │
                                                                    ▼
                                                              Position Size
                                                                    │
                                                                    ▼
                                                              Risk Gate
                                                              (Time-Decay + Ruin)
                                                                    │
                                                                    ▼
                                                           ┌──────────────┐
                                                           │ TRADE ENGINE │
                                                           │ (paper/real) │
                                                           └──────────────┘
```

**Critical Path Latency Budget (objetivo):**

| Etapa | Latencia Target |
|-------|----------------|
| WebSocket delta → Book Update | < 1ms |
| OBI + TFI → Spoof Score | < 3ms |
| Spoof + Signals → Strategy Decision | < 10ms |
| Strategy → Kelly Position Size | < 5ms |
| Position Size → Risk Gate → Trade | < 2ms |
| **Total End-to-End** | **< 25ms** |

---

## 6. Gestión de Cuellos de Botella

### 6.1 Cuello de Botella de Red

| Riesgo | Mitigación |
|--------|-----------|
| Bloqueo API CLOB (> 3-4 calls) | **WebSocket como fuente primaria.** REST solo para snapshots de reconciliación. Token bucket con presupuesto semafórico. |
| Latencia de red Polygon | Usar Alchemy/QuickNode con WebSocket en vez de RPC REST. Batching de eventos cada 2 segundos. |
| Desconexión WebSocket CLOB | Reconexión automática con exponential backoff. Detección de gaps por `seq_num` (no por timeout): si `delta.seq > expected_seq`, el libro se marca RECONCILING **instantáneamente** sin esperar. Snapshot+Rebuild en < 1s. |
| Redis saturado | Usar Redis Streams en vez de Pub/Sub para backpressure. TTL en todas las claves. |

### 6.2 Cuello de Botella de CPU

| Riesgo | Mitigación |
|--------|-----------|
| Cálculo OBI para 50 libros | Vectorizado con NumPy. Los libros son arrays de tamaño fijo (20 niveles × 2 sides). Operaciones O(1). |
| Embeddings NLP (correlation graph) | Modelo `all-MiniLM-L6-v2` (~80MB). Se ejecuta 1 vez al inicio y on-demand cuando nuevos mercados entran. NO en el critical path. |
| Thompson Sampling (muchas estrategias) | El sampling de Beta es O(1) por estrategia. Con 20 estrategias, < 1ms. |
| Backtesting | Proceso offline independiente. No comparte recursos con el live trading. |

### 6.3 Cuello de Botella de Memoria

| Componente | Memoria Estimada | Notas |
|------------|-----------------|-------|
| L2 Books (50 mercados × 20 niveles × 2 sides) | ~50 KB | Estructuras NumPy fijas. Sin garbage collection. |
| Trade Buffers (últimos 1000 trades × 50 mercados) | ~5 MB | Buffer circular. Sobrescritura automática. |
| Wallet Profiles (top 10K wallets) | ~20 MB | Solo perfiles con score en Redis. Full DB en PostgreSQL. |
| Correlation Graph | ~5 MB | Grafo en networkx. Recalculado cada 6h. |
| **Total memoria residente** | **~50-80 MB** | Muy por debajo del límite de cualquier contenedor. |

### 6.4 Modos de Degradación

| Escenario | Respuesta |
|-----------|----------|
| CLOB WS caído > 30s | Modo Gamma-only: precios Gamma, sin order book. Estrategias que requieren L2 (Market Making) se pausan. Estrategias direccionales siguen operando. |
| Polygon RPC caído | Whale tracking pausado. Conviction Multiplier = 1.0 (neutro) para todos los mercados. |
| Redis caído | Degradación a modo standalone: cada proceso usa su propio estado local. Sin comunicación entre módulos. El Portfolio Manager usa la última asignación cached. |
| SQLite bloqueado | WAL mode permite lecturas concurrentes. Si aún así hay contención, failover a archivo JSON en `/tmp`. |

---

## APÉNDICE A: Comparativa v1.0 vs v2.0

| Dimensión | v1.0 (Scout Lab) | v2.0 (Institucional) |
|-----------|-----------------|---------------------|
| **Frecuencia de datos** | Cada 5 min (polling) | Tiempo real (WebSocket) |
| **Fuente de precios** | Gamma API (con CLOB best-effort) | CLOB L2 WebSocket (con Gamma fallback) |
| **Order book** | No disponible | L2 completo + deltas |
| **Anti-desync** | No existe | Snapshot/Delta Reconciliation con state machine (seq_num-based, pausa instantánea) |
| **Estrategias** | 5 funciones deterministas | 8+ con MM, Arb de correlación, FOK simulado, Markout Analysis |
| **Detección de spoofing** | No | OBI vs TFI + cancel rate |
| **Whale tracking** | No | On-chain Polygon + Alpha Whale Score + Wallet Clustering Anti-Sybil |
| **Protección MEV / Gas** | No existe | Execution & Gas Manager: gas dinámico + Flashbots/mempool privada |
| **Gestión de riesgo** | TP/SL fijos (10%/20%) | Time-Decay dinámico + Kelly Fraccional + Ruin Gate + Toxic Flow Protection |
| **Asignación de capital** | 5% fijo para todas | Thompson Sampling MAB + Sortino + Kelly |
| **Métrica de rendimiento** | Win rate + P&L bruto | Sortino Ratio + risk-adjusted return |
| **Arquitectura** | Script secuencial | Multi-proceso asíncrono con Redis |
| **Latencia crítica** | ~90 segundos (ciclo scan) | < 25ms |
| **Mercados monitoreados** | 200 (Gamma polling) | Top 50 (CLOB WS) + 200 (Gamma radar) |

---

## APÉNDICE B: Prioridades de Implementación

Dado el alcance del documento, se recomienda el siguiente orden de implementación:

**Fase 1 — Fundación (Semanas 1-3):**
1. Migración a `asyncio` + `aiohttp`.
2. Radar Layer (Gamma polling async).
3. Selection Engine + Top 50.
4. Rate-Limit Budget Manager.
5. SQLite → WAL mode.

**Fase 2 — Datos en Tiempo Real (Semanas 3-5):**
6. WebSocket Manager con Snapshot/Delta Reconciliation.
7. BookAnalyzer + OBI en tiempo real.
8. TradeAggregator + TFI.
9. SpoofDetector.

**Fase 3 — Estrategias Avanzadas (Semanas 5-8):**
10. Market Making Engine + Quote Width Dinámico.
11. Markout Analysis + Protección contra Flujo Tóxico.
12. Time-Decay Risk Manager.
13. Correlation Graph Builder.
14. Capital Lock-Up Penalty + Arbitraje.
15. Legging Risk: Ejecución Fill-or-Kill (FOK) a nivel de aplicación.

**Fase 4 — Portfolio Intelligence (Semanas 8-10):**
16. Whale Tracker Daemon (Polygon) + Wallet Clustering Anti-Sybil.
17. Conviction Multiplier.
18. Execution & Gas Manager (Polygon gas + MEV protection).
19. Thompson Sampling Bandit.
20. Kelly Fraccional + Ruin Gate + Sortino.

**Fase 5 — Hardening (Semanas 10-12):**
21. Multi-proceso con Redis.
22. Modos de degradación.
23. Dashboard v2 (Streamlit con datos real-time).
24. Paper trading extendido (1-2 meses de validación).
25. Transición a real trading (requiere firma EIP-712 y wallet).

---

*Documento de Arquitectura Lógica — Scout Lab v2.0*  
*Preparado para Manu — 7 de Mayo, 2026*

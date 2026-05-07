[5/7/2026 8:27 PM] Hermes: ---

Scout Lab v2.0 — Dashboard de Comando en Tiempo Real

Arquitectura Visual y Flujo de Usuario

Documento de Diseño UI/UX  
Autor: Arquitecto de Frontend Senior — Plataformas de Trading Institucional HFT  
Fecha: Mayo 2026  
Versión: 1.0-draft  
Stack propuesto: React 19 + TypeScript + D3.js/Canvas + WebSockets (SSE) + Tailwind CSS

---

0. Principios de Diseño

Antes de entrar en las vistas, establezcamos los principios que gobiernan TODAS las decisiones de diseño:

0.1 Filosofía Visual: "Terminal de Comando, No Dashboard de Marketing"

Un dashboard de trading HFT no es una presentación de PowerPoint. Es un instrumento quirúrgico. Cada píxel debe transmitir información accionable. No hay espacio para decoración.

Reglas de oro:
- Dark-first: Tema oscuro (#0B0E14 background). Un trader mira la pantalla 12-16 horas/día. El brillo quema los ojos. Las pantallas oscuras consumen menos energía en OLED.
- Densidad de información: Bloomberg Terminal como referencia. El trader prefiere más datos en una pantalla que tener que navegar entre pestañas.
- Color = Decisión: VERDE = posición larga / salud buena / tendencia alcista. ROJO = posición corta / peligro / tendencia bajista. ÁMBAR (#F59E0B) = precaución / degradación. BLANCO = neutro / informativo. NUNCA usar color decorativo. Si no transmite señal, es gris.
- Tipografía monoespaciada para números: JetBrains Mono o Fira Code. Los traders leen tablas numéricas — la alineación importa. Las fuentes proporcionales destruyen la scanability vertical.
- Motion reservado para alertas: La animación continua cansa y distrae. Solo animar transiciones de alerta (pulso rojo en emergencias, fade-in de nuevas filas).
- Latencia visible: Los números de latencia se muestran en ms con 1 decimal. Son ciudadanos de primera clase, no letra pequeña.

0.2 Jerarquía de Atención (Heat Map de la Mirada)

El ojo del trader sigue este patrón al monitorizar:
1. Alerta roja/ámbar (lo que está ardiendo AHORA)
2. Posiciones abiertas + P&L (mi dinero)
3. Estrategias degradadas (qué ha dejado de funcionar)
4. Sistema health (¿está todo online?)

El layout refleja esta jerarquía: las alertas siempre están en top-strip fijo (sticky header). Las posiciones ocupan el centro visual. El health del sistema está en la barra inferior permanente.

0.3 Filosofía de Actualización: Push para Peligro, Stream para Datos, Poll para Histórico

Alertas de emergencia

• Categoría: Alertas de emergencia

• Método: Push (Toast + sonido + Telegram)

• Justificación: RECONCILING, FROZEN, Toxicidad > 1.5, Kill Switch activado. No pueden esperar ni 1 segundo.

Métricas en tiempo real

• Categoría: Métricas en tiempo real

• Método: Stream (WebSocket/SSE desde Redis Pub/Sub)

• Justificación: Books, trades, OBI, TFI, precios, P&L. Actualización sub-segundo vía suscripción nativa.

Rankings y estadísticas

• Categoría: Rankings y estadísticas

• Método: Stream (Redis, refresco cada época/10s)

• Justificación: Sortino, rankings, asignaciones. Cambian por época (6h) pero se muestran con el último valor cached.

Backtest e histórico

• Categoría: Backtest e histórico

• Método: Poll (on-demand)

• Justificación: Gráficos de rendimiento histórico. Solo cargar cuando el usuario expande la sección.

---

1. Layout Global
 (1/16)
[5/7/2026 8:27 PM] Hermes: ┌──────────────────────────────────────────────────────────────────────────┐
│ TOOLBAR SUPERIOR (sticky)                                                 │
│ [Scout Lab v2.0]  MODE: ● LIVE PAPER  |  UPTIME: 38h 12m  |  [KILL ▼]  │
├──────────┬──────────┬──────────┬──────────┬──────────┬───────────────────┤
│ ALERTAS  │ SISTEMA  │PORTFOLIO │ ORÁCULOS │ RIESGO   │ [⚙ Config]       │
│ [2] ⬤    │   ●      │          │          │          │                   │
├──────────┴──────────┴──────────┴──────────┴──────────┴───────────────────┤
│                                                                           │
│  ┌─── TOP STRIP: ALERTAS ACTIVAS ────────────────────────────────────┐   │
│  │ 🔴 [CRIT] RECONCILING: "Trump wins 2028" seq gap=3   ⏱ 2s ago    │   │
│  │ 🟡 [WARN] FROZEN: momentum_follow (Sortino -1.2)      ⏱ 5m ago    │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  ┌─── CONTENIDO PRINCIPAL (tab activa) ──────────────────────────────┐   │
│  │                                                                     │   │
│  │  [Aquí va el panel seleccionado en la navegación superior]          │   │
│  │                                                                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                           │
├──────────────────────────────────────────────────────────────────────────┤
│ BARRA DE ESTADO INFERIOR (sticky)                                          │
│ 📡 WS: 50/50 CLEAN | ⚡ RL: recon 70% avail | 🧠 MAB epoch 3/6h | 🐋 12α │
└──────────────────────────────────────────────────────────────────────────┘


Notas de navegación:
- Las 5 pestañas superiores son la navegación primaria. El badge numérico en rojo (⬤) indica alertas activas en esa pestaña.
- El Kill Switch en la toolbar es un botón físico. Un clic = pausa total de trading en todos los mercados. Confirmación requerida. Se muestra en rojo pulsante cuando está activo.
- La barra de estado inferior es omnipresente — visible en todas las pestañas. Contiene los 4 indicadores más vitales del sistema destilados en una línea.
- El modo (LIVE PAPER / BACKTEST / DRY RUN) se muestra en la toolbar con un indicador de color: verde = live paper, azul = backtest, gris = dry run.

---

2. Panel 1 — Sistema y Salud (System Health)

2.1 Propósito

Monitorizar la integridad operativa del sistema. Este panel responde a la pregunta: "¿Puedo confiar en los datos que estoy viendo?" Es el panel que el trader mira al iniciar la sesión — si hay algo en rojo aquí, todo lo demás es sospechoso.

2.2 Mapa de Layout
 (2/16)
[5/7/2026 8:27 PM] Hermes: ┌──────────────────────────────────────────────────────────────────────────┐
│ SYSTEM HEALTH                                                             │
├──────────────────────────────┬───────────────────────────────────────────┤
│                              │                                           │
│  H E A R T B E A T S        │  R A T E   L I M I T   B U D G E T       │
│                              │                                           │
│  ┌─────────────────────┐    │  ┌───────────────────────────────────┐    │
│  │ ● CLOB WebSocket    │    │  │ ████████████░░░░░░  70% Reconcil  │    │
│  │   50/50 subscribed  │    │  │ ████░░░░░░░░░░░░░░  20% Onboard   │    │
│  │   ⚡ 12ms avg RTT  │    │  │ ██░░░░░░░░░░░░░░░░  10% Ad-hoc    │    │
│  │                     │    │  │                                   │    │
│  │ ● Gamma API         │    │  │ Tokens disponibles: 3.2 / 4.0     │    │
│  │   ⚡ 258ms scan     │    │  │ Last acquired: 0.2s ago            │    │
│  │                     │    │  │ ⚠ Burst usado: 1.8/2.0             │    │
│  │ ● Polygon RPC       │    │  └───────────────────────────────────┘    │
│  │   ⚡ 1.4s block lag │    │                                           │
│  │                     │    │  R A T E   H I S T O R Y  (5m)           │
│  │ ● Redis Bus         │    │  ┌───────────────────────────────────┐    │
│  │   ⚡ <1ms pub/sub   │    │  │  ▁▁▂▁▃▁▂▁▁▁▂▁▃▂▁▁▁▂▁▁▁▁▁▁▁▁▁   │    │
│  │                     │    │  │  (area chart: tokens consumed/min) │    │
│  └─────────────────────┘    │  └───────────────────────────────────┘    │
│                              │                                           │
├──────────────────────────────┴───────────────────────────────────────────┤
│                                                                           │
│  R E C O N C I L I A T I O N   M A T R I X  (Top 50)                     │
│                                                                           │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │ CLEAN ████████████████████████████████████████████░░░░░░  47/50  │    │
│  │ RECON ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   3/50  │    │
│  │                                                                   │    │
│  │  MERCADOS RECONCILING:                                           │    │
│  │  ┌──────────┬──────────┬──────────┬──────────┬──────────┐       │    │
│  │  │ Market   │ Seq Gap  │ In State │ Deltas   │ Resync   │       │    │
│  │  │          │          │ since    │ buffered │  ETA     │       │    │
│  │  ├──────────┼──────────┼──────────┼──────────┼──────────┤       │    │
│  │  │ Trump…   │   3      │  4.2s    │    12    │  < 1s    │       │    │
│  │  │ BTC $…   │   7      │ 15.8s    │    45    │   3s     │       │    │
│  │  │ Fed…     │   1      │  0.8s    │     3    │  < 1s    │       │    │
│  │  └──────────┴──────────┴──────────┴──────────┴──────────┘       │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                           │
│  ┌──────────────────────────────┐  ┌──────────────────────────────────┐  │
│  │ DEGRADATION MODE            │  │ SNAPSHOT AGE (por mercado)       │  │
│  │ ● FULL — All systems go     │  │ ▓▓▓▓▓▓▓▓▓▓░░░░░ 85% < 5s        │  │
│  │                              │  │ ▓▓▓▓▓░░░░░░░░░ 10% 5-30s        │  │
│  │ Active strategies: 7/7      │  │ ▓░░░░░░░░░░░░░  5% > 30s ⚠       │  │
│  │ Allowed: MM, ARB, CORR, MOM │  │                                   │  │
│  │ Price source: CLOB L2       │  │ Last Global Snapshot: 2.1s ago    │  │
│  └──────────────────────────────┘  └──────────────────────────────────┘  │
│                                                                           │
├──────────────────────────────────────────────────────────────────────────┤
│  L A T E N C Y   B U D G E T   (Critical Path)                           │
│  ┌──────────────────────────────────────────────────────────────────────┐│
 (3/16)
[5/7/2026 8:27 PM] Hermes: ```
│  │                                                                       ││
│  │  WS→Book │ OBI+TFI→Spoof │ Signal→Decision │ Kelly→Position │ Risk→Trade│
│  │   ▓ 1ms  │    ▓▓ 3ms     │   ▓▓▓ 8ms       │  ▓ 2ms         │  ▓ 1ms   ││
│  │          │               │                 │                │          ││
│  │  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   ││
│  │                           TOTAL: 15ms / 25ms budget                   ││
│  └──────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘
```

2.3 Desglose de Widgets

2.3.1 Heartbeats (Panel superior izquierdo)
Visualización: Tarjetas de estado con icono, latencia, y contador.

**CLOB WebSocket*

• Métrica: CLOB WebSocket

• Fuente: `websocket_manager.get_health_metrics()`

• Actualización: Stream (cada heartbeat)

• Visualización: ● verde/ámbar/rojo + ms RTT + mercados suscritos

*Gamma API*

• Métrica: Gamma API

• Fuente: `async_scanner.radar_scan()` timing

• Actualización: Cada poll (30-60s)

• Visualización: ● + ms último scan + mercados encontrados

*Polygon RPC*

• Métrica: Polygon RPC

• Fuente: `degradation.check_health('polygon')`

• Actualización: Cada 10s

• Visualización: ● + block lag en segundos

*Redis Bus*

• Métrica: Redis Bus

• Fuente: `redis_bus` ping/latency

• Actualización: Cada 1s

• Visualización: ● + latencia pub/sub en ms

Indicador de color:
- 🟢 Verde: operativo, dentro de parámetros
- 🟡 Ámbar: degradado (latencia > 2x normal, reconexión en curso)
- 🔴 Rojo: caído o inalcanzable

Gráfico adicional: Mini-sparkline de 60s debajo de cada heartbeat mostrando la latencia histórica (línea blanca tenue sobre fondo oscuro). Permite detectar degradación gradual antes de que cruce el umbral.

2.3.2 Rate-Limit Budget Manager (Panel superior derecho)
Visualización: Barras de progreso horizontales tipo "fuel gauge" + sparkline histórica.

Reconciliation (70%)

• Bucket: Reconciliation (70%)

• % Budget: `rate_limiter.available('reconciliation')`

• Visualización: Barra azul oscuro

Onboarding (20%)

• Bucket: Onboarding (20%)

• % Budget: `rate_limiter.available('onboarding')`

• Visualización: Barra verde

Ad-hoc (10%)

• Bucket: Ad-hoc (10%)

• % Budget: `rate_limiter.available('ad_hoc')`

• Visualización: Barra gris

Debajo: Un área minichart de 5 minutos mostrando el consumo de tokens por minuto (eje Y: tokens/min, eje X: tiempo). Las líneas se colorean cuando se acercan al límite.

Alerta push cuando: Cualquier bucket cae por debajo del 10% disponible. El mensaje sugiere acción: "Reconciliation bucket al 8%. Posible loop de resync — revisar WebSocket health."

2.3.3 Reconciliation Matrix (Panel central)
Visualización: Barra de estado horizontal + tabla de mercados en RECONCILING.

La barra superior muestra el ratio CLEAN/RECONCILING como una barra de progreso:
- Verde = CLEAN (trading habilitado)
- Ámbar pulsante = RECONCILING (trading pausado)
- El texto muestra "47/50 CLEAN"

Tabla de mercados en RECONCILING:* Solo visible si hay > 0 mercados en este estado. Columnas:
- `Market` (nombre truncado a 30 chars)
- `Seq Gap` (diferencia entre seq esperado y recibido — indica cuántos mensajes se perdieron)
- `In State` (tiempo que lleva en RECONCILING — si > 30s, la fila se vuelve ámbar; si > 120s, roja)
- `Deltas Buffered` (cuántos deltas acumulados en el buffer circular)
- `Resync (4/16)
[5/7/2026 8:27 PM] Hermes: ETA` (estimación basada en latencia REST actual)

Gráfico adicional: Un calendar-heatmap de los últimos 30 minutos mostrando cada mercado como una celda que alterna entre verde (CLEAN) y ámbar (RECONCILING). Permite identificar patrones: ¿un mercado específico está siempre en RECONCILING? ¿Hay ráfagas de RECONCILING cada X minutos?

Alerta push cuando: Cualquier mercado entra en RECONCILING (crítico — el trading se pausa instantáneamente en ese mercado). También cuando un mercado lleva > 60s en RECONCILING (posible problema de connectivity).

2.3.4 Degradation Mode (Panel inferior izquierdo)
Visualización: Un indicador circular grande con el modo actual.

FULL

• Modo: FULL

• Color: Verde brillante

• Significado: Todos los sistemas operativos

MINIMAL

• Modo: MINIMAL

• Color: Ámbar

• Significado: Algún subsistema caído (ej: solo Gamma, sin CLOB)

STANDALONE

• Modo: STANDALONE

• Color: Rojo

• Significado: Sin Redis, cada módulo aislado

EMERGENCY

• Modo: EMERGENCY

• Color: Rojo pulsante

• Significado: Liquidación forzosa activa

Debajo del indicador, lista de estrategias permitidas en el modo actual:
- ✓ Market Making
- ✓ Correlation Arb  
- ✗ Momentum (requiere CLOB L2 → deshabilitado en modo Gamma-only)

Gráfico de transiciones: Un timeline horizontal de las últimas 24h mostrando cambios de modo como segmentos coloreados. Permite ver si el sistema es inestable (muchas transiciones).

2.3.5 Snapshot Age Distribution (Panel inferior derecho)
Visualización: Histograma de barras horizontales apiladas.

Muestra la distribución de edad de los snapshots de order book:
- < 5 segundos (verde oscuro)
- 5-30 segundos (verde claro)
- 30-120 segundos (ámbar)
- > 120 segundos (rojo)

Debajo: "Last Global Snapshot: 2.1s ago" con un contador que avanza en tiempo real.

Alerta push cuando: Más del 20% de los snapshots tienen > 30s de antigüedad.

2.3.6 Latency Budget Gauge (Panel inferior, ancho completo)
Visualización: Un "waterfall" horizontal que muestra cada etapa del critical path con su latencia real vs el budget asignado.

Cada etapa es una barra:
```
WS→Book Update  ████████████░░░░░░░  1.2ms / 5ms budget
OBI+TFI→Spoof   ██████░░░░░░░░░░░░░  2.8ms / 5ms budget  
Signal→Decision ██████████████░░░░░░  8.1ms / 10ms budget
Kelly→Position  ████░░░░░░░░░░░░░░░  1.9ms / 5ms budget
Risk→Trade      ██░░░░░░░░░░░░░░░░░  0.8ms / 2ms budget
────────────────────────────────────────
TOTAL           █████████████████░░░  14.8ms / 25ms budget (59%)
```

Las barras se colorean según el % de budget consumido: verde < 50%, ámbar 50-80%, rojo > 80%.

---

3. Panel 2 — Arena del Portfolio Manager

3.1 Propósito

Monitorizar el torneo de estrategias, entender qué estrategias están ganando dinero (y por qué), y ver cómo se asigna el capital dinámicamente. Responde a la pregunta: "¿Mi capital está donde debe estar?"

3.2 Mapa de Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ PORTFOLIO ARENA                                          Epoch 3/6h █░░  │
├──────────────────────────────┬───────────────────────────────────────────┤
│                              │                                           │
│  T O R N E O  R A N K I N G │  C A P I T A L   A L L O C A T I O N     │
│  (por Sortino Ratio)        │                                           │
│                              │  ┌───────────────────────────────────┐    │
│  ┌───┬──────────┬────┬─────┐│  │ ████████████ Market Making  34%   │    │
│  │ # │ Strategy │Sort│State││  │ ████████░░░░ Corr Arb      22%   │    │
│  ├───┼──────────┼────┼─────┤│  │ ██████░░░░░░ Whale Follow  17%   │    │
│  │ 1 │ corr_arb │3.21│ ●   ││  │ ████░░░░░░░░ Momentum      11%   │    │
│  │ 2 │ whale_f. │2.45│ ●   ││  │ ███░░░░░░░░░ Consensus Brk  8%  │    │
│  │ 3 │ mmaking  │1.87│ ●   ││  │ ██░░░░░░░░░░ Contrarian     5%   │    │
│  │ 4 │ momentum │0.92│ ●   ││  │ █░░░░░░░░░░░ Vol Breakout   3%   │    │
``` (5/16)
[5/7/2026 8:27 PM] Hermes: │  │ 5 │ consens. │0.45│ ◐   ││  │ ░░░░░░░░░░░░ New Market     0%   │    │
│  │ 6 │ contrar. │-0.2│ ⊘   ││  │░░░░░░░░░░░░░ [FROZEN]            │    │
│  │ 7 │ vol_brk  │-0.8│ ⊗   ││  └───────────────────────────────────┘    │
│  └───┴──────────┴────┴─────┘│                                           │
│                              │  ┌───────────────────────────────────┐    │
│  S O R T I N O   H I S T O  │  │ ● Active Capital      $2,847      │    │
│  (sparklines por estrategia)│  │ ○ Frozen Capital      $1,203      │    │
│                              │  │ ⊗ Retired Capital        $0      │    │
│  ┌──────────────────────┐   │  │                                   │    │
│  │ corr_arb  ▁▂▄▆██▆▄▃ │   │  │ Total Equity:        $4,050      │    │
│  │ whale_f   ▂▃▅▆██▇▅▄ │   │  │ P&L (24h):      +$127 (+3.2%)    │    │
│  │ mmaking   ▁▂▃▄▅▆▇█▇ │   │  │ Max Drawdown:   -$89 (-2.2%)     │    │
│  │ momentum  ▁▂▄▅▆▅▆▄▂ │   │  └───────────────────────────────────┘    │
│  │ consens.  █▇▆▄▃▂▁▁▁ │   │                                           │
│  │ contrar.  ███▇▆▅▄▃▂ │   │                                           │
│  │ vol_brk   ██▇▅▃▂▁▁▁ │   │                                           │
│  └──────────────────────┘   │                                           │
│                              │                                           │
├──────────────────────────────┴───────────────────────────────────────────┤
│                                                                           │
│  T H O M P S O N   S A M P L I N G   D I S T R I B U T I O N S           │
│  (distribuciones Beta posteriores por estrategia)                         │
│                                                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐│
│  │                                                                       ││
│  │  corr_arb     ░░░░░░░░░░░░████████████████████████░░░░░░░░  θ~0.78   ││
│  │  whale_f      ░░░░░░░░░░░░░░░███████████████████░░░░░░░░░  θ~0.71   ││
│  │  mmaking      ░░░░░░░░░░░░░░░░░░██████████████░░░░░░░░░░  θ~0.62   ││
│  │  momentum     ░░░░░░░░░░░░░░░░░░░░░░░████████░░░░░░░░░░░  θ~0.48   ││
│  │  consens.     ░░░░░░░░░░░░░░░░░░░░░░░░░██████░░░░░░░░░░░  θ~0.35   ││
│  │  contrar.     ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░███░░░░░░░░░  θ~0.18   ││
│  │  vol_brk      ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░██░░░░░░░░  θ~0.09   ││
│  │                                                                       ││
│  │  ▲ Las distribuciones más anchas = más incertidumbre = más           ││
│  │    exploración automática. Las estrechas = alta certeza.              ││
│  └──────────────────────────────────────────────────────────────────────┘│
│                                                                           │
├──────────────────────────────────────────────────────────────────────────┤
│  P O S I T I O N   S I Z I N G   P I P E L I N E  (última señal)         │
│                                                                           │
│  Signal: momentum_follow → YES "Trump wins 2028?" @ $0.62                 │
│  f_kelly=0.18 → k_dynamic=0.14 → f_fractional=0.025 → $101.25            │
│  └─ Ruin Gate: ✓ ($101 < $81 max loss)                                   │
│  └─ Correlation Penalty: -15% ($86.06 final)                             │
│  └─ Clamp: ✓ ($86 en rango [$10, $500])                                  │
│  ▶ EXECUTED: $86.06 YES @ $0.62                                          │
│                                                                           │
└──────────────────────────────────────────────────────────────────────────┘


3.3 Desglose de Widgets

3.3.1 Torneo de Estrategias Ranking (Panel superior izquierdo)
Visualización: Tabla ordenable con mini-sparklines.

Columnas:
- # — Posición en el ranking
- Strategy — Nombre de la estrategia con icono de tipo
- (6/16)
[5/7/2026 8:27 PM] Hermes: Sortino — Sortino Ratio actual (número grande, con 2 decimales, coloreado: verde > 2.0, blanco 1-2, ámbar 0-1, rojo < 0)
- △ Change — Flecha de cambio respecto a la época anterior (▲ subió, ▼ bajó, — igual)
- State — Indicador de estado: ● ACTIVE, ◐ PROBATION, ⊘ FROZEN, ⊗ RETIRED
- Alloc % — % del capital asignado
- Trades — Número de trades cerrados en esta época
- Win Rate — % de trades ganadores
- Sharpe — Ratio de Sharpe (referencia informativa, no usado para decisión)

Interacción: Click en una fila → expande detalles de esa estrategia (gráfico de equity curve, lista de últimos trades, parámetros del Kelly dinámico).

Ordenación por defecto: Sortino descendente.

Fondo de fila: Las filas FROZEN tienen fondo gris oscuro. Las RETIRED tienen fondo aún más oscuro con texto tachado. Las PROBATION tienen un sutil borde azul.

3.3.2 Sparklines de Sortino por Época (Panel central izquierdo)
Visualización: 7 mini-gráficos de líneas (uno por estrategia), cada uno mostrando la evolución del Sortino en las últimas 10 épocas (60 horas).

Eje X: épocas (1-10). Eje Y: Sortino. Sin etiquetas — solo la forma de la línea importa.

Líneas:
- Tendencia alcista → verde
- Tendencia bajista → rojo
- Estable → blanco
- Una línea de referencia horizontal en Sortino = 0 (para ver quién está consistentemente bajo agua)

3.3.3 Capital Allocation (Panel superior derecho)
Visualización: Gráfico de barras horizontales apiladas.

Muestra la asignación de capital actual:
- Barra verde = capital ACTIVO asignado a estrategias
- Barra gris = capital CONGELADO (estrategias FROZEN cuyo capital está inmovilizado en posiciones abiertas que deben cerrarse)
- Barra roja oscura = capital RETIRADO

Métrica de equity: Panel numérico con 4 cifras grandes:
- Total Equity (blanco, formato: $4,050)
- P&L 24h (verde si positivo, rojo si negativo, con %)
- Max Drawdown (rojo, con % desde el pico)
- Capital Efficiency (% del capital total que está desplegado en posiciones activas — verde si > 70%, ámbar si 40-70%, rojo si < 40%)

Mini equity curve: Un sparkline de 24h del equity total en la esquina superior derecha del panel.

3.3.4 Thompson Sampling Distributions (Panel central)
Visualización: Distribuciones de densidad Beta para cada estrategia.

Este es el widget más innovador del dashboard. Muestra las distribuciones posteriores Beta(α, β) de cada estrategia, que representan la creencia del sistema sobre la probabilidad de que cada estrategia tenga Sortino positivo.

Cada distribución se dibuja como una curva de densidad horizontal:
- El eje X es θ (probabilidad de Sortino > 0)
- La altura de la curva representa la densidad de probabilidad
- Una línea vertical marca la media de la distribución
- El ancho de la distribución indica INCERTIDUMBRE (estrategias nuevas o inconsistentes tienen distribuciones más anchas → más exploración)

Codificación de color:
- Verde = distribución concentrada en la derecha (θ alto, estrategia buena)
- Blanco = distribución centrada
- Rojo = distribución concentrada en la izquierda (θ bajo, estrategia mala)

Valor pedagógico: Este gráfico explica VISUALMENTE por qué el Bandit explora ciertas estrategias más que otras — la incertidumbre es visible como amplitud de la curva.

3.3.5 Position Sizing Pipeline (Panel inferior)
Visualización: Diagrama de flujo lineal que muestra el cálculo del último position size.

Cada paso del pipeline de Kelly Fraccional se muestra como un bloque:
[p_true=0.68] → [f_kelly=0.18] → [k_dynamic=0.14] → [f_frac=0.025] → [$101.25] → [Ruin:✓] → [Corr:-15%] → [$86.06] → EXECUTED ✓


Cada bloque muestra la entrada y salida del cálculo. Si algún gate bloquea la operación (Ruin Gate, Correlation Penalty excesivo), el bloque se muestra en rojo con el motivo. Si la operación se ejecuta, el último bloque es verde.

Este es un debug visual — permite al trader entender exactamente por qué se tomó (o no) una posición.

---
 (7/16)
[5/7/2026 8:27 PM] Hermes: 4. Panel 3 — Radar de Oráculos y Anti-Manipulación

4.1 Propósito

Detectar manipulación de mercado y seguir el dinero inteligente. Responde a las preguntas: "¿Alguien está manipulando el libro de órdenes? ¿Qué están haciendo las ballenas?"

4.2 Mapa de Layout

┌──────────────────────────────────────────────────────────────────────────┐
│ ORACLE RADAR                                                              │
├──────────────────────────────┬───────────────────────────────────────────┤
│                              │                                           │
│  S P O O F I N G   M A P    │  S P O O F   D E T A I L                 │
│  (Heatmap: Mercados × Hora) │                                           │
│                              │  ┌───────────────────────────────────┐    │
│  ┌───────────────────────┐  │  │ MARKET: "Trump wins 2028?"         │    │
│  │mkts↓  -30m  -20 -10 0│  │  │                                     │    │
│  │Trump   ░░░░░░▓▓████  │  │  │ OBI: ████████████░░░░░░░ +0.62     │    │
│  │BTC     ░░░░░░░░░░▓▓  │  │  │ TFI: ██████░░░░░░░░░░░░░ +0.15     │    │
│  │Fed     ░░░░░░░░░░░░  │  │  │ DIV: |||||||||||||||||||| 0.47      │    │
│  │Crypto  ░░▓▓███░░░░░  │  │  │                                     │    │
│  │Sports  ░░░░░░░░░░░░  │  │  │ Spoof Score: 0.62 ◉ PROBABLE        │    │
│  │... x50                │  │  │ Cancel Rate: 3.2x avg ⚠              │    │
│  └───────────────────────┘  │  │                                     │    │
│                              │  │ Action: Ignorar OBI. Size × 0.50   │    │
│                              │  │ Authoritative Dir: TFI → ▲ BUY     │    │
│                              │  └───────────────────────────────────┘    │
│                              │                                           │
│  OBI vs TFI   G A U G E     │  S P O O F   H I S T O R Y  (2h)         │
│  (mercado seleccionado)     │                                           │
│  ┌───────────────────────┐  │  ┌───────────────────────────────────┐    │
│  │   OBI = -0.62         │  │  │ 0.8┤                    ╭─╮       │    │
│  │   ◀══════════●───────▶│  │  │ 0.6┤    ╭╮  ╭─╮    ╭──╯ ╰╮      │    │
│  │         -1  0  +1     │  │  │ 0.4┤  ╭─╯╰──╯ ╰────╯     ╰──╮  │    │
│  │                        │  │  │ 0.2┤──╯                      ╰──│    │
│  │   TFI = -0.15          │  │  │  0 ┤───────────────────────────│    │
│  │   ◀════●══════════▶   │  │  │     └───────────────────────────┘    │
│  │         -1  0  +1     │  │  │      -120m              now           │
│  │                        │  │  │                                     │
│  │   DIVERGENCE = 0.47    │  │  │  Stops: 0.3 ── 0.5 ── 0.7          │
│  │   ⚠ PROBABLE SPOOFING │  │  └───────────────────────────────────┘    │
│  └───────────────────────┘  │                                           │
│                              │                                           │
├──────────────────────────────┴───────────────────────────────────────────┤
│                                                                           │
│  W H A L E   T R A C K E R                                                │
│                                                                           │
│  ┌─────────────────────────────────┐  ┌────────────────────────────────┐ │
│  │ ALPHA WHALES ONLINE       12 α │  │ WHALE FLOW BY MARKET (1h)      │ │
│  │                                 │  │                                │ │
│  │ ┌──────┬──────┬────┬────┬────┐ │  │ Trump ████████████████░░ +$12K │ │
│  │ │Wallet│Score │P&L │WR% │Tr/d│ │  │ BTC   ██████░░░░░░░░░░░░ -$8K  │ │
│  │ ├──────┼──────┼────┼────┼────┤ │  │ Fed   ██████████░░░░░░░░ +$4K  │ │
│  │ │0xA1B│ 0.94 │+$45│ 68%│ 7.2│ │  │Crypto ████░░░░░░░░░░░░░░ +$2K  │ │
│  │ │0xC3D│ 0.91 │+$32│ 72%│ 5.8│ │  │Sports ░░░░░░░░░░░░░░░░░░  $0   │ │
│  │ │0xE5F│ 0.89 │+$28│ 65%│ 4.1│ │  │                                │ │
│  │ │...  │      │    │    │    │ │  │ CM (avg): 1.18 ▲ bullish        │ │
 (8/16)
[5/7/2026 8:27 PM] Hermes: │  │ └──────┴──────┴────┴────┴────┘ │  └────────────────────────────────┘ │
│  └─────────────────────────────────┘                                     │
│                                                                           │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │ W A L L E T   C L U S T E R I N G   (Anti-Sybil)                  │   │
│  │                                                                     │   │
│  │ ┌─────────┬──────┬──────┬──────┬──────┬──────┬──────────────────┐ │   │
│  │ │ Cluster │Wallets│Total │P&L   │Score │Cohes │ Top Holdings     │ │   │
│  │ ├─────────┼──────┼──────┼──────┼──────┼──────┼──────────────────┤ │   │
│  │ │ 🐋 C1  │  8   │$200K │+$42K │ 0.92 │ 1.10 │ YES:Trump(45%)   │ │   │
│  │ │ 🐋 C12 │  4   │ $95K │+$18K │ 0.88 │ 1.00 │ NO:Fed(60%)      │ │   │
│  │ │ 🐟 C7  │  3   │ $15K │ +$2K │ 0.62 │ 0.85 │ YES:BTC(30%)     │ │   │
│  │ └─────────┴──────┴──────┴──────┴──────┴──────┴──────────────────┘ │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                           │
└──────────────────────────────────────────────────────────────────────────┘


4.3 Desglose de Widgets

4.3.1 Spoofing Heatmap (Panel superior izquierdo)
Visualización: Heatmap matricial — Mercados (filas) × Ventanas de tiempo (columnas).

Cada celda representa el Spoofing Score (S) de un mercado en una ventana de 10 minutos. La intensidad del color indica el score:
- Blanco/transparente = S < 0.3 (normal)
- Amarillo = 0.3 ≤ S < 0.5 (sospechoso)
- Naranja = 0.5 ≤ S < 0.7 (probable)
- Rojo oscuro = S ≥ 0.7 (confirmado)

Las filas se ordenan por S máximo en la última hora (los mercados más manipulados arriba). Un borde pulsante rodea las celdas donde S ≥ 0.7 (spoofing confirmado).

Interacción: Hover sobre una celda → tooltip con OBI, TFI, D_raw, cancel_rate_factor, y S exactos. Click → selecciona ese mercado para verlo en detalle en el panel derecho.

Valor operativo: Un trader experimentado puede detectar patrones: ¿un mercado tiene spoofing recurrente cada hora en punto? ¿Coincide con horarios de baja liquidez?

4.3.2 OBI vs TFI Gauge (Panel central superior)
Visualización: Dos gauges horizontales enfrentados + un número de divergencia grande.

El gauge de OBI y TFI usan un diseño de "termómetro bipolar":
OBI  ◀══════════════●─────────▶  +0.62
     -1              0        +1
 (9/16)
[5/7/2026 8:27 PM] Hermes: La flecha (●) marca el valor actual. El fondo se colorea en la dirección del imbalance (verde = presión compradora, rojo = presión vendedora). La intensidad del color escala con la magnitud.

El número de DIVERGENCE es grande y central — es el foco visual del panel. Se colorea según los umbrales: blanco < 0.3, amarillo 0.3-0.5, naranja 0.5-0.7, rojo > 0.7.

Pulsación: Si S ≥ 0.7, el número de divergencia pulsa (opacidad alterna 100% ↔ 60% cada 1s) para llamar la atención del trader.

4.3.3 Spoof Detail (Panel superior derecho)
Visualización: Panel de texto estructurado con métricas y acciones recomendadas.

- Market name (grande, arriba)
- OBI y TFI como barras de progreso horizontales (verde/rojo)
- Divergencia como número grande
- Spoof Score con clasificación textual (NORMAL / SOSPECHOSO / PROBABLE / CONFIRMADO)
- Cancel Rate anómalo (si > 2x la media, se muestra con ⚠)
- Acción recomendada por el sistema: "Ignorar OBI. Usar solo TFI para señales. Size × 0.50." en un recuadro con fondo coloreado.
- Dirección autoritativa: una flecha grande ▲ BUY o ▼ SELL basada en TFI (la señal "real").

4.3.4 Spoof History (Panel inferior derecho)
Visualización: Gráfico de líneas temporal mostrando la evolución del Spoof Score en las últimas 2 horas.

- Línea principal: S(t) en los últimos 120 minutos
- Bandas de referencia horizontales en S = 0.3, 0.5, 0.7
- El área bajo la curva se colorea según la zona (verde < 0.3, amarillo 0.3-0.5, naranja 0.5-0.7, rojo > 0.7)
- Marcadores de eventos: cuando el sistema tomó acciones (redujo size, pausó trading) se marcan con ▾

4.3.5 Whale Tracker (Panel inferior)
Dividido en tres secciones:

Alpha Whales Table (izquierda):
Tabla de las top 12 ballenas por Alpha Score. Columnas:
- Wallet (0xA1B... truncado)
- Score (Alpha Whale Score, 0-1, barra de progreso)
- Total P&L (USD, coloreado)
- Win Rate (%)
- Trades/Week (frecuencia de actividad)
- Last Active (tiempo desde última transacción)
- Fav Market (mercado donde más opera)

Click en una wallet → expande su perfil completo con equity curve, distribución de mercados, y lista de últimos 50 trades.

Whale Flow by Market (centro-derecha):
Barras horizontales mostrando el net whale flow (compras - ventas) por mercado en la última hora.
- Verde = flujo neto comprador (whales bullish)
- Rojo = flujo neto vendedor (whales bearish)
- Longitud de barra = magnitud del flujo en USD

Debajo: Conviction Multiplier promedio entre todos los mercados con un valor grande (ej: 1.18) y una flecha direccional.

Wallet Clustering Table (abajo, ancho completo):
Muestra los clusters detectados por el motor anti-Sybil:
- Cluster ID (C1, C7, C12... con icono 🐋 si Alpha, 🐟 si no)
- Wallets (número de wallets en el cluster)
- Total Volume (volumen agregado REAL del operador)
- Total P&L (P&L del cluster completo — la cifra que importa)
- Cluster Score (Alpha Score a nivel de cluster, con barra de progreso)
- Cohesion (factor de cohesión: 0.70-1.10 — indica certeza del clustering)
- Top Holdings (mercados principales con %)

Alerta push cuando:
- Spoofing Score ≥ 0.7 en cualquier mercado del Top 50 (MANIPULACIÓN CONFIRMADA)
- Una Alpha Whale conocida entra en un mercado del Top 50 con volumen > $10K
- Un nuevo cluster es detectado con ClusterAlphaScore > 0.85
- Conviction Multiplier cruza 1.30 o cae por debajo de 0.70 en algún mercado

---

5. Panel 4 — Ejecución y Riesgo

5.1 Propósito

Monitorizar posiciones abiertas, riesgo de time-decay, toxicidad de flujo, y costes de ejecución. Responde a las preguntas: "¿Cuánto dinero tengo en riesgo? ¿Qué posiciones caducan pronto? ¿Me están haciendo toxic flow?"

5.2 Mapa de Layout

┌──────────────────────────────────────────────────────────────────────────┐
│ EXECUTION & RISK MONITOR                                                  │
 (10/16)
[5/7/2026 8:27 PM] Hermes: ├──────────────────────────┬───────────────────────────────────────────────┤
│                          │                                               │
│  P O S I T I O N S      │  P O S I T I O N   D E T A I L               │
│  (Open Positions Table) │                                               │
│                          │  ┌───────────────────────────────────────┐    │
│  ┌──┬──────┬────┬──┬───┐│  │ MARKET: "Trump wins 2028?"             │    │
│  │# │Market│Size│PnL│τ%││  │ Side: YES   Size: $86.06               │    │
│  ├──┼──────┼────┼──┼───┤│  │                                       │    │
│  │1 │Trump │$86 │+$7│62││  │ P&L: +$7.12 (+8.3%)                    │    │
│  │2 │BTC   │$120│-$4│85││  │ Entry: $0.62   Mark: $0.67             │    │
│  │3 │Fed   │$45 │+$2│40││  │                                       │    │
│  │4 │Crypto│$62 │-$1│25││  │ Time-Decay Risk ─────────────────────  │    │
│  │5 │S&P   │$38 │+$5│15││  │ τ = 62%   risk_mult = 0.92            │    │
│  │6 │Oil   │$28 │-$8│91││  │ ██████████████████░░░░░ 62% consumed   │    │
│  │  │      │    │   │  ││  │ LIQUIDATE AT 95% ◄ 33% remaining      │    │
│  └──┴──────┴────┴──┴───┘│  │                                       │    │
│                          │  │ Markout Toxicity ───────────────────  │    │
│  S U M M A R Y           │  │ ┌──────┬───────┬───────┬────────┐    │    │
│  ┌──────────────────┐   │  │ │ τ+1s │ τ+5s  │ τ+10s │ τ+60s  │    │    │
│  │Total P&L: +$1.23 │   │  │ ├──────┼───────┼───────┼────────┤    │    │
│  │Positions:  6     │   │  │ │+0.05 │ -0.30 │ -1.20 │ -3.50  │    │    │
│  │Value:   $379     │   │  │ └──────┴───────┴───────┴────────┘    │    │
│  │Frozen:  $120     │   │  │ Markout Score: 1.20                   │    │
│  │Liq. zone: 2 pos │   │  │ Class: FLUJO TÓXICO ⚠                  │    │
│  └──────────────────┘   │  │ Action: Spread 3x, Size 50%           │    │
│                          │  └───────────────────────────────────────┘    │
│                          │                                               │
│                          │  Gas & MEV ──────────────────────────────    │
│                          │  ┌───────────────────────────────────────┐    │
│                          │  │ Gas Price: 45 GWEI ▼12%  | POL:$0.82 │    │
│                          │  │ Priority Fee: 2.1 GWEI (50th pct)    │    │
│                          │  │ Est. Tx Cost: $0.03                   │    │
│                          │  │ Est. Slippage: $0.42                  │    │
│                          │  │ MEV Risk: LOW (mempool privada)       │    │
│                          │  └───────────────────────────────────────┘    │
│                          │                                               │
├──────────────────────────┴───────────────────────────────────────────────┤
│                                                                           │
│  T I M E - D E C A Y   C A L E N D A R   (próximas 48h)                  │
│                                                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐│
│  │ Mercado              │τ%│████████████████████████░░░░░░░░│ Liquid │  │
│  │──────────────────────│──│                                 │  Date  │  │
│  │ Oil price > $80?     │91│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░│ +5h ⚠ │  │
│  │ BTC > $100K Dec?     │85│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░│ +12h  │  │
│  │ Trump wins 2028?     │62│▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░│ +142d │  │
│  │ Fed cuts rates?      │40│▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░│ +210d │  │
│  │ Crypto bull market?  │25│▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░│ +280d │  │
│  │ S&P 500 ATH Q3?      │15│▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░│ +300d │  │
│  └──────────────────────────────────────────────────────────────────────┘│
│                                                                           │
│  ┌────────────────────────────────────┐  ┌────────────────────────────┐  │
│  │ RISK MULTIPLIER CURVE            │  │ CORRELATION MATRIX          │  │
 (11/16)
[5/7/2026 8:27 PM] Hermes: │  │                                    │  │ (posiciones abiertas)       │  │
│  │  1.0 ┤████████████████████▌       │  │                             │  │
│  │      │                    ▐▌      │  │                             │  │
│  │  0.8 ┤                    ▐▌      │  │                             │  │
│  │      │                     ▐▌     │  │ (heatmap de correlación     │  │
│  │  0.6 ┤                      ▐▌    │  │  entre posiciones abiertas) │  │
│  │      │                       ▐▌   │  │                             │  │
│  │  0.4 ┤                        ▐█▌ │  │                             │  │
│  │      │                          ▐█▌│  │                             │  │
│  │  0.2 ┤                            ▐│  │                             │  │
│  │      │                             ▐│  │                             │  │
│  │  0.05┤                              │  │                             │  │
│  │      └──────────────────────────────│  │                             │  │
│  │       0%         70%       100% τ  │  │                             │  │
│  │       ○ Oil  ○ BTC  ○ Trump        │  │                             │  │
│  └────────────────────────────────────┘  └────────────────────────────┘  │
│                                                                           │
└──────────────────────────────────────────────────────────────────────────┘


5.3 Desglose de Widgets

5.3.1 Open Positions Table (Panel izquierdo)
Visualización: Tabla compacta de posiciones abiertas.

Columnas:
- # — prioridad (ordenado por riesgo: más cerca de liquidación primero)
- Market — nombre del mercado (truncado a 25 chars)
- Strategy — estrategia que generó la posición (icono + abreviatura)
- Side — YES/NO con color (verde = YES, rojo = NO)
- Size — tamaño en USD
- Entry — precio de entrada
- Mark — precio actual (mark-to-market)
- PnL — P&L no realizado (USD + %, coloreado)
- τ% — porcentaje de vida del mercado consumida (barra de progreso miniatura)
- Tox — Markout Toxicity actual (número pequeño, coloreado: verde < 0.3, amarillo 0.3-0.7, rojo > 0.7)
- Liq — indicador de zona de liquidación (⚠ si τ > 85%, 🔴 si τ > 95%)

Codificación de filas:
- Fondo normal = posición normal
- Fondo ámbar suave = τ > 70% (fase de reducción)
- Fondo naranja = τ > 85% (liquidación inminente)
- Fondo rojo pulsante = τ > 95% (liquidación forzosa en curso)
- Borde izquierdo coloreado según P&L (verde = ganancia, rojo = pérdida)

Interacción: Click en una fila → detalle en el panel derecho.

5.3.2 Position Detail Panel (Panel derecho, superior)
Visualización: Panel de información detallada de la posición seleccionada.

Sección 1 — Info básica: Market name, Side, Size, Entry/Mark price, P&L con % y sparkline de 1h.

Sección 2 — Time-Decay Risk bar: Una barra de progreso horizontal grande que muestra τ (porcentaje de vida consumida). Colores:
- 0-70%: verde
- 70-85%: amarillo
- 85-95%: naranja
- 95-100%: rojo pulsante

Debajo de la barra, el (12/16)
[5/7/2026 8:27 PM] Hermes: risk_multiplier actual y la fecha estimada de liquidación forzosa. Un contador regresivo muestra "33% remaining until liquidation" en formato días/horas.

Sección 3 — Markout Toxicity Matrix: Una mini-tabla de 2×4 mostrando los markouts a t+1s, t+5s, t+10s, t+60s. Cada celda coloreada:
- Verde = P&L positivo (flujo limpio — el mercado se mueve a favor después de ejecutar)
- Rojo = P&L negativo (flujo tóxico — el mercado se mueve en contra)
- Intensidad proporcional a la magnitud

Debajo: Markout Score y clasificación textual con acción recomendada.

Sección 4 — Gas & MEV: Indicadores rápidos del coste de ejecución para esta posición:
- Gas Price actual en GWEI con flecha de tendencia
- Precio de POL en USD
- Coste estimado de transacción (para cerrar la posición)
- Slippage estimado
- Nivel de riesgo MEV (LOW/MEDIUM/HIGH/CRITICAL)
- Estrategia anti-MEV activa (mempool privada / Flashbots / ninguna)

5.3.3 Time-Decay Calendar (Panel inferior, ancho completo)
Visualización: Tabla-Gantt híbrida que muestra todas las posiciones ordenadas por tiempo hasta expiración.

Cada fila es una posición. La barra de progreso muestra τ%. Una marca vertical "NOW" divide el pasado del futuro.

Columnas:
- Market 
- τ% con número
- Barra de progreso (lo más prominente visualmente)
- Liquidation Date (fecha/hora estimada de liquidación forzosa)
- Indicador de fase: ⚠ (reducción) o 🔴 (liquidación forzosa)

Las filas se ordenan por τ descendente (las que expiran antes, arriba).

Alerta push cuando: Cualquier posición cruza τ > 85% (liquidación inminente en < 15% de vida restante). También cuando τ > 95% (liquidación forzosa activada — CRÍTICO).

5.3.4 Risk Multiplier Curve (Panel inferior izquierdo)
Visualización: Gráfico de la función risk_multiplier(τ) con las posiciones actuales superpuestas como puntos.

- Curva teórica: línea blanca mostrando risk_multiplier = f(τ)
- Puntos de posiciones: círculos coloreados (según P&L) ubicados en su τ actual
- Un tooltip al hacer hover muestra el nombre del mercado y risk_multiplier

Valor operativo: Ver de un vistazo qué posiciones están entrando en la zona de peligro y cuánto inventory cap les queda.

5.3.5 Correlation Matrix (Panel inferior derecho)
Visualización: Heatmap de matriz de correlación entre las posiciones abiertas.

Formato triangular superior (la diagonal es 1.0 por definición). Las celdas se colorean:
- Azul oscuro = correlación negativa fuerte (diversificación real — las posiciones se compensan)
- Blanco = sin correlación
- Rojo oscuro = correlación positiva fuerte (⚠ riesgo de concentración — si una pierde, todas pierden)

Debajo de la matriz: La correlación media del portfolio y el corr_scalar resultante (que reduce el tamaño de nuevas posiciones para evitar sobre-concentración).

Alerta push cuando: La correlación media del portfolio > 0.7 (sobre-concentración). También cuando una nueva posición propuesta tiene > 0.8 de correlación con una posición existente.

---

6. Arquitectura de Notificaciones

6.1 Sistema de Tres Capas

CAPA 1: TOAST NOTIFICATIONS (in-app, top-right, auto-dismiss)
├── Duración: 5s para warning, 15s para critical (requiere dismiss manual)
├── Sonido: opcional, configurable (mute para sesiones largas)
└── Agrupación: toasts del mismo tipo se stackean con contador

CAPA 2: ALERT STRIP (sticky top bar, persiste hasta resolver)
├── Muestra alertas activas que requieren monitoreo continuo
├── Cada alerta tiene: severidad, mensaje, timestamp, botón [ACK]
└── Máximo 5 alertas visibles (scroll si hay más)

CAPA 3: PUSH EXTERNO (Telegram, email, webhook)
├── Solo para alertas CRITICAL que requieren atención inmediata
├── Rate-limited: máximo 1 push por minuto por tipo de alerta
└── Contiene: resumen de 1 línea + link al dashboard


6.2 Clasificación de Alertas

🔴 CRITICAL****

• Severidad: 🔴 CRITICAL

• Color: Rojo pulsante

• Canal: Toast + Alert Strip + Push
 (13/16)
[5/7/2026 8:27 PM] Hermes: • Ejemplos: RECONCILING > 60s, Spoof Score > 0.7, τ > 95%, Kill Switch activado, Markout > 1.5

🟠 ERROR****

• Severidad: 🟠 ERROR

• Color: Naranja

• Canal: Toast + Alert Strip

• Ejemplos: Estrategia FROZEN tras 3 épocas negativas, Redis caído, Polygon RPC caído, Cluster Alpha detectado

🟡 WARNING****

• Severidad: 🟡 WARNING

• Color: Ámbar

• Canal: Toast

• Ejemplos: Rate-limit bucket < 10%, τ > 85%, Whale entra en mercado Top 50, Correlación portfolio > 0.7

🔵 INFO****

• Severidad: 🔵 INFO

• Color: Azul

• Canal: Log silencioso

• Ejemplos: Estrategia entra en PROBATION, Nuevo mercado en Top 50, Epoch completada

6.3 Política de Push vs Poll

PUSH (eventos que no pueden esperar):
- Cambio de estado de order book (CLEAN → RECONCILING y viceversa)
- Cambio de modo de degradación
- Estrategia entra en FROZEN o RETIRED
- Spoofing confirmado (S > 0.7)
- Whale con volumen > $10K entra en mercado monitoreado
- τ cruza umbrales (70%, 85%, 95%)
- Markout Toxicity > 1.5
- Kill Switch activado

STREAM (datos que se actualizan continuamente):
- OBI, TFI, Spoof Score (actualización por cada delta/trade de WebSocket)
- Precios mark-to-market de posiciones abiertas
- Heartbeats (WebSocket, Redis, Polygon)
- Rate-limit token availability
- Whale flow por mercado
- P&L de posiciones abiertas

POLL (datos que se consultan bajo demanda o cada época):
- Thompson Sampling distributions (recalcular cada época = 6h)
- Wallet Clustering (recalcular cada 24h)
- Sortino histórico (cada época)
- Backtest results (on-demand)
- Correlation graph completo (cada 6h)

---

7. Sistema de Color y Lenguaje Visual

7.1 Paleta de Colores

Background primario:    #0B0E14  (azul-negro profundo)
Background secundario:  #131820  (paneles, cards)
Background terciario:   #1A212B  (inputs, filas alternas)
Borde sutil:           #1E2936  (separadores)
Borde activo:          #2D3A4A  (hover states)

Texto primario:        #E8ECF1  (blanco suave — no blanco puro)
Texto secundario:      #8896A6  (gris medio)
Texto terciario:       #5A6978  (gris oscuro, metadatos)

Verde (profit/bull):   #00C853  → usar #00E676 para bright
Rojo (loss/bear):      #FF1744  → usar #FF5252 para bright
Ámbar (warning):       #F59E0B  → usar #FFB74D para bright
Azul (info/link):      #2979FF
Púrpura (whale/special): #7C4DFF

Gradiente para heatmaps:
  Spoofing: #FFFFFF → #FFF9C4 → #FFB74D → #FF6D00 → #D50000
  Correlación: #1A237E → #42A5F5 → #FFFFFF → #FF7043 → #BF360C


7.2 Tipografía

- Display (números grandes, KPIs): JetBrains Mono, 24-48px, weight 700
- Tablas: JetBrains Mono, 11-13px, weight 400 (alineación monoespaciada es crítica)
- Labels y headings: Inter, 11-14px, weight 500-600
- Código/terminal: JetBrains Mono, 12px
- Alertas: Inter, 13px, weight 600

7.3 Iconografía

Usar un set de iconos minimalista y coherente (Lucide Icons):
- 🟢🟡🔴 = estados (no usar emojis — usar SVG circles coloreados)
- ▲▼ = direcciones (up/down, buy/sell)
- ⬤◐⊘⊗ = estados de estrategia (active/probation/frozen/retired)
- 📡⚡🧠🐋 = heartbeats y sistemas (usar iconos SVG de 16px)
- ⚠🔴 = alertas (warning/critical)

---

8. Especificaciones Técnicas para Implementación

8.1 Stack Recomendado

**Framework**

• Capa: Framework

• Tecnología: React 19 + TypeScript

• Justificación: Tipado estricto para interfaces de datos financieros

**Build**

• Capa: Build

• Tecnología: Vite

• Justificación: Hot-reload instantáneo, bundle splitting

**Estilos**

• Capa: Estilos

• Tecnología: Tailwind CSS + custom theme

• Justificación: Utilidades atómicas con design tokens

**Gráficos**

• Capa: Gráficos

• Tecnología: D3.js (SVG para complejidad) + Canvas (para rendimiento en tiempo real)

• Justificación: D3 para heatmaps complejos, Canvas para sparklines y streaming data

**State**

• Capa: State
 (14/16)
[5/7/2026 8:27 PM] Hermes: • Tecnología: Zustand (lightweight store) + React Query (server cache)

• Justificación: Zustand para estado UI local, React Query para datos del backend

**Streaming**

• Capa: Streaming

• Tecnología: Native EventSource (SSE)

• Justificación: Conexión unidireccional desde Redis Pub/Sub → servidor HTTP → navegador

**WebSocket**

• Capa: WebSocket

• Tecnología: ReconnectingWebSocket wrapper

• Justificación: Para datos de ultra-baja latencia (opcional si SSE basta)

**Backend bridge**

• Capa: Backend bridge

• Tecnología: FastAPI (Python)

• Justificación: Servir el bundle React + exponer endpoints SSE/WS que leen de Redis

8.2 Puente Backend-Frontend

El dashboard NO se conecta directamente a Redis. En su lugar:

Redis Pub/Sub → Python FastAPI Server → SSE/WS → React Dashboard


El servidor FastAPI:
1. Se suscribe a los canales Redis relevantes (book:delta, trade:print, whale:flow, etc.)
2. Transforma los mensajes en JSON estructurado
3. Los transmite vía Server-Sent Events (SSE) a los clientes conectados
4. Sirve el bundle estático de React en producción

Endpoints SSE:
- GET /stream/health → heartbeats, rate-limit, reconciliation status
- GET /stream/spoofing?market_id=X → OBI, TFI, Spoof Score para un mercado
- GET /stream/whales → whale flow, conviction multipliers
- GET /stream/positions → P&L, markouts, time-decay updates
- GET /stream/portfolio → asignaciones, Sortino, estrategias

Endpoints REST (polling):
- GET /api/portfolio/bandit → estado completo del Bandit (distribuciones, rankings)
- GET /api/clusters → resultados del clustering (actualizado cada 24h)
- GET /api/backtest?strategy=X → resultados de backtest históricos

8.3 Rendimiento

- Target FPS: 30 FPS para actualizaciones de UI (no tiene sentido ir a 60 FPS para datos financieros — el ojo no puede procesar cambios tan rápidos en números)
- Virtualización de tablas: Usar react-window para virtualizar tablas con > 20 filas
- Canvas para sparklines: Renderizar sparklines y heatmaps en Canvas (no SVG) cuando hay > 100 elementos — Canvas es 5-10x más rápido para muchos elementos pequeños
- Throttle de actualizaciones: El stream de WebSocket puede producir > 100 mensajes/segundo. Throttlear la UI a 10 actualizaciones/segundo máximo. Acumular y renderizar en lotes.
- Bundle splitting: Cargar D3.js solo en los paneles que lo necesitan (lazy loading por pestaña)

---

9. Resumen de Vistas

**System Health**

• Panel: System Health

• Propósito: ¿Puedo confiar en los datos?

• Widgets Clave: Heartbeats, Rate-Limit gauges, Reconciliation Matrix, Latency Budget

• Push Alerts: RECONCILING > 60s, Degradación FULL→MINIMAL, Rate-limit < 10%

**Portfolio Arena**

• Panel: Portfolio Arena

• Propósito: ¿Mi capital está bien asignado?

• Widgets Clave: Strategy Rankings, Thompson Distributions, Capital Allocation Bars, Kelly Pipeline

• Push Alerts: Estrategia FROZEN/RETIRED, Correlación portfolio > 0.7

**Oracle Radar**

• Panel: Oracle Radar

• Propósito: ¿Hay manipulación? ¿Qué hacen las whales?

• Widgets Clave: Spoofing Heatmap, OBI/TFI Gauge, Whales Table, Wallet Clusters

• Push Alerts: Spoof Confirmado (S > 0.7), Alpha Whale entra mercado Top 50, Cluster Alpha detectado

**Risk Monitor**

• Panel: Risk Monitor

• Propósito: ¿Cuánto riesgo tengo?

• Widgets Clave: Open Positions Table, Time-Decay Calendar, Markout Matrix, Correlation Heatmap

• Push Alerts: τ > 85%/95%, Markout > 1.5, Correlación > 0.8 entre posiciones

---

10. Próximos Pasos

1. Validar este diseño contigo — ¿Hay algo que falte, sobre, o deba cambiar?
2. Crear wireframes interactivos (HTML/CSS estático) de cada panel para validar layout antes de escribir código real.
3. Implementar el servidor FastAPI bridge que conecta Redis → SSE para el dashboard. (15/16)
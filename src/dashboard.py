"""
🔬 Polymarket Scout Lab — Dashboard profesional
===============================================
Monitoriza mercados de predicción, ejecuta backtests,
gestiona tu portfolio de paper trading y recibe alertas.

Lanzar con: streamlit run src/dashboard.py
"""

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Polymarket Scout Lab",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown("""
<style>
    /* Clean, modern look */
    .main .block-container { padding-top: 1rem; }
    header[data-testid="stHeader"] { background: transparent; }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 1rem;
    }
    div[data-testid="stMetric"] label {
        color: rgba(255,255,255,0.5) !important;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #fff !important;
        font-size: 1.6rem;
        font-weight: 700;
    }

    /* Tabs */
    button[data-baseweb="tab"] {
        font-size: 0.9rem;
        font-weight: 500;
        padding: 0.5rem 1.2rem;
    }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #6366f1 !important;
    }

    /* DataFrames */
    div[data-testid="stDataFrame"] {
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 8px;
        overflow: hidden;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0f0f1a 0%, #1a1a2e 100%);
        border-right: 1px solid rgba(255,255,255,0.05);
    }

    /* Buttons */
    div.stButton > button {
        border-radius: 8px;
        font-weight: 500;
        transition: all 0.2s;
    }
    div.stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
    }

    /* Expanders */
    div[data-testid="stExpander"] {
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 10px;
    }

    /* Hide Streamlit branding */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ── Database Connection ──────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent / "data" / "polymarket.db"


@st.cache_resource(ttl=10)
def get_connection():
    """Cached database connection (refreshes every 10s)."""
    db = DB_PATH
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── Data Loaders ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_portfolio_stats():
    conn = get_connection()
    if not conn:
        return {}
    try:
        cur = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(SUM(amount), 0) as total_invested
            FROM paper_trades
        """)
        row = cur.fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


@st.cache_data(ttl=30)
def load_recent_trades(limit=20):
    conn = get_connection()
    if not conn:
        return pd.DataFrame()
    try:
        return pd.read_sql("""
            SELECT id, strategy, side, question, price, amount, pnl, status,
                   datetime(entry_timestamp, 'unixepoch', 'localtime') as entry_time,
                   datetime(close_timestamp, 'unixepoch', 'localtime') as close_time
            FROM paper_trades
            ORDER BY id DESC
            LIMIT ?
        """, conn, params=(limit,))
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_alerts(limit=30):
    conn = get_connection()
    if not conn:
        return pd.DataFrame()
    try:
        return pd.read_sql("""
            SELECT s.id, s.condition_id, s.signal_type, s.score, s.detail,
                   datetime(s.timestamp, 'unixepoch', 'localtime') as time,
                   snap.question, snap.event_title, snap.price_yes, snap.volume
            FROM signals s
            LEFT JOIN snapshots snap ON s.condition_id = snap.condition_id
                AND s.timestamp = snap.timestamp
            ORDER BY s.timestamp DESC
            LIMIT ?
        """, conn, params=(limit,))
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_market_summary():
    conn = get_connection()
    if not conn:
        return pd.DataFrame()
    try:
        return pd.read_sql("""
            SELECT condition_id, question, event_title, price_yes, volume, spread,
                   datetime(timestamp, 'unixepoch', 'localtime') as last_update
            FROM snapshots
            WHERE timestamp = (SELECT MAX(timestamp) FROM snapshots)
            ORDER BY volume DESC
            LIMIT 50
        """, conn)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_snapshots_for_backtest(days=7):
    conn = get_connection()
    if not conn:
        return pd.DataFrame()
    cutoff = int(time.time()) - days * 86400
    try:
        return pd.read_sql("""
            SELECT condition_id, question, price_yes, volume, spread, timestamp
            FROM snapshots
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, conn, params=(cutoff,))
    except Exception:
        return pd.DataFrame()


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🔬 Scout Lab")
    st.markdown("Laboratorio de estrategias de predicción")

    st.divider()

    # Auto-refresh
    auto_refresh = st.checkbox("🔄 Auto-refresh (30s)", value=True)
    if auto_refresh:
        st.caption(f"Actualizando cada 30s • {datetime.now().strftime('%H:%M:%S')}")
        time.sleep(30)
        st.rerun()

    st.divider()

    # Quick stats from DB
    stats = load_portfolio_stats()
    if stats:
        st.metric("Total trades", stats.get("total", 0))
        st.metric("P&L realizado", f"${stats.get('total_pnl', 0):.2f}")
        st.metric("Win rate",
                  f"{stats.get('wins', 0) / max(stats.get('closed', 1), 1) * 100:.0f}%"
                  if stats.get("closed") else "—")

    st.divider()
    st.caption(f"Datos: {DB_PATH.name} | {datetime.now().strftime('%d/%m/%Y %H:%M')}")


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_overview, tab_backtest, tab_portfolio, tab_markets, tab_alerts = st.tabs([
    "📊 Overview", "📈 Backtest", "💼 Portfolio", "🔍 Mercados", "⚠️ Alertas"
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════

with tab_overview:
    st.header("Resumen del Laboratorio")

    # KPI row
    stats = load_portfolio_stats()
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Balance", "$1,000.00",
                  delta=f"${stats.get('total_pnl', 0):+.2f}" if stats.get("total_pnl") else None)
    with c2:
        st.metric("Total Trades", stats.get("total", 0))
    with c3:
        wins = stats.get("wins", 0)
        closed = max(stats.get("closed", 0), 1)
        st.metric("Win Rate", f"{wins / closed * 100:.0f}%")
    with c4:
        st.metric("P&L Realizado", f"${stats.get('total_pnl', 0):.2f}")
    with c5:
        st.metric("Abiertas", stats.get("open", 0))

    st.divider()

    # Recent trades
    st.subheader("Últimos trades")
    trades_df = load_recent_trades(10)
    if not trades_df.empty:
        # Format columns
        display_df = trades_df[["id", "strategy", "side", "question", "price", "amount", "pnl", "status", "entry_time"]].copy()
        display_df["price"] = display_df["price"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        display_df["amount"] = display_df["amount"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "—")
        display_df["pnl"] = display_df["pnl"].apply(
            lambda x: f"${x:+.2f}" if pd.notna(x) and x != 0 else ("—" if pd.isna(x) else "$0.00"))
        display_df.columns = ["#", "Estrategia", "Lado", "Mercado", "Precio", "Cantidad", "P&L", "Estado", "Fecha"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.info("📭 No hay trades todavía. Ejecuta un backtest para generar datos.")

    st.divider()

    # Alertas recientes
    st.subheader("Últimas alertas")
    alerts_df = load_alerts(5)
    if not alerts_df.empty:
        for _, alert in alerts_df.iterrows():
            score = alert.get("score", 0)
            emoji = "🔴" if score >= 50 else "🟡" if score >= 30 else "🟢"
            st.markdown(
                f"{emoji} **{alert['signal_type']}** (+{score}) — "
                f"*{alert.get('question', '?')[:80]}* — {alert.get('time', '?')}"
            )
    else:
        st.info("📭 No hay alertas todavía.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: BACKTEST
# ═══════════════════════════════════════════════════════════════════════════════

with tab_backtest:
    st.header("Backtest de Estrategias")

    st.markdown("""
    El backtest repite tus datos históricos y simula qué habría pasado si hubieras
    apostado siguiendo cada estrategia. Los resultados te dicen qué estrategia
    habría sido más rentable.
    """)

    col1, col2, col3 = st.columns(3)
    with col1:
        days = st.slider("Días de histórico", 1, 90, 7, help="Cuántos días hacia atrás analizar")
    with col2:
        strategy_filter = st.selectbox(
            "Estrategia",
            ["Todas", "momentum_follow", "contrarian", "consensus_breakout",
             "volume_breakout", "new_market_yes"],
            help="Filtrar a una estrategia concreta o ver todas"
        )
    with col3:
        run_btn = st.button("▶️ Ejecutar Backtest", type="primary", use_container_width=True)

    if run_btn:
        with st.spinner("Ejecutando backtest..."):
            # Run the backtest using our modules
            import yaml
            import json
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))

            from src.tracker import Tracker
            from src.paper_trader import PaperTrader
            from src.backtester import Backtester

            config_path = Path(__file__).parent.parent / "config.yaml"
            with open(config_path) as f:
                config = yaml.safe_load(f)

            tracker = Tracker(str(DB_PATH))
            tracker.init_db()
            tracker.init_paper_trading()

            pt = PaperTrader(tracker, initial_balance=1000.0, position_size_pct=0.05)
            bt = Backtester(tracker, pt, signal_config=config.get("signals", {}))

            strat = None if strategy_filter == "Todas" else strategy_filter
            results = bt.run(strategy_name=strat, days_back=days)

            # Display results
            if results:
                # Summary table
                rows = []
                for name, data in results.items():
                    r = data["report"]
                    rows.append({
                        "Estrategia": name,
                        "Trades": r["total_trades"],
                        "Cerrados": r["closed_positions"],
                        "Ganados": r["wins"],
                        "Perdidos": r["losses"],
                        "Win Rate": f"{r['win_rate']*100:.0f}%",
                        "P&L": f"${r['realized_pnl']:.2f}",
                        "ROI": f"{r['roi']*100:.1f}%",
                        "Invertido": f"${r['total_invested']:.2f}",
                    })
                summary_df = pd.DataFrame(rows)
                st.subheader("Resultados por estrategia")
                st.dataframe(summary_df, use_container_width=True, hide_index=True)

                # Bar chart: P&L per strategy
                chart_data = []
                for name, data in results.items():
                    chart_data.append({
                        "Estrategia": name,
                        "P&L ($)": data["report"]["realized_pnl"],
                        "Win Rate (%)": data["report"]["win_rate"] * 100,
                    })
                chart_df = pd.DataFrame(chart_data)

                col_a, col_b = st.columns(2)
                with col_a:
                    fig = px.bar(chart_df, x="Estrategia", y="P&L ($)",
                                 title="P&L por Estrategia",
                                 color="P&L ($)",
                                 color_continuous_scale=["red", "gray", "green"])
                    fig.update_layout(height=350)
                    st.plotly_chart(fig, use_container_width=True)
                with col_b:
                    fig2 = px.bar(chart_df, x="Estrategia", y="Win Rate (%)",
                                  title="Win Rate por Estrategia",
                                  color="Win Rate (%)",
                                  color_continuous_scale=["orange", "yellow", "green"])
                    fig2.update_layout(height=350)
                    st.plotly_chart(fig2, use_container_width=True)

                # All individual trades
                st.subheader("Trades individuales")
                all_trades = []
                for name, data in results.items():
                    for t in data.get("trades", []):
                        all_trades.append({
                            "Estrategia": name,
                            "Lado": t.get("side", "?"),
                            "Precio": f"{t.get('price', 0)*100:.1f}%" if t.get("price") else "—",
                            "Cantidad": f"${t.get('amount', 0):.2f}",
                            "P&L": f"${t.get('pnl', 0):+.2f}" if t.get("pnl") is not None else "—",
                            "Estado": t.get("status", "?"),
                            "Mercado": (t.get("question", "") or "")[:60],
                        })
                if all_trades:
                    st.dataframe(pd.DataFrame(all_trades), use_container_width=True, hide_index=True)
            else:
                st.warning("No se encontraron datos para backtest. Asegúrate de que hay snapshots en la base de datos.")
    else:
        st.info("👆 Selecciona los parámetros y pulsa **Ejecutar Backtest** para empezar.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: PORTFOLIO
# ═══════════════════════════════════════════════════════════════════════════════

with tab_portfolio:
    st.header("Paper Trading Portfolio")

    st.markdown("""
    Tu cartera virtual de paper trading. No se usa dinero real — es un simulador
    para probar estrategias antes de arriesgar capital.
    """)

    # Portfolio KPIs
    stats = load_portfolio_stats()
    total_invested = stats.get("total_invested", 0)
    total_pnl = stats.get("total_pnl", 0)
    balance = 1000.0 - total_invested + total_pnl

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("💰 Balance", f"${balance:.2f}")
    with c2:
        st.metric("📈 Invertido", f"${total_invested:.2f}")
    with c3:
        delta_color = "normal" if total_pnl >= 0 else "inverse"
        st.metric("💵 P&L Realizado", f"${total_pnl:+.2f}",
                  delta=f"${total_pnl:+.2f}" if total_pnl != 0 else None)
    with c4:
        wins = stats.get("wins", 0)
        closed = max(stats.get("closed", 0), 1)
        st.metric("🏆 Win Rate", f"{wins / closed * 100:.0f}%" if closed > 0 else "—")

    st.divider()

    # Open positions
    st.subheader("📂 Posiciones abiertas")
    conn = get_connection()
    if conn:
        try:
            open_pos = pd.read_sql("""
                SELECT id, strategy, side, question, price, amount,
                       datetime(entry_timestamp, 'unixepoch', 'localtime') as opened
                FROM paper_trades
                WHERE status = 'open'
                ORDER BY entry_timestamp DESC
            """, conn)
            if not open_pos.empty:
                display = open_pos.copy()
                display["price"] = display["price"].apply(lambda x: f"{x*100:.1f}%")
                display["amount"] = display["amount"].apply(lambda x: f"${x:.2f}")
                display.columns = ["#", "Estrategia", "Lado", "Mercado", "Precio", "Cantidad", "Abierto"]
                st.dataframe(display, use_container_width=True, hide_index=True)
            else:
                st.info("No hay posiciones abiertas.")
        except Exception:
            st.info("No hay posiciones abiertas.")

    st.divider()

    # Closed trades history
    st.subheader("📋 Historial de trades")
    trades_df = load_recent_trades(50)
    if not trades_df.empty:
        # P&L chart over time
        closed_trades = trades_df[trades_df["status"] == "closed"].copy()
        if not closed_trades.empty:
            closed_trades = closed_trades.sort_values("close_time")
            closed_trades["cumulative_pnl"] = closed_trades["pnl"].cumsum()
            fig = px.line(closed_trades, x="close_time", y="cumulative_pnl",
                          title="P&L Acumulado",
                          labels={"close_time": "Fecha", "cumulative_pnl": "P&L ($)"})
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

        # Full table
        display = trades_df[["id", "strategy", "side", "question", "price", "amount", "pnl", "status", "entry_time"]].copy()
        display["price"] = display["price"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        display["amount"] = display["amount"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "—")
        display["pnl"] = display["pnl"].apply(
            lambda x: f"${x:+.2f}" if pd.notna(x) and x != 0 else ("—" if pd.isna(x) else "$0.00"))
        display.columns = ["#", "Estrategia", "Lado", "Mercado", "Precio", "Cantidad", "P&L", "Estado", "Fecha"]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No hay trades todavía. Ejecuta un backtest para empezar a generar datos.")

    st.divider()

    # Manual trade
    st.subheader("🎯 Apostar manualmente (paper trade)")
    st.markdown("Simula una apuesta manual sin arriesgar dinero real.")
    markets_df = load_market_summary()
    if not markets_df.empty:
        market_options = [f"{row['question'][:60]} ({row['price_yes']*100:.0f}% YES)" for _, row in markets_df.head(30).iterrows()]
        selected_market = st.selectbox("Mercado", market_options, key="manual_market")
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            side = st.selectbox("Lado", ["YES", "NO"], key="manual_side")
        with col_b:
            amount = st.number_input("Cantidad ($)", min_value=1.0, max_value=100.0, value=10.0, step=5.0, key="manual_amount")
        with col_c:
            if st.button("🎯 Apostar", type="primary", use_container_width=True, key="manual_btn"):
                idx = market_options.index(selected_market)
                row = markets_df.iloc[idx]
                price = row["price_yes"] if side == "YES" else (1.0 - row["price_yes"])
                conn = get_connection()
                conn.execute("""
                    INSERT INTO paper_trades (condition_id, question, side, amount, price, shares,
                                             status, entry_timestamp, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?, 'manual')
                """, (row["condition_id"], row["question"], side, amount, price,
                      round(amount / price, 4), int(time.time())))
                conn.commit()
                st.success(f"✅ Trade manual colocado: {side} en \"{row['question'][:50]}...\" @ {price*100:.1f}% — ${amount:.2f}")
                st.rerun()
    else:
        st.warning("No hay datos de mercado disponibles.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: MERCADOS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_markets:
    st.header("Mercados de Polymarket")

    st.markdown("Datos en tiempo real de los mercados que está monitorizando el scout.")

    markets_df = load_market_summary()

    if not markets_df.empty:
        # Volume distribution
        col_a, col_b = st.columns(2)
        with col_a:
            st.metric("Mercados activos", len(markets_df))
        with col_b:
            total_vol = markets_df["volume"].sum()
            st.metric("Volumen total", f"${total_vol/1e6:.1f}M" if total_vol > 1e6 else f"${total_vol/1e3:.1f}K")

        # Top markets by price
        st.subheader("Top mercados por probabilidad")
        top = markets_df.nlargest(15, "price_yes")[["question", "price_yes", "volume", "spread"]].copy()
        top["price_yes"] = top["price_yes"].apply(lambda x: f"{x*100:.1f}%")
        top["volume"] = top["volume"].apply(lambda x: f"${x/1e6:.1f}M" if x > 1e6 else f"${x/1e3:.1f}K")
        top["spread"] = top["spread"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        top.columns = ["Mercado", "Prob. YES", "Volumen", "Spread"]
        st.dataframe(top, use_container_width=True, hide_index=True)

        # Volume chart
        st.subheader("Distribución de volumen")
        vol_data = markets_df.nlargest(20, "volume")[["question", "volume"]].copy()
        vol_data["question"] = vol_data["question"].str[:50]
        fig = px.bar(vol_data, x="volume", y="question", orientation="h",
                     title="Top 20 mercados por volumen",
                     labels={"volume": "Volumen ($)", "question": ""})
        fig.update_layout(height=500, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("No hay datos de mercado. Asegúrate de que el cron job está corriendo.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5: ALERTAS
# ═══════════════════════════════════════════════════════════════════════════════

with tab_alerts:
    st.header("Historial de Alertas")

    st.markdown("""
    Señales detectadas por el scout. Cada alerta indica que un mercado mostró
    alguna anomalía (momentum, volumen, spread) que podría ser una oportunidad.
    """)

    alerts_df = load_alerts(100)

    if not alerts_df.empty:
        # Alert stats
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total alertas", len(alerts_df))
        with c2:
            avg_score = alerts_df["score"].mean()
            st.metric("Score promedio", f"{avg_score:.0f}/100")
        with c3:
            top_signal = alerts_df["signal_type"].mode().iloc[0] if len(alerts_df) > 0 else "—"
            st.metric("Señal más común", top_signal)

        st.divider()

        # Signal type distribution
        signal_counts = alerts_df["signal_type"].value_counts().reset_index()
        signal_counts.columns = ["Señal", "Cantidad"]
        fig = px.pie(signal_counts, values="Cantidad", names="Señal",
                     title="Distribución de tipos de señal")
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

        # Alerts table
        st.subheader("Todas las alertas")
        display = alerts_df[["signal_type", "score", "question", "price_yes", "time"]].copy()
        display["price_yes"] = display["price_yes"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
        display.columns = ["Señal", "Score", "Mercado", "Precio YES", "Fecha"]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("📭 No hay alertas todavía. Las alertas se generan cuando el scout detecta movimientos significativos en los mercados.")

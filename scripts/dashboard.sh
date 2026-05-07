#!/bin/bash
# Polymarket Scout Dashboard — launcher
# Adds user-local site-packages to Python path for streamlit/plotly
export PYTHONPATH="/opt/data/home/.local/lib/python3.13/site-packages:$PYTHONPATH"
cd /opt/data/polymarket-scout
exec /opt/hermes/.venv/bin/python3 -m streamlit run src/dashboard.py --server.port 8501 --server.headless true "$@"

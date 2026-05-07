#!/bin/bash
# Polymarket Scout — cron wrapper
# Runs every 5 minutes, sends alerts directly to Telegram (no LLM needed)

cd /opt/data/polymarket-scout
/opt/hermes/.venv/bin/python -m src.cli scan >> /opt/data/polymarket-scout/data/cron.log 2>&1

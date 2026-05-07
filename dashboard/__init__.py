"""
Polymarket Scout Lab v2.0 — Dashboard Backend Server.

FastAPI server that bridges the Python trading engine with the React frontend.
Streams real-time data via Server-Sent Events (SSE) and exposes REST API
endpoints for polling data.

Architecture:
    Redis Pub/Sub → Python orchestrator → FastAPI → SSE/JSON → React Dashboard
"""

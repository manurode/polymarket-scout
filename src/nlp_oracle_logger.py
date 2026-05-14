"""
NLP Oracle Logger — Log dedicado del Oráculo de Sentimiento en disco.

Escribe exclusivamente eventos del NLP Oracle a ``data/nlp_oracle.log``:
- Ingestión de titulares (fuente, preview)
- Validaciones de premise (APPROVED/REJECTED con scores)
- Estado del streamer (conexión, canales, mensajes recibidos)
- Estado del buffer (tamaño, purgas, TTL)
- Carga/errores del modelo Zero-Shot
- Sistema (start/stop)

NUNCA escribe:
- Líneas del orchestrator (Radar, MM quotes, etc.)
- HTTP access logs
- Health checks, degradación
- Eventos de trading (eso va en trading.log)

El log de consola (Python root logger) permanece intacto.

Usage:
    from src.nlp_oracle_logger import nlp_log

    nlp_log.streamer_connected(channels, resolved_count)
    nlp_log.headline_ingested(source, text)
    nlp_log.premise_validated(question, side, score, threshold, approved, headline)
    nlp_log.buffer_status(count, oldest_age, ingested_total)
    nlp_log.model_loaded(model_name, device)
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Logger setup ────────────────────────────────────────────────────────────

NLP_LOG_NAME = "nlp_oracle"
NLP_LOG_FILE = "data/nlp_oracle.log"

_nlp_logger = logging.getLogger(NLP_LOG_NAME)
_nlp_logger.setLevel(logging.DEBUG)
_nlp_logger.propagate = False  # NO enviar al root logger (consola)

# Limpiar handlers previos (evitar duplicados en reloads)
_nlp_logger.handlers.clear()

# File handler — append mode
_log_dir = Path(NLP_LOG_FILE).parent
_log_dir.mkdir(parents=True, exist_ok=True)

_file_handler = logging.FileHandler(NLP_LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_nlp_logger.addHandler(_file_handler)


# ── Public API ──────────────────────────────────────────────────────────────

class NlpOracleLog:
    """Logger dedicado del NLP Oracle. Métodos helper para cada tipo de evento."""

    def __init__(self):
        self._log = _nlp_logger

    # ── Sistema ──────────────────────────────────────────────────────────

    def system_start(self) -> None:
        self._log.info("=" * 60)
        self._log.info(
            "🔮 NLP ORACLE LOG — %s",
            datetime.now(timezone.utc).isoformat(),
        )
        self._log.info("=" * 60)

    def system_stop(self, reason: str = "") -> None:
        reason_str = f" — {reason}" if reason else ""
        self._log.info("⏹️ NLP ORACLE STOP%s", reason_str)

    def oracle_config(self, enabled: bool, model: str, threshold: float,
                      channels: list[str], buffer_ttl: int) -> None:
        self._log.info(
            "⚙️ CONFIG | enabled=%s model=%s threshold=%.2f ttl=%ds channels=%s",
            enabled, model, threshold, buffer_ttl,
            ", ".join(channels) if channels else "(none)",
        )

    # ── Streamer ─────────────────────────────────────────────────────────

    def streamer_connecting(self, channels: list[str]) -> None:
        self._log.info(
            "📡 STREAMER | Conectando a Telegram... canales=%s",
            ", ".join(channels),
        )

    def streamer_connected(self, channels: list[str], resolved_count: int) -> None:
        self._log.info(
            "📡 STREAMER | ✅ Conectado — %d/%d canales resueltos",
            resolved_count, len(channels),
        )

    def streamer_auth_needed(self) -> None:
        self._log.warning(
            "📡 STREAMER | ⚠️  AUTENTICACIÓN REQUERIDA — "
            "ejecuta el streamer interactivamente una vez para "
            "introducir el código de verificación de Telegram"
        )

    def streamer_disconnected(self) -> None:
        self._log.info("📡 STREAMER | Desconectado")

    def streamer_error(self, error: str) -> None:
        self._log.error("📡 STREAMER | ❌ Error: %s", error)

    def streamer_channel_resolved(self, username: str, resolved: bool,
                                  entity_type: str = "") -> None:
        status = "✅" if resolved else "❌"
        type_str = f" ({entity_type})" if entity_type else ""
        self._log.info(
            "📡 STREAMER | Canal %s %s%s",
            username, status, type_str,
        )

    # ── Ingestión ────────────────────────────────────────────────────────

    def headline_ingested(self, source: str, text: str) -> None:
        preview = text.replace("\n", " ")[:120]
        self._log.info(
            "📰 INGEST | source=%-20s | %s",
            source, preview,
        )

    def headline_dropped(self, reason: str, text: str = "") -> None:
        preview = text.replace("\n", " ")[:60] if text else ""
        self._log.debug(
            "📰 DROP | reason=%-15s | %s",
            reason, preview,
        )

    # ── Buffer ───────────────────────────────────────────────────────────

    def buffer_purge(self, removed: int, remaining: int) -> None:
        self._log.debug(
            "🗑️  BUFFER PURGE | removed=%d remaining=%d",
            removed, remaining,
        )

    def buffer_status(self, count: int, oldest_age_s: float | None,
                      ingested_total: int) -> None:
        oldest_str = f"{oldest_age_s:.0f}s" if oldest_age_s is not None else "N/A"
        self._log.debug(
            "📊 BUFFER | count=%d oldest=%s ingested_total=%d",
            count, oldest_str, ingested_total,
        )

    # ── Modelo ───────────────────────────────────────────────────────────

    def model_loading(self, model_name: str) -> None:
        self._log.info("🧠 MODEL | Cargando %s ...", model_name)

    def model_loaded(self, model_name: str, device: str) -> None:
        self._log.info("🧠 MODEL | ✅ Cargado %s (device=%s)", model_name, device)

    def model_error(self, model_name: str, error: str) -> None:
        self._log.error("🧠 MODEL | ❌ Error cargando %s: %s", model_name, error)

    # ── Validación ──────────────────────────────────────────────────────

    def premise_validated(
        self,
        token_id: str,
        question: str,
        side: str,
        score: float,
        threshold: float,
        approved: bool,
        top_headline: str,
        buffer_count: int,
    ) -> None:
        action = "✅ APPROVED" if approved else "❌ REJECTED"
        headline_preview = top_headline[:80] if top_headline else "(sin noticias)"
        score_bar = _score_bar(score)

        self._log.info(
            "🔍 VALIDATE | %s | token=%-16s side=%-3s score=%.3f %s thr=%.2f buf=%-3d | "
            "Q: %s | News: %s",
            action,
            token_id[:16] if token_id else "?",
            side,
            score,
            score_bar,
            threshold,
            buffer_count,
            question[:60],
            headline_preview,
        )

    def premise_skipped_no_buffer(self, token_id: str, question: str) -> None:
        self._log.debug(
            "🔍 VALIDATE | ⏭️  SKIP (buffer vacío) | token=%s Q=%s",
            token_id[:16] if token_id else "?",
            question[:60],
        )

    def premise_error(self, token_id: str, question: str, error: str) -> None:
        self._log.error(
            "🔍 VALIDATE | ❌ ERROR | token=%s Q=%s | %s",
            token_id[:16] if token_id else "?",
            question[:60],
            error,
        )

    # ── Getters ──────────────────────────────────────────────────────────

    def get_log_path(self) -> str:
        return str(Path(NLP_LOG_FILE).resolve())


# ── Helpers ─────────────────────────────────────────────────────────────────

def _score_bar(score: float, width: int = 10) -> str:
    """Mini barra ASCII para visualizar el score."""
    filled = int(score * width)
    empty = width - filled
    return f"[{'#' * filled}{'-' * empty}]"


# ── Singleton ───────────────────────────────────────────────────────────────

nlp_log = NlpOracleLog()

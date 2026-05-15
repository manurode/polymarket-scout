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

    # ── Orchestrator trigger ──
    nlp_log.trigger_context(token_id, strategy, side, confidence, question,
                            volume_spike_ratio=2.8, market_regime="trending")

    # ── Streamer ──
    nlp_log.streamer_message_raw(channel, msg_id, text, has_media, ts)
    nlp_log.streamer_message_filtered(reason, msg_id, channel, text)
    nlp_log.streamer_connected(channels, resolved_count)
    nlp_log.streamer_channel_resolved(username, True, "Channel")

    # ── Buffer ──
    nlp_log.buffer_added(headline, count, max_size, total)
    nlp_log.buffer_full(max_size, dropped_headline)
    nlp_log.buffer_status(count, oldest_age, ingested_total)

    # ── Model ──
    nlp_log.model_loading(model_name)
    nlp_log.model_loaded(model_name, device)
    nlp_log.model_load_timing(model_name, elapsed_s, device)

    # ── Validation pipeline ──
    nlp_log.validation_start(token_id, question, side, target_label, buf_count, thr)
    nlp_log.oracle_disabled(token_id, question)
    nlp_log.headline_classified(idx, total, premise, scores, target_label)
    nlp_log.headline_classification_error(idx, total, premise, error)
    nlp_log.premise_validated(token_id, question, side, score, thr, approved, headline, buf)
    nlp_log.premise_skipped_no_buffer(token_id, question)
    nlp_log.validation_complete(token_id, approved, score, thr, target, checked, total, ms)
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

    def streamer_message_raw(self, channel: str, msg_id: int,
                             text: str, has_media: bool,
                             msg_timestamp: str = "") -> None:
        """Log raw Telegram message BEFORE any filtering.
        
        This is the FIRST log entry in the pipeline — every single message
        that arrives from Telegram is logged here, regardless of whether
        it gets filtered out later. Essential for debugging ingestion issues.
        """
        preview = text.replace("\n", " ")[:200]
        media_flag = "📎" if has_media else "📝"
        ts_str = f" ts={msg_timestamp}" if msg_timestamp else ""
        self._log.debug(
            "📡 RAW_MSG | id=%-10d chan=%-20s %s | len=%d%s | %s",
            msg_id, channel, media_flag, len(text), ts_str, preview,
        )

    def streamer_message_filtered(self, reason: str, msg_id: int,
                                  channel: str, text: str = "") -> None:
        """Log why a raw message was filtered out (before buffer ingestion)."""
        preview = text.replace("\n", " ")[:80] if text else ""
        self._log.debug(
            "📡 FILTER | reason=%-15s id=%d chan=%s | %s",
            reason, msg_id, channel, preview,
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

    def buffer_added(self, headline_preview: str, count: int,
                     max_size: int, ingested_total: int) -> None:
        """Log when a headline is successfully added to the buffer."""
        pct = f"{(count / max_size) * 100:.0f}%"
        self._log.debug(
            "📊 BUFFER_ADD | count=%d/%d (%s) total=%d | %s",
            count, max_size, pct, ingested_total,
            headline_preview[:100],
        )

    def buffer_full(self, max_size: int, dropped_headline_preview: str) -> None:
        """Log when the buffer is at max capacity (oldest entry evicted)."""
        self._log.warning(
            "📊 BUFFER_FULL | max=%d — oldest evicted | dropped: %s",
            max_size, dropped_headline_preview[:80],
        )

    # ── Modelo ───────────────────────────────────────────────────────────

    def model_loading(self, model_name: str) -> None:
        self._log.info("🧠 MODEL | Cargando %s ...", model_name)

    def model_loaded(self, model_name: str, device: str) -> None:
        self._log.info("🧠 MODEL | ✅ Cargado %s (device=%s)", model_name, device)

    def model_load_timing(self, model_name: str, elapsed_s: float,
                          device: str) -> None:
        """Log how long the model took to load."""
        self._log.info(
            "🧠 MODEL | ⏱️  Carga completada: %s en %.1fs (device=%s)",
            model_name, elapsed_s, device,
        )

    def model_error(self, model_name: str, error: str) -> None:
        self._log.error("🧠 MODEL | ❌ Error cargando %s: %s", model_name, error)

    # ── Validación ──────────────────────────────────────────────────────

    def validation_start(
        self,
        token_id: str,
        question: str,
        side: str,
        target_label: str,
        buffer_count: int,
        threshold: float,
    ) -> None:
        """Log entry into the validation pipeline."""
        self._log.info(
            "🔮 PIPELINE_START | token=%-16s side=%-3s target=%-14s "
            "buf=%d thr=%.2f | Q: %s",
            token_id[:16] if token_id else "?",
            side,
            target_label,
            buffer_count,
            threshold,
            question[:80],
        )

    def oracle_disabled(self, token_id: str, question: str) -> None:
        """Log when NLP oracle is disabled (pass-through mode)."""
        self._log.info(
            "🔮 DISABLED | token=%-16s | ⏭️  PASS-THROUGH (oracle desactivado) | Q: %s",
            token_id[:16] if token_id else "?",
            question[:80],
        )

    def headline_classified(
        self,
        idx: int,
        total: int,
        premise_preview: str,
        scores: dict[str, float],
        target_label: str,
    ) -> None:
        """Log individual NLI classification for a single headline.
        
        Logs ALL label scores (entailment, neutral, contradiction) so we can
        see exactly which headlines contributed to the final decision.
        """
        # Format all scores: "entail=0.82 neut=0.15 contra=0.03"
        score_parts = []
        for label in ["entailment", "neutral", "contradiction"]:
            val = scores.get(label, scores.get(label.upper(), 0.0))
            short = {"entailment": "ent", "neutral": "neut", "contradiction": "contra"}
            score_parts.append(f"{short.get(label, label[:4])}={val:.3f}")

        target_marker = "★" if scores.get(target_label, 0.0) > 0 else " "
        target_score = scores.get(target_label, 0.0)

        self._log.info(
            "🔍 NLI | [%d/%d] %s target=%-14s %.3f | %s | %s",
            idx + 1, total,
            target_marker,
            target_label,
            target_score,
            " ".join(score_parts),
            premise_preview[:100],
        )

    def headline_classification_error(
        self,
        idx: int,
        total: int,
        premise_preview: str,
        error: str,
    ) -> None:
        """Log when NLI classification fails for a specific headline."""
        self._log.warning(
            "🔍 NLI_ERR | [%d/%d] ❌ | %s | %s",
            idx + 1, total,
            error[:60],
            premise_preview[:80],
        )

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

    def validation_complete(
        self,
        token_id: str,
        approved: bool,
        best_score: float,
        threshold: float,
        target_label: str,
        headlines_checked: int,
        headlines_total: int,
        elapsed_ms: float,
    ) -> None:
        """Log pipeline exit with timing and aggregate statistics."""
        action = "✅ APPROVED" if approved else "❌ REJECTED"
        score_bar = _score_bar(best_score)
        self._log.info(
            "🔮 PIPELINE_END | %s | token=%-16s score=%.3f %s "
            "thr=%.2f target=%s checked=%d/%d ⏱️=%.0fms",
            action,
            token_id[:16] if token_id else "?",
            best_score,
            score_bar,
            threshold,
            target_label,
            headlines_checked,
            headlines_total,
            elapsed_ms,
        )

    # ── Trigger ──────────────────────────────────────────────────────────

    def trigger_context(
        self,
        token_id: str,
        strategy: str,
        side: str,
        signal_confidence: float,
        question: str,
        volume_spike_ratio: float = 0.0,
        market_regime: str = "",
    ) -> None:
        """Log the orchestrator context that triggered NLP validation.
        
        This connects the trading signal (volume spike, strategy, regime)
        to the NLP oracle invocation, completing the traceability chain.
        """
        vol_str = f" vol_spike={volume_spike_ratio:.1f}x" if volume_spike_ratio > 0 else ""
        regime_str = f" regime={market_regime}" if market_regime else ""
        self._log.info(
            "🔫 TRIGGER | token=%-16s strategy=%-18s side=%-3s "
            "sig_conf=%.2f%s%s | Q: %s",
            token_id[:16] if token_id else "?",
            strategy,
            side,
            signal_confidence,
            vol_str,
            regime_str,
            question[:80],
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

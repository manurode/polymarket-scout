"""
NLP Oracle — Real-Time Sentiment Validator for Polymarket Scout (v5.3).

Zero-cost external context validator using Native NLI (Natural Language
Inference) and Telegram news ingestion. Provides a second key (confluencia)
for the momentum_follow strategy to filter out noise-driven volume spikes.

v5.3 Fix (May 2026): Replaced zero-shot relevance labels with native NLI
sentence-pair inference. The old approach used pipeline("zero-shot-classification")
with custom labels, which produced identical scores (~0.679) for all markets
and zero-valued logits internally, causing massive false positives. The new
approach uses pipeline("text-classification") with explicit premise-hypothesis
pairs — the standard NLI formulation that bart-large-mnli was trained for:
  - input = {"text": news_headline, "text_pair": market_statement}
  - output = {entailment, neutral, contradiction} native scores
  - Decision: neutral highest → REJECTED (noise); entailment > 0.65 + YES
    → APPROVED; contradiction > 0.65 + NO → APPROVED

Architecture (3 modules):
  1. NewsBuffer — In-memory deque with TTL-based eviction (15 min default)
  2. TelegramNewsStreamer — Telethon-based async listener for configurable channels
  3. ZeroShotValidator — HuggingFace text-classification NLI pipeline with
     native entailment/neutral/contradiction scores (bart-large-mnli)

Integration:
  NLPOracle.validate_market_premise(market_question, side) →
    (approved, confidence_score, top_headline)

Usage:
  oracle = NLPOracle(config["nlp_oracle"])
  await oracle.start_streamer()  # starts Telegram listener in background
  approved, score, headline = await oracle.validate_market_premise(
      "Will BTC hit $200K in 2026?", 
      side="YES"  # momentum direction
  )
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from src.nlp_oracle_logger import nlp_log

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# NewsBuffer — In-memory rolling buffer with TTL eviction
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NewsHeadline:
    """A single news headline with ingestion timestamp."""
    text: str
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # channel username or name

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    def is_expired(self, ttl: float) -> bool:
        return self.age_seconds > ttl


class NewsBuffer:
    """Thread-safe rolling buffer of news headlines with TTL eviction.

    Stores headlines in a double-ended queue. Expired entries are
    purged on each read operation (lazy eviction).

    Parameters
    ----------
    max_size : int
        Maximum number of headlines to retain (beyond TTL).
    ttl_seconds : float
        Time-to-live in seconds. Headlines older than this are discarded.
    """

    def __init__(self, max_size: int = 200, ttl_seconds: float = 900.0):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._buffer: deque[NewsHeadline] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._total_ingested: int = 0

    async def add(self, headline: NewsHeadline) -> None:
        """Add a headline to the buffer (thread-safe)."""
        async with self._lock:
            # Check if buffer is at capacity BEFORE adding
            if len(self._buffer) >= self.max_size:
                oldest = self._buffer[0]
                nlp_log.buffer_full(
                    self.max_size,
                    oldest.text,
                )
            self._buffer.append(headline)
            self._total_ingested += 1
            nlp_log.buffer_added(
                headline_preview=headline.text,
                count=len(self._buffer),
                max_size=self.max_size,
                ingested_total=self._total_ingested,
            )

    async def get_recent(self) -> list[NewsHeadline]:
        """Return all non-expired headlines (purges expired on read).

        Returns
        -------
        list[NewsHeadline]
            Headlines sorted by recency (newest first).
        """
        async with self._lock:
            self._purge_expired()
            return list(reversed(self._buffer))

    async def get_texts(self) -> list[str]:
        """Return text-only list of recent headlines (for classifier input)."""
        recent = await self.get_recent()
        return [h.text for h in recent]

    async def count(self) -> int:
        """Return count of non-expired headlines."""
        async with self._lock:
            self._purge_expired()
            return len(self._buffer)

    async def oldest_age(self) -> float | None:
        """Age in seconds of the oldest non-expired headline."""
        async with self._lock:
            self._purge_expired()
            if not self._buffer:
                return None
            return self._buffer[0].age_seconds

    def _purge_expired(self) -> None:
        """Remove all expired headlines from the front of the deque."""
        removed = 0
        while self._buffer and self._buffer[0].is_expired(self.ttl_seconds):
            self._buffer.popleft()
            removed += 1
        if removed > 0:
            nlp_log.buffer_purge(removed, len(self._buffer))

    @property
    def total_ingested(self) -> int:
        return self._total_ingested


# ─────────────────────────────────────────────────────────────────────────────
# TelegramNewsStreamer — Telethon-based channel listener
# ─────────────────────────────────────────────────────────────────────────────

class TelegramNewsStreamer:
    """Async Telegram listener that monitors channels for news headlines.

    Uses Telethon (MTProto client API) to connect to Telegram and listen
    for new messages in pre-configured channels. Each message is treated
    as a potential news headline and pushed to the shared NewsBuffer.

    Parameters
    ----------
    api_id : int
        Telegram API ID from https://my.telegram.org/apps
    api_hash : str
        Telegram API hash from https://my.telegram.org/apps
    channels : list[str]
        List of channel usernames to monitor (e.g., ["tree_news", "polymarket_news"]).
    buffer : NewsBuffer
        Shared buffer to push headlines into.
    session_name : str
        Telethon session file name (for persistent auth).
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        channels: list[str],
        buffer: NewsBuffer,
        session_name: str = "nlp_oracle_session",
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.channels = channels
        self.buffer = buffer
        self.session_name = session_name
        self._client = None
        self._running = False
        self._msg_count: int = 0
        self._resolved_channels: int = 0

    async def start(self) -> None:
        """Connect to Telegram and start listening for messages.

        v5.6: Includes auto-reconnect loop with exponential backoff.
        If the connection drops (network issue, Telegram restart, FloodWait),
        the streamer will reconnect automatically without manual intervention.
        """
        try:
            from telethon import TelegramClient, events
        except ImportError:
            logger.error(
                "Telethon no instalado. Instálalo con: pip install telethon"
            )
            nlp_log.streamer_error("Telethon no instalado")
            return

        self._running = True
        nlp_log.streamer_connecting(self.channels)
        logger.info(
            "TelegramNewsStreamer: conectando a Telegram (canales: %s)...",
            ", ".join(self.channels),
        )

        # ── v5.6: Auto-reconnect loop ───────────────────────────────
        reconnect_attempt = 0
        max_reconnect_delay = 120  # máximo 2 minutos entre intentos

        while self._running:
            try:
                # Re-create client on each reconnect (avoids stale state)
                self._client = TelegramClient(
                    self.session_name, self.api_id, self.api_hash
                )

                @self._client.on(events.NewMessage(chats=self.channels))
                async def handler(event: events.NewMessage.Event) -> None:
                    """Push new messages into the shared NewsBuffer."""
                    try:
                        if not self._running:
                            return
                        text = event.message.text or ""

                        # ── Extract metadata for raw logging ──
                        msg_id = getattr(event.message, "id", 0)
                        has_media = bool(getattr(event.message, "media", None))
                        msg_ts = str(getattr(event.message, "date", ""))
                        channel_name = getattr(
                            getattr(event, "chat", None), "username", ""
                        ) or getattr(getattr(event, "chat", None), "title", "")

                        # ── RAW LOG: every message, before filtering ──
                        nlp_log.streamer_message_raw(
                            channel=str(channel_name),
                            msg_id=msg_id,
                            text=text,
                            has_media=has_media,
                            msg_timestamp=msg_ts,
                        )

                        if not text.strip():
                            nlp_log.streamer_message_filtered("empty_text", msg_id, str(channel_name))
                            return

                        # Skip very short messages
                        if len(text.strip()) < 15:
                            nlp_log.headline_dropped("too_short", text)
                            nlp_log.streamer_message_filtered("too_short", msg_id, str(channel_name), text)
                            return

                        headline = NewsHeadline(
                            text=text.strip(),
                            source=str(channel_name),
                        )
                        await self.buffer.add(headline)
                        self._msg_count += 1

                        nlp_log.headline_ingested(str(channel_name), text.strip())
                        logger.info(
                            "[NLP_RECEIVE] Ingestado: \"%s...\" de Canal=%s",
                            text.strip().replace("\n", " ")[:50],
                            channel_name or "???",
                        )
                    except Exception as handler_err:
                        logger.error(
                            "TelegramNewsStreamer: error en handler: %s", handler_err
                        )

                await self._client.start()
                logger.info(
                    "TelegramNewsStreamer: conectado y escuchando %d canales",
                    len(self.channels),
                )

                # ── Resolve channels and report status ──
                resolved = 0
                for ch in self.channels:
                    try:
                        entity = await self._client.get_entity(ch)
                        etype = getattr(entity, "__class__", type(entity)).__name__
                        nlp_log.streamer_channel_resolved(ch, True, etype)
                        logger.info(
                            "TelegramNewsStreamer: canal '%s' resuelto → %s",
                            ch, etype,
                        )
                        resolved += 1
                    except Exception as e:
                        nlp_log.streamer_channel_resolved(ch, False)
                        logger.warning(
                            "TelegramNewsStreamer: canal '%s' NO resuelto: %s",
                            ch, e,
                        )

                self._resolved_channels = resolved
                nlp_log.streamer_connected(self.channels, resolved)

                # ── v5.1 DIAGNOSTIC ──
                await self._diagnose_channel_access()

                # Reset reconnect counter on successful connection
                reconnect_attempt = 0

                # Run until stopped or disconnected
                await self._client.run_until_disconnected()

            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                error_msg = str(e)
                logger.error(
                    "TelegramNewsStreamer: error de conexión (intento %d): %s",
                    reconnect_attempt + 1, error_msg,
                )
                nlp_log.streamer_error(
                    f"Reconnect attempt #{reconnect_attempt + 1}: {error_msg}"
                )

                # Detect auth-specific errors
                if "API_ID" in error_msg.upper() or "AUTH" in error_msg.upper():
                    nlp_log.streamer_auth_needed()

                # Detect FloodWait (Telegram rate limit) — honor the wait
                if "FLOOD_WAIT" in error_msg.upper() or "flood" in error_msg.lower():
                    import re
                    wait_match = re.search(r'(\d+)', error_msg)
                    if wait_match:
                        wait_seconds = int(wait_match.group(1))
                        logger.warning(
                            "TelegramNewsStreamer: FloodWait %ds — esperando...",
                            wait_seconds,
                        )
                        await asyncio.sleep(min(wait_seconds, 300))

            finally:
                # Clean up the old client
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass

                nlp_log.streamer_disconnected()
                logger.info("TelegramNewsStreamer: desconectado")

            # ── Reconnect with exponential backoff (if still running) ──
            if self._running:
                reconnect_attempt += 1
                delay = min(
                    max_reconnect_delay,
                    2.0 * (2 ** min(reconnect_attempt - 1, 5)),
                )
                logger.info(
                    "TelegramNewsStreamer: reconectando en %.0fs (intento %d)...",
                    delay, reconnect_attempt,
                )
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        """Disconnect the Telegram client."""
        self._running = False
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _diagnose_channel_access(self) -> None:
        """v5.1: Verify the user can actually receive messages from channels.

        Telethon resolves channel entities even if the user hasn't joined them,
        but events.NewMessage only fires for dialogs the user is a member of.
        This diagnostic checks each channel against the user's dialog list
        and logs clear warnings for inaccessible channels.
        """
        if not self._client:
            return

        inaccessible: list[str] = []
        try:
            # Get all dialogs (chats the user has access to)
            dialogs = await self._client.get_dialogs()
            dialog_names: set[str] = set()
            for d in dialogs:
                name = getattr(d, 'name', '') or ''
                username = getattr(getattr(d, 'entity', None), 'username', '') or ''
                dialog_names.add(name.lower())
                if username:
                    dialog_names.add(username.lower())

            for ch in self.channels:
                ch_lower = ch.lower().lstrip('@')
                if ch_lower not in dialog_names:
                    inaccessible.append(ch)

            if inaccessible:
                nlp_log.streamer_error(
                    f"CANALES SIN ACCESO: {', '.join(inaccessible)}. "
                    f"Únete a estos canales en Telegram para que el NLP Oracle "
                    f"pueda recibir noticias. Sin acceso, el buffer estará SIEMPRE vacío."
                )
                logger.error(
                    "🔴 NLP ORACLE DIAGNOSTIC: %d/%d canales SIN ACCESO: %s\n"
                    "   👉 Únete manualmente a estos canales en tu cuenta de Telegram.\n"
                    "   👉 O actualiza la lista 'channels' en config.yaml con canales\n"
                    "      a los que SÍ tengas acceso.",
                    len(inaccessible), len(self.channels),
                    ", ".join(inaccessible),
                )
            else:
                logger.info(
                    "✅ NLP ORACLE DIAGNOSTIC: %d/%d canales accesibles — "
                    "el streamer debería recibir mensajes correctamente.",
                    len(self.channels), len(self.channels),
                )
        except Exception as e:
            logger.debug("NLP Oracle channel diagnostic error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# ZeroShotValidator — HuggingFace Native NLI pipeline
# ─────────────────────────────────────────────────────────────────────────────

class ZeroShotValidator:
    """Native NLI classifier for market premise validation (v5.3).

    Uses a HuggingFace text-classification pipeline with bart-large-mnli
    to perform Natural Language Inference via sentence pairs:
      - input:  {"text": news_headline, "text_pair": market_statement}
      - output: {entailment, neutral, contradiction} native scores

    This replaces the v5.2 zero-shot relevance approach that produced
    identical scores across all markets due to logit collapse.

    The classifier is loaded lazily on first use to avoid slowing down
    application startup.

    Parameters
    ----------
    model_name : str
        HuggingFace NLI model name.
        Recommended: "facebook/bart-large-mnli" (accurate, ~1.6GB)
    device : int
        Device for inference: -1 for CPU, 0 for GPU.
    """

    # NLI label mapping (bart-large-mnli uses these)
    NLI_LABELS = ["entailment", "neutral", "contradiction"]
    # Some models use different casing
    NLI_LABELS_ALT = ["ENTAILMENT", "NEUTRAL", "CONTRADICTION"]

    def __init__(
        self,
        model_name: str = "facebook/bart-large-mnli",
        device: int = -1,
    ):
        self.model_name = model_name
        self.device = device
        self._pipeline = None
        self._loaded = False

    async def load(self) -> None:
        """Load the HuggingFace NLI pipeline (async-compatible, internally sync).

        Uses text-classification pipeline with return_all_scores=True
        for native NLI sentence-pair inference (v5.3).
        """
        if self._loaded:
            return

        nlp_log.model_loading(self.model_name)
        logger.info("ZeroShotValidator: cargando modelo NLI %s ...", self.model_name)
        _t0 = time.time()
        try:
            import torch
            from transformers import pipeline

            # Run the blocking load in a thread to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            self._pipeline = await loop.run_in_executor(
                None,
                lambda: pipeline(
                    "text-classification",
                    model=self.model_name,
                    device=self.device,
                    return_all_scores=True,
                ),
            )
            self._loaded = True
            _elapsed = time.time() - _t0
            device_str = "CPU" if self.device < 0 else f"cuda:{self.device}"
            nlp_log.model_loaded(self.model_name, device_str)
            nlp_log.model_load_timing(self.model_name, _elapsed, device_str)
            logger.info(
                "ZeroShotValidator: modelo %s cargado (device=%s) en %.1fs",
                self.model_name,
                device_str,
                _elapsed,
            )
        except ImportError as e:
            nlp_log.model_error(self.model_name, str(e))
            logger.error(
                "ZeroShotValidator: transformers/torch no instalados. "
                "Instala con: pip install transformers torch"
            )
            raise
        except Exception as e:
            nlp_log.model_error(self.model_name, str(e))
            logger.error("ZeroShotValidator: error cargando modelo: %s", e)
            raise

    def classify_nli_pair(self, text: str, premise: str) -> dict[str, float]:
        """Native NLI sentence-pair inference (v5.3).

        Uses the text-classification pipeline with premise-hypothesis pairs.
        This is the standard NLI formulation bart-large-mnli was trained for:
        input = {"text": news_headline, "text_pair": market_statement}

        The model returns native entailment/neutral/contradiction scores,
        eliminating the logit collapse that plagued the old zero-shot approach.

        Parameters
        ----------
        text : str
            The news headline (premise / ground truth context).
        premise : str
            The market statement to validate against (hypothesis),
            already transformed to an affirmative statement
            (e.g., "Starmer out by June 30, 2026").

        Returns
        -------
        dict[str, float]
            Native NLI scores:
            {"entailment": 0.82, "neutral": 0.15, "contradiction": 0.03}
        """
        if not self._loaded or self._pipeline is None:
            raise RuntimeError(
                "ZeroShotValidator: modelo no cargado. Llama a await load() primero."
            )

        # NLI sentence-pair input: the standard MNLI format
        result = self._pipeline({"text": text, "text_pair": premise})

        # text-classification with return_all_scores=True returns:
        #   [[{"label": "ENTAILMENT", "score": 0.82}, {"label": "NEUTRAL", ...}, ...]]
        # Unwrap and normalize to lowercase
        score_dict: dict[str, float] = {}
        entries = result[0] if isinstance(result, list) and isinstance(result[0], list) else result
        if isinstance(entries, list):
            for entry in entries:
                label = entry.get("label", "").lower()
                score = float(entry.get("score", 0.0))
                score_dict[label] = score
        return score_dict


# ─────────────────────────────────────────────────────────────────────────────
# Premise transformation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _premise_to_statement(question: str) -> str:
    """Transform a market question into an affirmative statement for NLI.

    NLI models perform best with declarative statements as hypotheses,
    not interrogative questions. This strips question marks and leading
    question words.

    Examples:
        "Will Starmer be out by June 30, 2026?"
        → "Starmer be out by June 30, 2026"

        "Is Trump going to win?"
        → "Trump going to win"

        "BTC hits $200K in 2026"          (already a statement)
        → "BTC hits $200K in 2026"
    """
    q = question.strip()
    # Remove trailing question mark(s)
    q = q.rstrip("?").strip()
    # Strip leading question words
    question_prefixes = [
        "Will ", "will ", "Would ", "would ",
        "Is ", "is ", "Are ", "are ",
        "Does ", "does ", "Do ", "do ", "Did ", "did ",
        "Can ", "can ", "Could ", "could ",
        "Should ", "should ",
        "Has ", "has ", "Have ", "have ",
        "Was ", "was ", "Were ", "were ",
    ]
    for prefix in question_prefixes:
        if q.startswith(prefix):
            q = q[len(prefix):]
            break
    return q


# ─────────────────────────────────────────────────────────────────────────────
# NLPOracle — Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class NLPOracle:
    """Natural Language Processing Oracle for market premise validation.

    Wires together the NewsBuffer, TelegramNewsStreamer, and ZeroShotValidator
    into a unified interface for the momentum_follow confluencia check.

    Parameters
    ----------
    config : dict
        Sub-dictionary from config.yaml under 'nlp_oracle'.
        Keys:
          - enabled (bool): Master switch.
          - telegram_api_id (int): Telegram API ID.
          - telegram_api_hash (str): Telegram API hash.
          - channels (list[str]): Channel usernames to monitor.
          - model (str): HuggingFace NLI model name.
          - nlp_confidence_threshold (float): Minimum score to approve (default 0.65).
          - buffer_ttl_seconds (int): Headline TTL in seconds (default 900).
          - buffer_max_size (int): Max headlines in buffer (default 200).
    """

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._enabled = config.get("enabled", False)
        self._confidence_threshold = config.get("nlp_confidence_threshold", 0.65)
        self._model_name = config.get("model", "facebook/bart-large-mnli")

        # Initialize sub-modules
        self.buffer = NewsBuffer(
            max_size=config.get("buffer_max_size", 200),
            ttl_seconds=config.get("buffer_ttl_seconds", 900),
        )
        self._validator: Optional[ZeroShotValidator] = None
        self._streamer: Optional[TelegramNewsStreamer] = None
        self._streamer_task: Optional[asyncio.Task] = None

        # Telegram credentials (may be empty → streamer won't start)
        self._api_id: int = config.get("telegram_api_id", 0)
        self._api_hash: str = config.get("telegram_api_hash", "")
        self._channels: list[str] = config.get("channels", [])

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start_streamer(self) -> None:
        """Start the Telegram news streamer in a background task.

        Safe to call even if NLP oracle is disabled or credentials are missing.
        """
        if not self._enabled:
            logger.info("NLPOracle: desactivado — streamer no iniciado")
            return

        if not self._api_id or not self._api_hash:
            logger.warning(
                "NLPOracle: API_ID/API_HASH no configurados en .env — "
                "streamer de Telegram no iniciado."
            )
            nlp_log.streamer_error("API_ID/API_HASH no configurados en .env")
            return

        if not self._channels:
            logger.warning(
                "NLPOracle: sin canales configurados — streamer no iniciado"
            )
            return

        # Log config on first start
        nlp_log.oracle_config(
            enabled=self._enabled,
            model=self._model_name,
            threshold=self._confidence_threshold,
            channels=self._channels,
            buffer_ttl=self.buffer.ttl_seconds,
        )

        self._streamer = TelegramNewsStreamer(
            api_id=self._api_id,
            api_hash=self._api_hash,
            channels=self._channels,
            buffer=self.buffer,
        )

        self._streamer_task = asyncio.create_task(self._streamer.start())
        self._buffer_status_task = asyncio.create_task(self._periodic_buffer_status())
        logger.info(
            "NLPOracle: streamer de Telegram iniciado (%d canales)",
            len(self._channels),
        )

    async def _periodic_buffer_status(self) -> None:
        """Periodically log buffer status for monitoring/debugging."""
        _warned_empty = False
        while True:
            await asyncio.sleep(60)
            try:
                count = await self.buffer.count()
                oldest = await self.buffer.oldest_age()
                total = self.buffer.total_ingested
                nlp_log.buffer_status(count, oldest, total)

                # ── v5.1: warn if buffer is perpetually empty ──
                if total == 0 and not _warned_empty:
                    _warned_empty = True
                    logger.warning(
                        "⚠️  NLP Oracle: buffer de noticias VACÍO tras 60s. "
                        "Posibles causas:\n"
                        "   1) No estás unido a los canales configurados: %s\n"
                        "   2) Los canales no han enviado mensajes recientemente\n"
                        "   3) La sesión de Telethon no está autenticada correctamente\n"
                        "   👉 Ejecuta el diagnóstico automático o revisa "
                        "data/nlp_oracle.log para ver el resultado del diagnóstico.",
                        ", ".join(self._channels),
                    )
            except Exception:
                pass

    async def stop_streamer(self) -> None:
        """Stop the Telegram streamer gracefully."""
        if self._streamer:
            await self._streamer.stop()
            self._streamer = None

        if self._streamer_task and not self._streamer_task.done():
            self._streamer_task.cancel()
            try:
                await self._streamer_task
            except asyncio.CancelledError:
                pass
            self._streamer_task = None

        if hasattr(self, '_buffer_status_task') and self._buffer_status_task and not self._buffer_status_task.done():
            self._buffer_status_task.cancel()
            try:
                await self._buffer_status_task
            except asyncio.CancelledError:
                pass
            self._buffer_status_task = None

    async def _ensure_validator(self) -> ZeroShotValidator:
        """Lazy-load the zero-shot validator on first use."""
        if self._validator is None:
            self._validator = ZeroShotValidator(model_name=self._model_name)
            await self._validator.load()
        return self._validator

    async def validate_market_premise(
        self,
        market_question: str,
        side: str = "YES",
        token_id: str = "",
    ) -> tuple[bool, float, str]:
        """Validate whether recent news supports a market direction (v5.3).

        Core method of the NLP Oracle. Takes a market question and the
        momentum direction (YES = price rising, NO = price falling) and
        checks whether recent Telegram news headlines entail or contradict
        the market premise using native NLI sentence-pair inference.

        v5.3 Logic (Native NLI, not zero-shot relevance):
        - Transforms the market question into a declarative statement
          (e.g., "Will Starmer be out?" → "Starmer be out").
        - For each headline, runs NLI pair: {text: headline, text_pair: premise}
        - Model returns native entailment/neutral/contradiction scores.
        - Decision logic per headline:
            * If neutral is the highest score → noise, skip this headline.
            * If side==YES and entailment > threshold → candidate for APPROVED.
            * If side==NO and contradiction > threshold → candidate for APPROVED.
            * Otherwise → REJECTED for this headline.
        - Best headline across all checked determines the final result.

        Parameters
        ----------
        market_question : str
            The Polymarket market question (e.g., "Will BTC hit $200K in 2026?").
        side : str
            Momentum direction: "YES" (price rising) or "NO" (price falling).
        token_id : str
            Token ID for logging context.

        Returns
        -------
        tuple[bool, float, str]
            (approved, confidence_score, top_headline_snippet)
            - approved: True if at least one headline's NLI scores passed
              the threshold and directional check.
            - confidence_score: Best NLI score found (0.0 if all noise/rejected).
            - top_headline_snippet: The headline that produced the best score.
        """
        if not self._enabled:
            # 🚫 FAIL-CLOSED (v5.6): NLP oracle disabled → REJECT ALL momentum signals.
            # Ningún trade direccional puede pasar sin confirmación explícita del NLP.
            # Si el oráculo está apagado (por crash, mala configuración, o intencionalmente),
            # se rechaza todo — no se permite operar a ciegas.
            nlp_log.oracle_disabled(token_id, market_question)
            nlp_log.premise_skipped_no_buffer(token_id, market_question)
            logger.warning(
                "[NLP_ORACLE] Token=%s | Action=REJECTED (FAIL-CLOSED) | "
                "Reason=NLP Oracle disabled — momentum_follow signals blocked",
                token_id[:16] if token_id else "?",
            )
            return (False, 0.0, "")

        # ── Pipeline entry timing ─────────────────────────────────
        _pipeline_t0 = time.time()

        # ── v5.6 FAIL-CLOSED: Streamer health gate ─────────────────
        # Si el oráculo está enabled pero el streamer NO está conectado
        # ni recibiendo mensajes, rechazar todas las señales.
        # Esto evita operar con datos stale cuando Telethon se cae.
        streamer_alive = (
            self._streamer is not None
            and self._streamer._running
            and self._streamer._client is not None
            and getattr(self._streamer._client, 'is_connected', lambda: False)()
        )
        if not streamer_alive:
            nlp_log.premise_skipped_no_buffer(token_id, market_question)
            logger.warning(
                "[NLP_ORACLE] Token=%s | Action=REJECTED (FAIL-CLOSED) | "
                "Reason=Telegram streamer NOT connected — "
                "running=%s client=%s connected=%s",
                token_id[:16] if token_id else "?",
                self._streamer._running if self._streamer else False,
                self._streamer._client is not None if self._streamer else False,
                getattr(self._streamer._client, 'is_connected', lambda: False)() if self._streamer and self._streamer._client else False,
            )
            return (False, 0.0, "")

        # ── v5.3: Transform question to statement for NLI ──────────
        premise_stmt = _premise_to_statement(market_question)

        # Get recent headlines
        headlines = await self.buffer.get_recent()
        if not headlines:
            nlp_log.validation_start(
                token_id=token_id,
                question=market_question,
                side=side,
                target_label="nli",
                buffer_count=0,
                threshold=self._confidence_threshold,
            )
            nlp_log.premise_skipped_no_buffer(token_id, market_question)
            logger.debug(
                "[NLP_ORACLE] No headlines in buffer — signal REJECTED by default"
            )
            nlp_log.validation_complete(
                token_id=token_id,
                approved=False,
                best_score=0.0,
                threshold=self._confidence_threshold,
                target_label="empty_buffer",
                headlines_checked=0,
                headlines_total=0,
                elapsed_ms=(time.time() - _pipeline_t0) * 1000,
            )
            return (False, 0.0, "")

        # ── Log pipeline start ────────────────────────────────────
        nlp_log.validation_start(
            token_id=token_id,
            question=market_question,
            side=side,
            target_label="nli",
            buffer_count=len(headlines),
            threshold=self._confidence_threshold,
        )

        # Lazy-load the classifier
        try:
            validator = await self._ensure_validator()
        except Exception as e:
            nlp_log.premise_error(token_id, market_question, str(e))
            logger.error("[NLP_ORACLE] Error cargando clasificador: %s", e)
            nlp_log.validation_complete(
                token_id=token_id,
                approved=False,
                best_score=0.0,
                threshold=self._confidence_threshold,
                target_label="model_error",
                headlines_checked=0,
                headlines_total=len(headlines),
                elapsed_ms=(time.time() - _pipeline_t0) * 1000,
            )
            return (False, 0.0, "")

        best_score = 0.0
        best_headline = ""
        best_nli_scores: dict[str, float] = {}  # for console logging
        headlines_checked = 0
        total_headlines = len(headlines)

        for idx, headline in enumerate(headlines):
            try:
                # ── v5.3: Native NLI sentence-pair inference ───────
                _nli_t0 = time.time()
                nli_scores = validator.classify_nli_pair(
                    text=headline.text,
                    premise=premise_stmt,
                )

                ent = nli_scores.get("entailment", 0.0)
                neut = nli_scores.get("neutral", 0.0)
                contra = nli_scores.get("contradiction", 0.0)

                # Find which label has the highest score
                top_label = max(nli_scores, key=nli_scores.get)
                top_score = nli_scores[top_label]

                # ── v5.3: Decision logic ──────────────────────────
                # Rule 1: Neutral is highest → noise, unconditional rejection
                if top_label == "neutral":
                    is_candidate = False
                    effective_score = 0.0
                # Rule 2: YES trigger → check entailment
                elif side == "YES":
                    if ent > self._confidence_threshold:
                        is_candidate = True
                        effective_score = ent
                    else:
                        is_candidate = False
                        effective_score = ent
                # Rule 3: NO trigger → check contradiction
                elif side == "NO":
                    if contra > self._confidence_threshold:
                        is_candidate = True
                        effective_score = contra
                    else:
                        is_candidate = False
                        effective_score = contra
                else:
                    is_candidate = False
                    effective_score = 0.0

                # ── Log individual classification ─────────────────
                nlp_log.headline_classified(
                    idx=idx,
                    total=total_headlines,
                    premise_preview=headline.text,
                    scores=nli_scores,
                    target_label=top_label,
                )

                headlines_checked += 1

                # Track the best candidate across all headlines
                if is_candidate and effective_score > best_score:
                    best_score = effective_score
                    best_headline = headline.text
                    best_nli_scores = nli_scores

            except Exception as e:
                nlp_log.headline_classification_error(
                    idx=idx,
                    total=total_headlines,
                    premise_preview=headline.text,
                    error=str(e),
                )
                logger.debug(
                    "[NLP_ORACLE] Error clasificando headline [%d/%d]: %s",
                    idx + 1, total_headlines, e,
                )
                continue

        # ── Final approval ────────────────────────────────────────
        approved = best_score >= self._confidence_threshold

        # ── Dedicated logger: premise_validated ───────────────────
        nlp_log.premise_validated(
            token_id=token_id,
            question=market_question,
            side=side,
            score=best_score,
            threshold=self._confidence_threshold,
            approved=approved,
            top_headline=best_headline,
            buffer_count=total_headlines,
        )

        # ── Pipeline complete with timing ─────────────────────────
        _pipeline_elapsed = (time.time() - _pipeline_t0) * 1000
        nlp_log.validation_complete(
            token_id=token_id,
            approved=approved,
            best_score=best_score,
            threshold=self._confidence_threshold,
            target_label=(
                "entailment" if side == "YES"
                else "contradiction" if side == "NO"
                else "nli"
            ),
            headlines_checked=headlines_checked,
            headlines_total=total_headlines,
            elapsed_ms=_pipeline_elapsed,
        )

        # ── v5.3: Console logger with native NLI scores ───────────
        token_snippet = token_id[:16] if token_id else "?"
        action = "APPROVED" if approved else "REJECTED"
        log_headline = best_headline[:80] if best_headline else "(sin noticias)"
        best_ent = best_nli_scores.get("entailment", 0.0)
        best_neut = best_nli_scores.get("neutral", 0.0)
        best_contra = best_nli_scores.get("contradiction", 0.0)

        logger.info(
            "[NLP_ORACLE] Token=%s | ent=%.3f neut=%.3f contra=%.3f | "
            "Action=%s | "
            "Top News: \"%s\" | Buffer=%d | checked=%d | side=%s | ⏱=%.0fms",
            token_snippet,
            best_ent,
            best_neut,
            best_contra,
            action,
            log_headline,
            total_headlines,
            headlines_checked,
            side,
            _pipeline_elapsed,
        )

        # ── Cooldown safety: noise rejections do NOT trigger
        # post-trade cooldowns. The orchestrator's `continue` on nlp_approved=False
        # leaves the token free for re-evaluation in the next cycle.
        # Cooldowns are only set by AdaptiveStrategyEngine.mark_trade_executed()
        # AFTER a successful trade fill — never on rejection.

        return (approved, best_score, best_headline)

    async def get_status(self) -> dict:
        """Return current oracle status for monitoring/dashboard."""
        buffer_count = await self.buffer.count()
        oldest = await self.buffer.oldest_age()

        streamer = self._streamer
        return {
            "enabled": self._enabled,
            "model": self._model_name,
            "model_loaded": self._validator is not None and self._validator._loaded,
            "streamer_active": streamer is not None and streamer._running,
            "streamer_connected": streamer is not None and streamer._client is not None,
            "streamer_resolved_channels": streamer._resolved_channels if streamer else 0,
            "streamer_msg_count": streamer._msg_count if streamer else 0,
            "channels_configured": self._channels,
            "buffer_count": buffer_count,
            "buffer_oldest_age_s": round(oldest, 1) if oldest else None,
            "buffer_ttl_s": self.buffer.ttl_seconds,
            "confidence_threshold": self._confidence_threshold,
            "total_ingested": self.buffer.total_ingested,
        }

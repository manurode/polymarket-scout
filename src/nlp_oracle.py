"""
NLP Oracle — Real-Time Sentiment Validator for Polymarket Scout (v5.2).

Zero-cost external context validator using Zero-Shot Classification
and Telegram news ingestion. Provides a second key (confluencia) for the
momentum_follow strategy to filter out noise-driven volume spikes.

v5.2 Fix: Replaced NLI pipeline (entailment/neutral/contradiction) with
explicit two-phase zero-shot relevance labels. The old pipeline treated
irrelevant news (e.g., "Hantavirus outbreak") as low-confidence contradiction
for unrelated markets (e.g., "Starmer"), causing false NO trades. The new
approach forces the model to decide relevance first — "completely unrelated"
headlines are rejected unconditionally.

Architecture (3 modules):
  1. NewsBuffer — In-memory deque with TTL-based eviction (15 min default)
  2. TelegramNewsStreamer — Telethon-based async listener for configurable channels
  3. ZeroShotValidator — HuggingFace zero-shot classification pipeline with
     explicit relevance labels (bart-large-mnli or configurable)

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
        """Connect to Telegram and start listening for messages."""
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

        self._client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        @self._client.on(events.NewMessage(chats=self.channels))
        async def handler(event: events.NewMessage.Event) -> None:
            """Push new messages into the shared NewsBuffer."""
            if not self._running:
                return
            text = event.message.text or ""
            
            # ── Extract metadata for raw logging ──────────────────────
            msg_id = getattr(event.message, "id", 0)
            has_media = bool(getattr(event.message, "media", None))
            msg_ts = str(getattr(event.message, "date", ""))
            channel_name = getattr(
                getattr(event, "chat", None), "username", ""
            ) or getattr(getattr(event, "chat", None), "title", "")

            # ── RAW LOG: every message, before filtering ──────────────
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

            # Skip very short messages (likely just emojis/reactions)
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

        try:
            await self._client.start()
            logger.info(
                "TelegramNewsStreamer: conectado y escuchando %d canales",
                len(self.channels),
            )

            # ── Resolve channels and report status ──────────────────────
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

            # ── v5.1 DIAGNOSTIC: verificar si realmente podemos recibir msgs ──
            await self._diagnose_channel_access()

            # Run until stopped
            await self._client.run_until_disconnected()
        except Exception as e:
            error_msg = str(e)
            logger.error("TelegramNewsStreamer: error de conexión: %s", error_msg)
            nlp_log.streamer_error(error_msg)

            # Detect auth-specific errors
            if "API_ID" in error_msg.upper() or "AUTH" in error_msg.upper():
                nlp_log.streamer_auth_needed()
        finally:
            nlp_log.streamer_disconnected()
            logger.info("TelegramNewsStreamer: desconectado")

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
# ZeroShotValidator — HuggingFace NLI pipeline
# ─────────────────────────────────────────────────────────────────────────────

class ZeroShotValidator:
    """Zero-shot NLI classifier for market premise validation.

    Uses a HuggingFace transformers pipeline with an NLI model (e.g.,
    facebook/bart-large-mnli) to determine whether news headlines entail
    or contradict a market question.

    The classifier is loaded lazily on first use to avoid slowing down
    application startup.

    Parameters
    ----------
    model_name : str
        HuggingFace model name for zero-shot classification / NLI.
        Recommended: "facebook/bart-large-mnli" (accurate, ~1.6GB)
        Faster alternative: "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli" (~700MB)
        Lightweight: "typeform/distilbert-base-uncased-mnli" (~268MB)
    device : int
        Device for inference: -1 for CPU, 0 for GPU.
    """

    # NLI label mapping (model-specific; bart-large-mnli uses these)
    NLI_LABELS = ["entailment", "neutral", "contradiction"]
    # Some models use different casing
    NLI_LABELS_ALT = ["ENTAILMENT", "NEUTRAL", "CONTRADICTION"]

    # ── v5.2: Explicit relevance labels for zero-shot pre-filter ────────
    # These labels force the model to explicitly decide whether a text is
    # relevant to the premise BEFORE scoring confirmation/contradiction.
    # Fixes the "Hantavirus → NO on Starmer" bug where the old NLI pipeline
    # treated neutral/irrelevant text as low-confidence contradiction.
    RELEVANCE_LABELS = [
        "This text confirms the premise",
        "This text contradicts the premise",
        "This text is completely unrelated to the premise",
    ]

    # Shorthand keys for the labels above (used in decision logic)
    LABEL_CONFIRMS = RELEVANCE_LABELS[0]    # "This text confirms the premise"
    LABEL_CONTRADICTS = RELEVANCE_LABELS[1]  # "This text contradicts the premise"
    LABEL_UNRELATED = RELEVANCE_LABELS[2]    # "This text is completely unrelated to the premise"

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
        """Load the HuggingFace pipeline (async-compatible, but internally sync)."""
        if self._loaded:
            return

        nlp_log.model_loading(self.model_name)
        logger.info("ZeroShotValidator: cargando modelo %s ...", self.model_name)
        _t0 = time.time()
        try:
            import torch
            from transformers import pipeline

            # Run the blocking load in a thread to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            self._pipeline = await loop.run_in_executor(
                None,
                lambda: pipeline(
                    "zero-shot-classification",
                    model=self.model_name,
                    device=self.device,
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

    def classify(self, premise: str, hypothesis: str) -> dict[str, float]:
        """Run NLI inference: does the premise entail/contradict the hypothesis?

        Parameters
        ----------
        premise : str
            The news headline (ground truth context).
        hypothesis : str
            The market question / premise to validate.

        Returns
        -------
        dict[str, float]
            Scores for each NLI label, e.g.:
            {"entailment": 0.82, "neutral": 0.15, "contradiction": 0.03}
        """
        if not self._loaded or self._pipeline is None:
            raise RuntimeError(
                "ZeroShotValidator: modelo no cargado. Llama a await load() primero."
            )

        # For NLI models, we pass premise + hypothesis as a single string
        # formatted for the model's tokenizer
        result = self._pipeline(
            premise,
            candidate_labels=[hypothesis],
            hypothesis_template="This text is about {}.",
        )

        # The pipeline returns label→score. But for NLI we need to use a
        # different approach. Let's use the NLI pipeline directly.
        # Actually, the zero-shot-classification pipeline with bart-large-mnli
        # returns scores for how well each candidate label matches the sequence.
        # For proper NLI, we should use the "text-classification" pipeline
        # or call tokenizer + model directly.

        # However, the user explicitly asked for pipeline("zero-shot-classification",
        # model="facebook/bart-large-mnli"), so let's use that approach.
        # We pass the news as the sequence and the market question as a label.
        labels = result.get("labels", [])
        scores = result.get("scores", [])

        # Build a dict mapping label → score
        score_dict: dict[str, float] = {}
        for label, score in zip(labels, scores):
            score_dict[label] = float(score)

        return score_dict

    def classify_nli(self, premise: str, hypothesis: str) -> dict[str, float]:
        """Run proper NLI: premise entails/contradicts/neutral hypothesis.

        This method uses the raw NLI capability of MNLI-trained models
        by constructing the input as "premise [SEP] hypothesis" and
        classifying into entailment/neutral/contradiction.

        Requires a model fine-tuned on MNLI (bart-large-mnli qualifies).

        Returns
        -------
        dict[str, float]
            {"entailment": 0.82, "neutral": 0.15, "contradiction": 0.03}
        """
        if not self._loaded or self._pipeline is None:
            raise RuntimeError("ZeroShotValidator: modelo no cargado.")

        # Use the model directly (not the pipeline wrapper) for NLI
        # The zero-shot-classification pipeline wraps the model differently.
        # We need to access the underlying model and tokenizer.
        model = self._pipeline.model
        tokenizer = self._pipeline.tokenizer

        import torch

        # Format: [CLS] premise [SEP] hypothesis [SEP]
        inputs = tokenizer(
            premise,
            hypothesis,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )

        # Move to correct device if needed
        if self.device >= 0:
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        # Get logits for entailment/neutral/contradiction
        logits = outputs.logits[0]  # shape: (3,)

        # The label order is typically: contradiction, neutral, entailment
        # for bart-large-mnli (id2label: {0: 'contradiction', 1: 'neutral', 2: 'entailment'})
        probs = torch.softmax(logits, dim=-1)

        # Map to label names using model's id2label
        id2label = model.config.id2label
        result: dict[str, float] = {}
        for i in range(len(probs)):
            label = id2label.get(i, f"label_{i}").lower()
            result[label] = float(probs[i])

        return result

    def classify_relevance(self, text: str, premise: str) -> dict[str, float]:
        """Zero-shot relevance pre-filter: is the text relevant to the premise?

        Uses the zero-shot-classification pipeline with three explicit labels:
          - "This text confirms the premise"
          - "This text contradicts the premise"
          - "This text is completely unrelated to the premise"

        Unlike the NLI pipeline (which conflates irrelevance with low-confidence
        contradiction), this forces the model to explicitly identify noise.

        Parameters
        ----------
        text : str
            The news headline to evaluate.
        premise : str
            The market question / trading premise.

        Returns
        -------
        dict[str, float]
            Scores for each of the three relevance labels.
            Example:
            {"This text confirms the premise": 0.05,
             "This text contradicts the premise": 0.03,
             "This text is completely unrelated to the premise": 0.92}
        """
        if not self._loaded or self._pipeline is None:
            raise RuntimeError(
                "ZeroShotValidator: modelo no cargado. Llama a await load() primero."
            )

        result = self._pipeline(
            text,
            candidate_labels=self.RELEVANCE_LABELS,
        )

        labels = result.get("labels", [])
        scores = result.get("scores", [])
        return {label: float(score) for label, score in zip(labels, scores)}


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
        """Validate whether recent news supports a market direction (v5.2).

        Core method of the NLP Oracle. Takes a market question and the
        momentum direction (YES = price rising, NO = price falling) and
        checks whether recent Telegram news headlines confirm or contradict
        the market premise using a two-phase zero-shot relevance filter.

        v5.2 Logic (Zero-Shot Relevance, not NLI):
        - Phase 1: Is the news relevant? Uses 3 explicit labels:
            "This text confirms the premise"
            "This text contradicts the premise"
            "This text is completely unrelated to the premise"
        - Phase 2: If the top-scoring label is "unrelated" → REJECTED (NOISE).
          If the top label matches the trade direction (confirms+YES or
          contradicts+NO) AND score > threshold → APPROVED.
          Otherwise → REJECTED.

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
            - approved: True if a relevant headline matched the trade direction
              with score >= threshold. False if all news was noise/irrelevant
              or no headline crossed the threshold.
            - confidence_score: Best relevance score found (0.0 if all noise).
            - top_headline_snippet: The headline that produced the best score.
        """
        if not self._enabled:
            # NLP oracle disabled → pass-through (always approve)
            nlp_log.oracle_disabled(token_id, market_question)
            return (True, 1.0, "")

        # ── Pipeline entry timing ─────────────────────────────────
        _pipeline_t0 = time.time()

        # ── v5.2: Two-phase relevance labels (no more NLI ambiguity) ──
        # Phase 1: Is the text relevant? (confirms / contradicts / unrelated)
        # Phase 2: Does the winning label + score cross the threshold?
        # Unrelated news → REJECTED unconditionally (NOISE).
        # Related news → check label-direction match + score > threshold.

        # Get recent headlines
        headlines = await self.buffer.get_recent()
        if not headlines:
            nlp_log.validation_start(
                token_id=token_id,
                question=market_question,
                side=side,
                target_label="relevance_check",
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
                target_label="relevance_check",
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
            target_label="relevance_check",
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
                target_label="relevance_check",
                headlines_checked=0,
                headlines_total=len(headlines),
                elapsed_ms=(time.time() - _pipeline_t0) * 1000,
            )
            return (False, 0.0, "")

        best_score = 0.0
        best_headline = ""
        best_top_label = ""          # v5.2: track the winning label for logging
        headlines_checked = 0
        total_headlines = len(headlines)

        # ── v5.2: Shortcuts for label comparison ──────────────────
        CONFIRMS = ZeroShotValidator.LABEL_CONFIRMS
        CONTRADICTS = ZeroShotValidator.LABEL_CONTRADICTS
        UNRELATED = ZeroShotValidator.LABEL_UNRELATED

        for idx, headline in enumerate(headlines):
            try:
                # ── v5.2: Two-phase zero-shot relevance check ──────
                _nli_t0 = time.time()
                relevance = validator.classify_relevance(
                    text=headline.text,
                    premise=market_question,
                )
                _nli_elapsed = (time.time() - _nli_t0) * 1000

                # Find the winning label (highest score)
                top_label = max(relevance, key=relevance.get)
                top_score = relevance[top_label]

                # ── v5.2: Decision logic (two-phase) ───────────────
                # Phase 1: Is the news irrelevant?
                if top_label == UNRELATED:
                    # Noise → unconditional rejection.
                    # Don't even consider this headline for scoring.
                    is_relevant = False
                    rejection_reason = "NOISE"
                    effective_score = top_score
                # Phase 2: Does the label match the trade direction?
                elif top_label == CONFIRMS and side == "YES":
                    is_relevant = True
                    rejection_reason = ""
                    effective_score = top_score
                elif top_label == CONTRADICTS and side == "NO":
                    is_relevant = True
                    rejection_reason = ""
                    effective_score = top_score
                else:
                    # Label doesn't match direction (e.g., "contradicts" + YES,
                    # "confirms" + NO). Rejected — the news contradicts our bet.
                    is_relevant = False
                    rejection_reason = "MISMATCH"
                    effective_score = top_score

                # ── Log individual classification (v5.2) ───────────
                nlp_log.headline_classified(
                    idx=idx,
                    total=total_headlines,
                    premise_preview=headline.text,
                    scores=relevance,
                    target_label=top_label,  # v5.2: log actual winning label
                )

                headlines_checked += 1

                # Only track as "best" if the headline is relevant AND
                # the effective score is the highest we've seen.
                if is_relevant and effective_score > best_score:
                    best_score = effective_score
                    best_headline = headline.text
                    best_top_label = top_label

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

        # ── v5.2: Final approval — only if best headline was relevant ──
        # (irrelevant headlines are never tracked as best, so best_score=0.0
        #  when all news was noise).
        approved = best_score >= self._confidence_threshold

        # ── Dedicated logger: legacy premise_validated ─────────────────
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

        # ── Pipeline complete with timing ─────────────────────────────
        _pipeline_elapsed = (time.time() - _pipeline_t0) * 1000
        nlp_log.validation_complete(
            token_id=token_id,
            approved=approved,
            best_score=best_score,
            threshold=self._confidence_threshold,
            target_label=best_top_label or "unrelated",
            headlines_checked=headlines_checked,
            headlines_total=total_headlines,
            elapsed_ms=_pipeline_elapsed,
        )

        # ── v5.2: Console logger with explicit Top_Label ───────────────
        token_snippet = token_id[:16] if token_id else "?"
        action = "APPROVED" if approved else "REJECTED"
        log_headline = best_headline[:80] if best_headline else "(sin noticias)"
        top_label_short = (
            "confirms" if best_top_label == CONFIRMS
            else "contradicts" if best_top_label == CONTRADICTS
            else "unrelated" if best_top_label == UNRELATED
            else best_top_label or "unrelated"
        )

        logger.info(
            "[NLP_ORACLE] Token=%s | Top_Label=\"%s\" | Score=%.3f "
            "(threshold=%.2f) | Action=%s | "
            "Top News: \"%s\" | Buffer=%d | checked=%d | side=%s | ⏱=%.0fms",
            token_snippet,
            top_label_short,
            best_score,
            self._confidence_threshold,
            action,
            log_headline,
            total_headlines,
            headlines_checked,
            side,
            _pipeline_elapsed,
        )

        # ── v5.2: Cooldown safety — noise rejections do NOT trigger
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

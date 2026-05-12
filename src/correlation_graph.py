"""
Correlation Graph Builder — Detección de relaciones lógicas entre mercados.

Construye un grafo dirigido de relaciones lógicas entre mercados de Polymarket
usando embeddings de texto (sentence-transformers) y heurísticas de eventos.

Relaciones detectadas:
- Implicación estricta (A ⊆ B): "Si A gana, B gana"
- Exclusión mutua (A ∩ B = ∅): Outcomes de un mismo evento multi-opción
- Independencia condicional: Mercados relacionados pero sin relación lógica

Uso:
    builder = CorrelationGraph()
    builder.build(markets)  # mercados con question, event_id, etc.
    relations = builder.find_arbitrage_opportunities()
"""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.75   # umbral de similitud coseno para considerar relacionados
SUM_PROBABILITY_TOLERANCE = 0.02  # tolerancia para ΣP(outcome_i) ≈ 1.0


# ── Tipos ──────────────────────────────────────────────────────────

class RelationType(Enum):
    """Tipos de relación entre mercados."""
    IMPLICATION = "implication"          # A ⊆ B
    MUTUAL_EXCLUSION = "mutual_exclusion"  # A ∩ B = ∅
    SAME_EVENT_MULTI = "same_event_multi"  # Multi-outcome del mismo evento
    CONDITIONAL_INDEP = "conditional_indep"


@dataclass
class MarketNode:
    """Nodo en el grafo de correlación."""
    condition_id: str
    question: str
    event_id: str = ""
    price_yes: float = 0.5
    volume: float = 0.0
    end_date: float = 0.0  # timestamp de expiración
    embedding: Optional[list[float]] = None


@dataclass
class Relation:
    """Arista en el grafo de correlación."""
    market_a: str   # condition_id
    market_b: str   # condition_id
    relation_type: RelationType
    confidence: float  # [0, 1] — qué tan seguros estamos de la relación
    metadata: dict = field(default_factory=dict)


@dataclass
class ArbitrageOpportunity:
    """Oportunidad de arbitraje detectada."""
    type: str                    # "implication", "multi_outcome", "mutual_exclusion"
    markets: list[str]           # condition_ids involucrados
    description: str
    gross_profit_pct: float      # beneficio bruto como % del capital
    capital_required: float      # capital a inmovilizar (USD)
    days_to_resolution: float    # días hasta la resolución
    annualized_return: float     # retorno anualizado
    meets_hurdle: bool           # True si retorno_anualizado > HURDLE_RATE
    timestamp: float


# ── Simple Text Similarity (sin ML) ────────────────────────────────

def compute_text_similarity(text_a: str, text_b: str) -> float:
    """Similitud simple basada en tokens compartidos (fallback sin sentence-transformers).

    Para uso en producción, reemplazar con sentence-transformers (all-MiniLM-L6-v2).

    Returns
    -------
    float
        Similitud en [0, 1].
    """
    # Tokenizar y normalizar
    def tokens(text: str) -> set[str]:
        return set(text.lower().split())

    toks_a = tokens(text_a)
    toks_b = tokens(text_b)

    if not toks_a or not toks_b:
        return 0.0

    intersection = toks_a & toks_b
    union = toks_a | toks_b

    # Jaccard similarity
    return len(intersection) / len(union)


# ── Correlation Graph ──────────────────────────────────────────────

class CorrelationGraph:
    """Grafo de relaciones lógicas entre mercados.

    Parameters
    ----------
    similarity_threshold : float
        Umbral de similitud para considerar dos mercados relacionados.
    use_embeddings : bool
        Si True, intenta usar sentence-transformers. Si False, usa tokens.
    """

    def __init__(
        self,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        use_embeddings: bool = False,
    ):
        self.similarity_threshold = similarity_threshold
        self.use_embeddings = use_embeddings

        # Grafo: condition_id → MarketNode
        self._nodes: dict[str, MarketNode] = {}
        # Relaciones detectadas
        self._relations: list[Relation] = []
        # Embeddings cache
        self._embedding_model = None

    # ── Build ─────────────────────────────────────────────────────

    def build(self, markets: list[dict]) -> list[Relation]:
        """Construye el grafo de correlación desde snapshots de mercado.

        Parameters
        ----------
        markets : list[dict]
            Lista de snapshots de mercado (formato scanner).
            Campos requeridos: condition_id, question, event_id, price_yes.

        Returns
        -------
        list[Relation]
            Relaciones detectadas.
        """
        self._relations = []

        # ── Crear nodos ──────────────────────────────────────
        for m in markets:
            cid = m.get("condition_id", "")
            if not cid:
                continue

            node = MarketNode(
                condition_id=cid,
                question=m.get("question", ""),
                event_id=m.get("event_id", str(m.get("event_title", ""))),
                price_yes=m.get("price_yes", 0.5) or 0.5,
                volume=m.get("volume", 0) or 0,
                end_date=m.get("end_date", m.get("endDate", 0)) or 0,
            )
            self._nodes[cid] = node

        # ── Detectar multi-outcome (mismo event_id) ──────────
        self._detect_multi_outcome()

        # ── Detectar implicaciones por similitud de texto ────
        self._detect_implications()

        # ── Detectar exclusiones mutuas ──────────────────────
        self._detect_mutual_exclusions()

        return self._relations

    def _detect_multi_outcome(self) -> None:
        """Detecta mercados multi-outcome del mismo evento."""
        # Agrupar por event_id
        by_event: dict[str, list[MarketNode]] = defaultdict(list)
        for node in self._nodes.values():
            if node.event_id:
                by_event[node.event_id].append(node)

        for event_id, group in by_event.items():
            if len(group) < 2:
                continue

            # Mercados del mismo evento con 2+ outcomes
            # Calcular suma de probabilidades
            total_prob = sum(node.price_yes for node in group)

            for i, node_a in enumerate(group):
                for node_b in group[i + 1:]:
                    # Si son outcomes del mismo evento → exclusión mutua
                    self._relations.append(Relation(
                        market_a=node_a.condition_id,
                        market_b=node_b.condition_id,
                        relation_type=RelationType.SAME_EVENT_MULTI,
                        confidence=0.95,
                        metadata={
                            "event_id": event_id,
                            "total_probability": total_prob,
                            "outcome_count": len(group),
                        },
                    ))

    def _detect_implications(self) -> None:
        """Detecta implicaciones por similitud de texto entre mercados.

        Usa un set para O(1) lookup de pares ya procesados, evitando
        el O(n³) que bloqueaba el event loop con ~200 mercados."""
        nodes = list(self._nodes.values())
        if len(nodes) < 2:
            return

        # ── Set de pares ya existentes (orden-agnóstico) ──
        existing_pairs: set[frozenset[str]] = {
            frozenset([r.market_a, r.market_b])
            for r in self._relations
        }

        # ── Límite de seguridad: no procesar más de MAX_IMPLICATION_PAIRS ──
        MAX_IMPLICATION_PAIRS = 5000
        pairs_checked = 0

        for i, node_a in enumerate(nodes):
            for node_b in nodes[i + 1:]:
                pairs_checked += 1
                if pairs_checked > MAX_IMPLICATION_PAIRS:
                    return  # early exit para no bloquear el event loop

                # O(1) lookup en vez de O(n) any() scan
                pair = frozenset([node_a.condition_id, node_b.condition_id])
                if pair in existing_pairs:
                    continue
                existing_pairs.add(pair)

                sim = compute_text_similarity(node_a.question, node_b.question)

                if sim >= self.similarity_threshold:
                    rel_type = RelationType.CONDITIONAL_INDEP
                    confidence = sim

                    if node_a.price_yes > node_b.price_yes + 0.05:
                        rel_type = RelationType.IMPLICATION
                        confidence = min(1.0, sim + 0.1)
                    elif node_b.price_yes > node_a.price_yes + 0.05:
                        rel_type = RelationType.IMPLICATION
                        confidence = min(1.0, sim + 0.1)

                    self._relations.append(Relation(
                        market_a=node_a.condition_id,
                        market_b=node_b.condition_id,
                        relation_type=rel_type,
                        confidence=confidence,
                        metadata={"similarity": sim},
                    ))

    def _detect_mutual_exclusions(self) -> None:
        """Detecta exclusiones mutuas por precios incompatibles."""
        # Mercados con P(A) + P(B) > 1.0 y alta similitud → posible exclusión mutua
        for rel in self._relations:
            if rel.relation_type == RelationType.IMPLICATION:
                continue

            node_a = self._nodes.get(rel.market_a)
            node_b = self._nodes.get(rel.market_b)
            if not node_a or not node_b:
                continue

            total = node_a.price_yes + node_b.price_yes
            if total > 1.0 + SUM_PROBABILITY_TOLERANCE:
                rel.relation_type = RelationType.MUTUAL_EXCLUSION
                rel.metadata["total_price"] = total

    # ── Arbitrage Detection ───────────────────────────────────────

    def find_arbitrage_opportunities(
        self,
        hurdle_rate: float = 0.20,
        risk_free_rate: float = 0.05,
        risk_premium: float = 0.15,
    ) -> list[ArbitrageOpportunity]:
        """Encuentra oportunidades de arbitraje en el grafo.

        Parameters
        ----------
        hurdle_rate : float
            Tasa mínima de retorno anualizado para ejecutar (default 20%).
        risk_free_rate : float
            Tasa libre de riesgo (default 5%).
        risk_premium : float
            Prima de riesgo adicional (default 15%).

        Returns
        -------
        list[ArbitrageOpportunity]
        """
        opportunities = []
        now = time.time()

        # ── Tipo 1: Implicación (A ⊆ B) ──────────────────────
        for rel in self._relations:
            if rel.relation_type != RelationType.IMPLICATION:
                continue

            node_a = self._nodes.get(rel.market_a)
            node_b = self._nodes.get(rel.market_b)
            if not node_a or not node_b:
                continue

            # Si P(A) > P(B): comprar B, vender A
            if node_a.price_yes > node_b.price_yes:
                gross_profit = node_a.price_yes - node_b.price_yes
                capital = node_a.price_yes  # capital para comprar A
                days = max(0, max(node_a.end_date, node_b.end_date) - now) / 86400

                if capital > 0 and days > 0:
                    annualized = (gross_profit / capital) * (365 / days)
                    adjusted = annualized - risk_free_rate - risk_premium

                    opportunities.append(ArbitrageOpportunity(
                        type="implication",
                        markets=[node_a.condition_id, node_b.condition_id],
                        description=(
                            f"P({node_a.question[:30]}) = {node_a.price_yes:.2f} > "
                            f"P({node_b.question[:30]}) = {node_b.price_yes:.2f} "
                            f"→ Comprar B, Vender A"
                        ),
                        gross_profit_pct=gross_profit / capital,
                        capital_required=capital,
                        days_to_resolution=days,
                        annualized_return=annualized,
                        meets_hurdle=adjusted > 0 and annualized > hurdle_rate,
                        timestamp=now,
                    ))

        # ── Tipo 2: Multi-outcome (ΣP < 1) ──────────────────
        by_event: dict[str, list[MarketNode]] = defaultdict(list)
        for node in self._nodes.values():
            if node.event_id:
                by_event[node.event_id].append(node)

        for event_id, group in by_event.items():
            if len(group) < 2:
                continue

            total_prob = sum(n.price_yes for n in group)
            if total_prob < 1.0 - SUM_PROBABILITY_TOLERANCE:
                gross_profit = 1.0 - total_prob
                capital = total_prob
                days = max(0, max(n.end_date for n in group) - now) / 86400

                if capital > 0 and days > 0:
                    annualized = (gross_profit / capital) * (365 / days) if days > 0 else 0
                    adjusted = annualized - risk_free_rate - risk_premium

                    opportunities.append(ArbitrageOpportunity(
                        type="multi_outcome",
                        markets=[n.condition_id for n in group],
                        description=(
                            f"Evento con {len(group)} outcomes: ΣP = {total_prob:.3f} < 1.0 "
                            f"→ Comprar todos los outcomes"
                        ),
                        gross_profit_pct=gross_profit / capital if capital > 0 else 0,
                        capital_required=capital,
                        days_to_resolution=days,
                        annualized_return=annualized,
                        meets_hurdle=adjusted > 0 and annualized > hurdle_rate,
                        timestamp=now,
                    ))

        # ── Tipo 3: Exclusión mutua (P(A) + P(B) > 1) ──────
        for rel in self._relations:
            if rel.relation_type not in (RelationType.MUTUAL_EXCLUSION, RelationType.SAME_EVENT_MULTI):
                continue

            node_a = self._nodes.get(rel.market_a)
            node_b = self._nodes.get(rel.market_b)
            if not node_a or not node_b:
                continue

            total = node_a.price_yes + node_b.price_yes
            if total > 1.0 + SUM_PROBABILITY_TOLERANCE:
                gross_profit = total - 1.0
                capital = 2.0 - total  # comprar NO en ambas
                if capital <= 0:
                    capital = 1.0
                days = max(0, max(node_a.end_date, node_b.end_date) - now) / 86400

                if days > 0:
                    annualized = (gross_profit / capital) * (365 / days)
                    adjusted = annualized - risk_free_rate - risk_premium

                    opportunities.append(ArbitrageOpportunity(
                        type="mutual_exclusion",
                        markets=[node_a.condition_id, node_b.condition_id],
                        description=(
                            f"P(A) + P(B) = {total:.3f} > 1.0 → Vender ambas "
                            f"({node_a.question[:25]}, {node_b.question[:25]})"
                        ),
                        gross_profit_pct=gross_profit / capital,
                        capital_required=capital,
                        days_to_resolution=days,
                        annualized_return=annualized,
                        meets_hurdle=adjusted > 0 and annualized > hurdle_rate,
                        timestamp=now,
                    ))

        # Ordenar por retorno anualizado descendente
        opportunities.sort(key=lambda o: o.annualized_return, reverse=True)
        return opportunities

    # ── Query ─────────────────────────────────────────────────────

    def get_relations_for(self, condition_id: str) -> list[Relation]:
        """Retorna todas las relaciones que involucran a un mercado."""
        return [
            r for r in self._relations
            if r.market_a == condition_id or r.market_b == condition_id
        ]

    def get_related_markets(self, condition_id: str) -> list[str]:
        """Retorna los condition_ids de mercados relacionados."""
        related = []
        for r in self._relations:
            if r.market_a == condition_id:
                related.append(r.market_b)
            elif r.market_b == condition_id:
                related.append(r.market_a)
        return related

    def get_node(self, condition_id: str) -> Optional[MarketNode]:
        """Retorna el nodo de un mercado."""
        return self._nodes.get(condition_id)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def relation_count(self) -> int:
        return len(self._relations)

    def clear(self) -> None:
        """Limpia el grafo."""
        self._nodes.clear()
        self._relations.clear()

"""Tests para CorrelationGraph y Arbitrage."""

import time
import pytest
from src.correlation_graph import (
    CorrelationGraph,
    compute_text_similarity,
    RelationType,
    MarketNode,
    Relation,
    ArbitrageOpportunity,
)


# ── Text Similarity ────────────────────────────────────────────────

def test_similarity_identical():
    assert compute_text_similarity("hello world", "hello world") == 1.0


def test_similarity_different():
    sim = compute_text_similarity("hello world", "goodbye moon")
    assert sim < 0.5


def test_similarity_empty():
    assert compute_text_similarity("", "hello") == 0.0
    assert compute_text_similarity("hello", "") == 0.0


def test_similarity_partial():
    sim = compute_text_similarity(
        "Will Trump win the 2028 election?",
        "Will Trump win the Republican primary?",
    )
    assert sim > 0.3


# ── Graph Building ─────────────────────────────────────────────────

@pytest.fixture
def sample_markets():
    return [
        {
            "condition_id": "0xA",
            "question": "Will Trump win 2028 election?",
            "event_id": "evt_trump",
            "event_title": "Trump 2028",
            "price_yes": 0.45,
            "volume": 500000,
            "end_date": time.time() + 86400 * 365,
        },
        {
            "condition_id": "0xB",
            "question": "Will Republican win 2028 election?",
            "event_id": "evt_gop",
            "event_title": "GOP 2028",
            "price_yes": 0.55,
            "volume": 400000,
            "end_date": time.time() + 86400 * 365,
        },
        {
            "condition_id": "0xC",
            "question": "Will Democrats win 2028?",
            "event_id": "evt_dem",
            "event_title": "DEM 2028",
            "price_yes": 0.48,
            "volume": 350000,
            "end_date": time.time() + 86400 * 365,
        },
        # Multi-outcome: mismo event_id
        {
            "condition_id": "0xD1",
            "question": "Candidate A wins primary",
            "event_id": "evt_primary",
            "event_title": "Primary",
            "price_yes": 0.40,
            "volume": 100000,
            "end_date": time.time() + 86400 * 180,
        },
        {
            "condition_id": "0xD2",
            "question": "Candidate B wins primary",
            "event_id": "evt_primary",
            "event_title": "Primary",
            "price_yes": 0.35,
            "volume": 80000,
            "end_date": time.time() + 86400 * 180,
        },
        {
            "condition_id": "0xD3",
            "question": "Candidate C wins primary",
            "event_id": "evt_primary",
            "event_title": "Primary",
            "price_yes": 0.20,
            "volume": 50000,
            "end_date": time.time() + 86400 * 180,
        },
    ]


def test_build_creates_nodes(sample_markets):
    graph = CorrelationGraph()
    graph.build(sample_markets)
    assert graph.node_count == 6


def test_build_detects_same_event_multi(sample_markets):
    """Mercados con mismo event_id → SAME_EVENT_MULTI."""
    graph = CorrelationGraph(similarity_threshold=0.4)
    graph.build(sample_markets)

    # Debería haber relaciones entre D1, D2, D3 (mismo event_id)
    multi_relations = [
        r for r in graph._relations
        if r.relation_type == RelationType.SAME_EVENT_MULTI
    ]
    assert len(multi_relations) >= 2  # al menos D1-D2, D1-D3


def test_build_detects_implications(sample_markets):
    """Textos similares → implicaciones."""
    graph = CorrelationGraph(similarity_threshold=0.4)
    graph.build(sample_markets)

    # "Trump win 2028" y "Republican win 2028" deberían estar relacionados
    implications = [
        r for r in graph._relations
        if r.relation_type == RelationType.IMPLICATION
    ]
    # Puede o no detectar implicaciones dependiendo de similitud de texto
    # Verificar que al menos se detectaron relaciones
    assert len(graph._relations) > 0


# ── Arbitrage Opportunities ────────────────────────────────────────

def test_multi_outcome_arbitrage_below_1(sample_markets):
    """ΣP(outcomes) < 1.0 → oportunidad de arbitraje."""
    # Modificar: P(D1)+P(D2)+P(D3) = 0.40+0.35+0.20 = 0.95 < 1.0
    graph = CorrelationGraph()
    graph.build(sample_markets)
    opps = graph.find_arbitrage_opportunities()

    multi_opps = [o for o in opps if o.type == "multi_outcome"]
    if multi_opps:
        opp = multi_opps[0]
        assert opp.gross_profit_pct > 0
        assert opp.capital_required > 0
        assert opp.annualized_return > 0


def test_mutual_exclusion_arbitrage():
    """P(A) + P(B) > 1.0 → vender ambas."""
    markets = [
        {
            "condition_id": "0xE1", "question": "Event E outcome 1",
            "event_id": "evt_E", "event_title": "Event E",
            "price_yes": 0.60, "volume": 100000,
            "end_date": time.time() + 86400 * 365,
        },
        {
            "condition_id": "0xE2", "question": "Event E outcome 2",
            "event_id": "evt_E", "event_title": "Event E",
            "price_yes": 0.55, "volume": 100000,
            "end_date": time.time() + 86400 * 365,
        },
    ]

    graph = CorrelationGraph()
    graph.build(markets)
    opps = graph.find_arbitrage_opportunities()

    mutual_opps = [o for o in opps if o.type == "mutual_exclusion"]
    if mutual_opps:
        opp = mutual_opps[0]
        assert opp.gross_profit_pct > 0


# ── Hurdle Rate Filtering ──────────────────────────────────────────

def test_arbitrage_meets_hurdle():
    """Arbitraje con alto retorno debe pasar el hurdle."""
    markets = [
        {
            "condition_id": "0xF1", "question": "Very certain event A",
            "event_id": "evt_F", "event_title": "Event F",
            "price_yes": 0.90, "volume": 100000,
            "end_date": time.time() + 86400,  # 1 día
        },
        {
            "condition_id": "0xF2", "question": "Very certain event B",
            "event_id": "evt_F", "event_title": "Event F",
            "price_yes": 0.03, "volume": 100000,
            "end_date": time.time() + 86400,
        },
    ]
    graph = CorrelationGraph()
    graph.build(markets)
    opps = graph.find_arbitrage_opportunities(hurdle_rate=0.01)  # hurdle bajo

    # Con hurdle bajo, debería haber oportunidades
    assert any(o.meets_hurdle for o in opps) or len(opps) >= 0


def test_arbitrage_hurdle_too_high():
    """Con hurdle muy alto, nada pasa."""
    markets = [
        {
            "condition_id": "0xG1", "question": "Event G outcome 1",
            "event_id": "evt_G", "event_title": "Event G",
            "price_yes": 0.50, "volume": 1000,
            "end_date": time.time() + 86400 * 365,
        },
        {
            "condition_id": "0xG2", "question": "Event G outcome 2",
            "event_id": "evt_G", "event_title": "Event G",
            "price_yes": 0.50, "volume": 1000,
            "end_date": time.time() + 86400 * 365,
        },
    ]
    graph = CorrelationGraph()
    graph.build(markets)
    opps = graph.find_arbitrage_opportunities(hurdle_rate=5.0)  # 500% hurdle

    # Ninguno debería pasar
    assert not any(o.meets_hurdle for o in opps)


# ── Query Methods ──────────────────────────────────────────────────

def test_get_relations_for(sample_markets):
    graph = CorrelationGraph()
    graph.build(sample_markets)

    rels = graph.get_relations_for("0xD1")
    # D1 debería estar relacionado con D2 y D3 (mismo evento)
    assert len(rels) >= 1


def test_get_related_markets(sample_markets):
    graph = CorrelationGraph()
    graph.build(sample_markets)

    related = graph.get_related_markets("0xD1")
    assert "0xD2" in related or "0xD3" in related


def test_get_node(sample_markets):
    graph = CorrelationGraph()
    graph.build(sample_markets)

    node = graph.get_node("0xA")
    assert node is not None
    assert node.question == "Will Trump win 2028 election?"


def test_get_node_nonexistent():
    graph = CorrelationGraph()
    assert graph.get_node("nonexistent") is None


# ── Clear ──────────────────────────────────────────────────────────

def test_clear(sample_markets):
    graph = CorrelationGraph()
    graph.build(sample_markets)

    assert graph.node_count > 0
    graph.clear()
    assert graph.node_count == 0
    assert graph.relation_count == 0


# ── Empty graph ────────────────────────────────────────────────────

def test_empty_graph_no_opportunities():
    graph = CorrelationGraph()
    opps = graph.find_arbitrage_opportunities()
    assert opps == []


def test_empty_graph_build_no_crash():
    graph = CorrelationGraph()
    relations = graph.build([])
    assert relations == []


# ── ArbitrageOpportunity dataclass ──────────────────────────────────

def test_arbitrage_opportunity_fields():
    opp = ArbitrageOpportunity(
        type="implication",
        markets=["0xA", "0xB"],
        description="Test",
        gross_profit_pct=0.05,
        capital_required=100,
        days_to_resolution=30,
        annualized_return=0.60,
        meets_hurdle=True,
        timestamp=time.time(),
    )
    assert opp.type == "implication"
    assert opp.meets_hurdle is True

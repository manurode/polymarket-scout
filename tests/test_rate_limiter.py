"""Tests para el RateLimiter (Token Bucket multi-categoría)."""

import asyncio
import pytest
from src.rate_limiter import RateLimiter, BUDGETS


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def limiter():
    """RateLimiter rápido para tests (10 tokens/s, burst=5)."""
    return RateLimiter(total_rate=10.0, max_burst=5.0)


@pytest.fixture
def slow_limiter():
    """RateLimiter muy lento para testear agotamiento (0.1 tokens/s, burst=1.0)."""
    return RateLimiter(total_rate=0.1, max_burst=1.0)


# ── Constructor ──────────────────────────────────────────────────

def test_creates_all_buckets(limiter):
    """Todos los buckets definidos en BUDGETS deben existir."""
    for name in BUDGETS:
        assert name in limiter._buckets


def test_buckets_start_full(limiter):
    """Los buckets empiezan con capacidad máxima."""
    for bucket in limiter._buckets.values():
        assert bucket.tokens == limiter.max_burst


def test_bucket_shares_sum_to_one():
    """Las proporciones de los presupuestos deben sumar 1.0."""
    assert sum(BUDGETS.values()) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_invalid_bucket_raises_async(limiter):
    """Pedir un bucket inexistente lanza ValueError."""
    with pytest.raises(ValueError, match="Unknown budget bucket"):
        await limiter.acquire("nonexistent")


# ── Acquire ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_consumes_token(limiter):
    """acquire() exitoso reduce tokens en 1."""
    assert await limiter.acquire("reconciliation")
    bucket = limiter._buckets["reconciliation"]
    assert bucket.tokens == pytest.approx(4.0)  # 5 - 1


@pytest.mark.asyncio
async def test_acquire_drains_bucket(slow_limiter):
    """Al agotar los tokens, acquire retorna False."""
    assert await slow_limiter.acquire("reconciliation")  # consume → 0.0
    # Segunda debería fallar (no hay tokens)
    assert not await slow_limiter.acquire("reconciliation")


@pytest.mark.asyncio
async def test_buckets_are_independent(limiter):
    """Consumir de un bucket no afecta a los otros."""
    # Drenar reconciliation
    for _ in range(5):
        assert await limiter.acquire("reconciliation")
    # ad_hoc debería seguir teniendo tokens
    assert limiter._buckets["ad_hoc"].tokens == pytest.approx(5.0)
    assert await limiter.acquire("ad_hoc")


# ── Refill ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refill_over_time(limiter):
    """Los tokens se recargan con el tiempo."""
    # Drenar un bucket
    for _ in range(5):
        assert await limiter.acquire("reconciliation")

    assert not await limiter.acquire("reconciliation")

    # Esperar recarga: 10 tokens/s, share 0.70 → 7 tokens/s para reconciliation
    # Necesita 1 token → ~0.14s. Damos 0.3s para margen.
    await asyncio.sleep(0.3)

    assert await limiter.acquire("reconciliation")


@pytest.mark.asyncio
async def test_refill_respects_shares(limiter):
    """Cada bucket recibe su porcentaje de la recarga total."""
    # Drenar todos
    for _ in range(5):
        assert await limiter.acquire("reconciliation")
        assert await limiter.acquire("onboarding")
        assert await limiter.acquire("ad_hoc")

    await asyncio.sleep(0.5)  # 10 tokens/s × 0.5s = 5 tokens totales

    # reconciliation debería tener ~5 × 0.70 = 3.5 tokens
    # onboarding ~5 × 0.20 = 1.0
    # ad_hoc ~5 × 0.10 = 0.5

    # Verificar que reconciliation puede adquirir más que ad_hoc
    rec_tokens = limiter._buckets["reconciliation"].tokens
    adhoc_tokens = limiter._buckets["ad_hoc"].tokens
    assert rec_tokens > adhoc_tokens


# ── Cap (máximo burst) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tokens_never_exceed_max_burst(limiter):
    """Los tokens no superan max_burst incluso tras larga inactividad."""
    # Drenar ligeramente
    await limiter.acquire("reconciliation")
    # Esperar mucho — la recarga ocurre en el siguiente acquire
    await asyncio.sleep(1.0)

    # Disparar refill llamando a acquire (que internamente llama _refill)
    await limiter.acquire("reconciliation")

    bucket = limiter._buckets["reconciliation"]
    # Después del refill + acquire, debería estar en max_burst - 1
    assert bucket.tokens == pytest.approx(limiter.max_burst - 1.0)


# ── Wait acquire ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_acquire_succeeds_when_refilled(limiter):
    """wait_acquire espera y adquiere cuando los tokens se recargan."""
    for _ in range(5):
        assert await limiter.acquire("reconciliation")

    # Debería esperar y adquirir
    result = await limiter.wait_acquire("reconciliation", timeout=2.0)
    assert result


@pytest.mark.asyncio
async def test_wait_acquire_timeout(slow_limiter):
    """wait_acquire retorna False si el timeout expira."""
    await slow_limiter.acquire("reconciliation")  # agotar
    result = await slow_limiter.wait_acquire("reconciliation", timeout=0.05)
    assert not result


@pytest.mark.asyncio
async def test_acquire_or_wait_fast_path(limiter):
    """acquire_or_wait adquiere inmediatamente si hay tokens."""
    result = await limiter.acquire_or_wait("onboarding")
    assert result


# ── Stats ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stats_tracks_acquired_and_denied(limiter):
    """stats() refleja adquisiciones y denegaciones."""
    await limiter.acquire("reconciliation")
    await limiter.acquire("reconciliation")

    stats = limiter.stats()
    assert stats["reconciliation"]["acquired"] == 2
    assert stats["reconciliation"]["denied"] == 0

    # Agotar y forzar denegación
    for _ in range(3):
        await limiter.acquire("reconciliation")
    assert not await limiter.acquire("reconciliation")

    stats = limiter.stats()
    assert stats["reconciliation"]["denied"] >= 1


# ── Available ────────────────────────────────────────────────────

def test_available_checks_without_consuming(limiter):
    """available() no modifica el estado."""
    assert limiter.available("reconciliation")
    tokens_before = limiter._buckets["reconciliation"].tokens
    assert limiter.available("reconciliation")
    assert limiter._buckets["reconciliation"].tokens == tokens_before


def test_available_invalid_bucket(limiter):
    """available() retorna False para buckets inexistentes."""
    assert not limiter.available("fantasy_bucket")

"""
Rate-Limit Budget Manager — Token Bucket para el CLOB de Polymarket.

El CLOB bloquea agresivamente tras ~3-5 peticiones. Este módulo implementa
un algoritmo de Token Bucket con tres presupuestos independientes que comparten
un límite global de recarga, evitando que ningún subsistema monopolice el
rate-limit y garantizando fair scheduling.

Uso:
    limiter = RateLimiter(total_rate=0.4)  # 0.4 tokens/s → ~4 req / 10s
    async with limiter.bucket("onboarding"):
        await scanner.get_price(token_id)  # solo se ejecuta si hay token
"""

import asyncio
import time
from dataclasses import dataclass


# ── Budget allocation ─────────────────────────────────────────────
# Estos porcentajes controlan cómo se reparte la recarga entre buckets.
BUDGETS = {
    "reconciliation": 0.70,  # snapshots REST del CLOB durante resync
    "onboarding":     0.20,  # nuevos mercados que entran al Top 50
    "ad_hoc":         0.10,  # debug, dashboard, consultas manuales
}


@dataclass
class _Bucket:
    """Estado interno de un bucket."""
    name: str
    tokens: float
    share: float          # fracción del total_rate que recibe
    max_tokens: float     # capacidad máxima (evita acumulación infinita)
    total_acquired: int = 0
    total_denied: int = 0


class RateLimiter:
    """Token Bucket multi-categoría con recarga compartida.

    Parameters
    ----------
    total_rate : float
        Tokens generados por segundo para TODOS los buckets combinados.
        Default 0.4 → ~4 peticiones cada 10 segundos (límite práctico CLOB).
    max_burst : float
        Capacidad máxima de tokens acumulables por bucket.
    """

    def __init__(self, total_rate: float = 0.4, max_burst: float = 2.0):
        self.total_rate = total_rate
        self.max_burst = max_burst

        self._buckets = {
            name: _Bucket(
                name=name,
                tokens=max_burst,  # empezar lleno (cada bucket tiene su propia capacidad)
                share=share,
                max_tokens=max_burst,
            )
            for name, share in BUDGETS.items()
        }

        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    # ── Refill ───────────────────────────────────────────────────

    def _refill(self):
        """Recalcula tokens según el tiempo transcurrido desde el último refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return

        new_tokens = elapsed * self.total_rate
        for bucket in self._buckets.values():
            bucket.tokens = min(
                bucket.max_tokens,
                bucket.tokens + new_tokens * bucket.share,
            )

        self._last_refill = now

    # ── Public API ───────────────────────────────────────────────

    async def acquire(self, bucket_name: str) -> bool:
        """Intenta consumir 1 token del bucket. Retorna True si tuvo éxito.

        Sin bloqueo: si no hay tokens disponibles, retorna False inmediatamente.
        """
        bucket = self._buckets.get(bucket_name)
        if bucket is None:
            raise ValueError(
                f"Unknown budget bucket '{bucket_name}'. "
                f"Valid: {list(BUDGETS.keys())}"
            )

        async with self._lock:
            self._refill()

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                bucket.total_acquired += 1
                return True

            bucket.total_denied += 1
            return False

    async def wait_acquire(self, bucket_name: str, timeout: float = 5.0) -> bool:
        """Espera hasta que haya un token disponible o se alcance el timeout.

        Returns True si se adquirió el token, False si expiró el timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            if await self.acquire(bucket_name):
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            # Esperar ~1 ciclo de recarga antes de reintentar
            await asyncio.sleep(min(0.25, remaining))

    async def acquire_or_wait(self, bucket_name: str, timeout: float = 2.0) -> bool:
        """Adquiere inmediatamente si puede, o espera con timeout corto."""
        if await self.acquire(bucket_name):
            return True
        return await self.wait_acquire(bucket_name, timeout)

    # ── Inspection ───────────────────────────────────────────────

    def stats(self) -> dict:
        """Retorna estadísticas de uso de todos los buckets (sin lock, snapshot)."""
        return {
            name: {
                "tokens": round(b.tokens, 4),
                "share": b.share,
                "acquired": b.total_acquired,
                "denied": b.total_denied,
            }
            for name, b in self._buckets.items()
        }

    def available(self, bucket_name: str) -> bool:
        """Comprueba si hay al menos 1 token disponible (sin consumirlo).

        Nota: no es atómico — el token podría consumirse antes de usarse.
        """
        bucket = self._buckets.get(bucket_name)
        if bucket is None:
            return False
        # snapshot no atómico pero suficiente para sondear
        return bucket.tokens >= 1.0

    def get_all_budgets(self) -> dict:
        """Retorna estado de todos los buckets en formato compatible con frontend.

        Returns
        -------
        dict
            {bucket_name: {"available": pct, "total": 100, "label": str}}
        """
        self._refill()
        result = {}
        for name, bucket in self._buckets.items():
            pct = round((bucket.tokens / bucket.max_tokens) * 100)
            result[name] = {
                "available": pct,
                "total": 100,
                "label": name.replace("_", " ").title(),
            }
        return result


# ── Singleton por conveniencia ────────────────────────────────────────────────────

_default_limiter: RateLimiter | None = None


def get_default_limiter() -> RateLimiter:
    """Retorna (y crea si es necesario) el RateLimiter global."""
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = RateLimiter()
    return _default_limiter

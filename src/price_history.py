"""
Price History — Almacén persistente de snapshots de precios del radar.

Guarda snapshots en archivos JSON rotativos (uno por día) en data/history/.
Cada snapshot contiene: timestamp, condition_id, question, price, volume, spread.

Esto permite backtesting de estrategias sobre datos históricos reales.

Uso:
    store = PriceHistory()
    store.save_snapshots(snapshots)
    
    # Consultar historial
    df = store.to_dataframe("2026-05-08")
    prices = store.get_price_series(condition_id="0xabc...")
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("data/history")


class PriceHistory:
    """Almacén de snapshots históricos del radar."""

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._last_save = 0.0
        self._min_interval_s = 30  # mínimo entre saves

    def _daily_file(self) -> Path:
        """Archivo JSON para el día actual."""
        today = time.strftime("%Y-%m-%d")
        return self.data_dir / f"snapshots_{today}.json"

    def save_snapshots(self, snapshots: list[dict]) -> int:
        """Guarda snapshots en el archivo del día.

        Returns número de registros guardados.
        """
        now = time.time()
        if now - self._last_save < self._min_interval_s:
            return 0

        self._last_save = now
        filepath = self._daily_file()

        records = []
        for snap in snapshots:
            price = snap.get("price_yes")
            if price is None:
                continue
            records.append({
                "t": int(time.time()),
                "cid": snap.get("condition_id", ""),
                "q": snap.get("question", "")[:120],
                "p": round(float(price), 4),
                "v": round(float(snap.get("volume", 0)), 2),
                "s": round(float(snap.get("spread", 0)), 4) if snap.get("spread") else None,
            })

        if not records:
            return 0

        try:
            # Leer existente, append, guardar
            existing = []
            if filepath.exists():
                try:
                    existing = json.loads(filepath.read_text())
                except (json.JSONDecodeError, OSError):
                    existing = []

            existing.extend(records)
            filepath.write_text(json.dumps(existing))
            logger.debug("PriceHistory: %d snapshots guardados en %s", len(records), filepath.name)
        except OSError as e:
            logger.error("PriceHistory: error guardando %s: %s", filepath, e)

        return len(records)

    def load_day(self, date_str: str) -> list[dict]:
        """Carga todos los snapshots de un día específico.

        Parameters
        ----------
        date_str : str
            Fecha en formato "YYYY-MM-DD".

        Returns
        -------
        list[dict]
        """
        filepath = self.data_dir / f"snapshots_{date_str}.json"
        if not filepath.exists():
            return []
        try:
            return json.loads(filepath.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("PriceHistory: error cargando %s: %s", filepath, e)
            return []

    def get_price_series(
        self,
        condition_id: str | None = None,
        keyword: str | None = None,
        days: int = 7,
    ) -> list[dict]:
        """Obtiene serie de precios para un mercado específico.

        Parameters
        ----------
        condition_id : str | None
            Filtrar por condition_id exacto.
        keyword : str | None
            Filtrar por palabra clave en la pregunta.
        days : int
            Días hacia atrás a buscar.

        Returns
        -------
        list[dict]
            Lista de snapshots ordenados por timestamp.
        """
        results = []
        for i in range(days):
            date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            day_data = self.load_day(date_str)
            for snap in day_data:
                cid = snap.get("cid", "")
                question = snap.get("q", "")
                if condition_id and cid != condition_id:
                    continue
                if keyword and keyword.lower() not in question.lower():
                    continue
                results.append(snap)

        results.sort(key=lambda s: s.get("t", 0))
        return results

    def get_available_markets(self, days: int = 7) -> list[dict]:
        """Lista mercados con datos históricos.

        Returns
        -------
        list[dict]
            [{"condition_id": "...", "question": "...", "snapshot_count": N, "first_seen": t, "last_seen": t}]
        """
        markets: dict[str, dict] = {}
        for i in range(days):
            date_str = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            for snap in self.load_day(date_str):
                cid = snap.get("cid", "")
                if not cid:
                    continue
                if cid not in markets:
                    markets[cid] = {
                        "condition_id": cid,
                        "question": snap.get("q", ""),
                        "snapshot_count": 0,
                        "first_seen": snap["t"],
                        "last_seen": snap["t"],
                    }
                m = markets[cid]
                m["snapshot_count"] += 1
                m["first_seen"] = min(m["first_seen"], snap["t"])
                m["last_seen"] = max(m["last_seen"], snap["t"])

        return sorted(markets.values(), key=lambda m: m["snapshot_count"], reverse=True)

    def get_total_snapshots(self) -> int:
        """Total de snapshots en todos los archivos."""
        total = 0
        for f in self.data_dir.glob("snapshots_*.json"):
            try:
                data = json.loads(f.read_text())
                total += len(data)
            except (json.JSONDecodeError, OSError):
                pass
        return total

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_metrics.py
=============

Utilitaires de suivi "compute SQL" via pg_stat_statements.
Permet de faire un snapshot avant/après un run, puis d'afficher
les requêtes ayant le plus consommé de temps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass
class QueryDelta:
    queryid: str
    calls_delta: int
    total_exec_time_ms_delta: float
    rows_delta: int
    mean_exec_time_ms: float
    query_sample: str


class DbMetricsTracker:
    def __init__(self, engine):
        self.engine = engine
        self._start: dict[str, dict[str, Any]] = {}
        self._enabled = False
        self._error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def error(self) -> str | None:
        return self._error

    def _read_snapshot(self) -> dict[str, dict[str, Any]]:
        sql = text(
            """
            SELECT
                queryid::text AS queryid,
                calls,
                total_exec_time,
                rows,
                query
            FROM pg_stat_statements
            """
        )
        with self.engine.begin() as conn:
            rows = conn.execute(sql).mappings().all()
        return {str(r["queryid"]): dict(r) for r in rows}

    def start(self) -> None:
        try:
            # Test extension accessible
            with self.engine.begin() as conn:
                conn.execute(text("SELECT 1 FROM pg_stat_statements LIMIT 1"))
            self._start = self._read_snapshot()
            self._enabled = True
            self._error = None
        except Exception as e:  # pragma: no cover
            self._enabled = False
            self._error = str(e)

    def stop(self) -> list[QueryDelta]:
        if not self._enabled:
            return []

        end = self._read_snapshot()
        deltas: list[QueryDelta] = []

        for qid, after in end.items():
            before = self._start.get(qid)
            if not before:
                calls_delta = int(after["calls"] or 0)
                total_ms = float(after["total_exec_time"] or 0.0)
                rows_delta = int(after["rows"] or 0)
            else:
                calls_delta = int((after["calls"] or 0) - (before["calls"] or 0))
                total_ms = float((after["total_exec_time"] or 0.0) - (before["total_exec_time"] or 0.0))
                rows_delta = int((after["rows"] or 0) - (before["rows"] or 0))

            if calls_delta <= 0 and total_ms <= 0:
                continue

            mean_ms = total_ms / calls_delta if calls_delta > 0 else 0.0
            query_sample = str(after.get("query") or "").strip().replace("\n", " ")
            if len(query_sample) > 220:
                query_sample = query_sample[:220] + "..."

            deltas.append(
                QueryDelta(
                    queryid=qid,
                    calls_delta=calls_delta,
                    total_exec_time_ms_delta=total_ms,
                    rows_delta=rows_delta,
                    mean_exec_time_ms=mean_ms,
                    query_sample=query_sample,
                )
            )

        deltas.sort(key=lambda d: d.total_exec_time_ms_delta, reverse=True)
        return deltas


def print_db_metrics_summary(log, deltas: list[QueryDelta], *, top_n: int = 8) -> None:
    if not deltas:
        log("📈 pg_stat_statements: aucun delta mesurable sur ce run.")
        return

    total_ms = sum(d.total_exec_time_ms_delta for d in deltas)
    total_calls = sum(d.calls_delta for d in deltas)
    log(
        f"📈 pg_stat_statements (run): {len(deltas)} requêtes impactées | "
        f"{total_calls:,} appels | {total_ms/1000:.2f}s exec SQL cumulée"
    )

    for i, d in enumerate(deltas[:top_n], 1):
        log(
            f"   #{i} {d.total_exec_time_ms_delta/1000:>7.2f}s | "
            f"calls={d.calls_delta:>5} | mean={d.mean_exec_time_ms:>7.1f}ms | "
            f"rows={d.rows_delta:>7} | {d.query_sample}"
        )

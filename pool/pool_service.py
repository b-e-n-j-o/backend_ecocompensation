from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

# Aligné sur backend/sql/ecocompensation_results_pool_tables.sql (CREATE IF NOT EXISTS).
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    scope text NOT NULL DEFAULT 'parcelles',
    options_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pool_runs_project_created
ON ecocompensation_results.parcelles_pool_runs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    rank integer NULL,
    surface_ha double precision NULL,
    miller double precision NULL,
    distance_km double precision NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu)
);

CREATE INDEX IF NOT EXISTS idx_pool_project_run
ON ecocompensation_results.parcelles_pool(project_id, run_id);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_metrics (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    metric_key text NOT NULL,
    metric_value_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu, metric_key)
);

CREATE INDEX IF NOT EXISTS idx_pool_metrics_project_run_idu
ON ecocompensation_results.parcelles_pool_metrics(project_id, run_id, idu);
"""


def ensure_tables(conn) -> None:
    for stmt in [s.strip() for s in CREATE_TABLES_SQL.split(";") if s.strip()]:
        conn.execute(text(stmt))


def create_run(conn, project_id: str, scope: str, options_json: dict[str, Any], total_count: int) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO ecocompensation_results.parcelles_pool_runs
                (id, project_id, scope, options_json, total_count)
            VALUES
                (CAST(:run_id AS uuid), CAST(:project_id AS uuid), :scope, CAST(:options_json AS jsonb), :total_count)
            """
        ),
        {
            "run_id": run_id,
            "project_id": project_id,
            "scope": scope,
            "options_json": json.dumps(options_json),
            "total_count": int(total_count),
        },
    )
    return run_id


def insert_pool_parcelles(conn, project_id: str, run_id: str, parcelles: list[dict[str, Any]]) -> None:
    if not parcelles:
        return
    for p in parcelles:
        conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.parcelles_pool
                    (run_id, project_id, idu, rank, surface_ha, miller, distance_km)
                VALUES
                    (
                        CAST(:run_id AS uuid),
                        CAST(:project_id AS uuid),
                        :idu,
                        :rank,
                        :surface_ha,
                        :miller,
                        :distance_km
                    )
                ON CONFLICT (run_id, idu) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    surface_ha = EXCLUDED.surface_ha,
                    miller = EXCLUDED.miller,
                    distance_km = EXCLUDED.distance_km,
                    created_at = now()
                """
            ),
            {
                "run_id": run_id,
                "project_id": project_id,
                "idu": p.get("idu"),
                "rank": p.get("rank"),
                "surface_ha": p.get("surface_ha"),
                "miller": p.get("miller"),
                "distance_km": p.get("distance_km"),
            },
        )


def upsert_metric(conn, project_id: str, run_id: str, idu: str, metric_key: str, metric_value: dict[str, Any]) -> None:
    conn.execute(
        text(
            """
            INSERT INTO ecocompensation_results.parcelles_pool_metrics
                (run_id, project_id, idu, metric_key, metric_value_jsonb, updated_at)
            VALUES
                (
                    CAST(:run_id AS uuid),
                    CAST(:project_id AS uuid),
                    :idu,
                    :metric_key,
                    CAST(:metric_value_jsonb AS jsonb),
                    now()
                )
            ON CONFLICT (run_id, idu, metric_key) DO UPDATE SET
                metric_value_jsonb = EXCLUDED.metric_value_jsonb,
                updated_at = now()
            """
        ),
        {
            "run_id": run_id,
            "project_id": project_id,
            "idu": idu,
            "metric_key": metric_key,
            "metric_value_jsonb": json.dumps(metric_value),
        },
    )


def list_runs(conn, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT id, project_id, scope, options_json, total_count, created_at
            FROM ecocompensation_results.parcelles_pool_runs
            WHERE project_id = CAST(:project_id AS uuid)
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_pool(conn, project_id: str, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT idu, rank, surface_ha, miller, distance_km, created_at
            FROM ecocompensation_results.parcelles_pool
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = CAST(:run_id AS uuid)
            ORDER BY rank NULLS LAST, idu
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_metrics(conn, project_id: str, run_id: str, idu: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT metric_key, metric_value_jsonb, updated_at
            FROM ecocompensation_results.parcelles_pool_metrics
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = CAST(:run_id AS uuid)
              AND idu = :idu
            ORDER BY metric_key
            """
        ),
        {"project_id": project_id, "run_id": run_id, "idu": idu},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_all_metrics_grouped_by_idu(conn, project_id: str, run_id: str) -> dict[str, list[dict[str, Any]]]:
    """Toutes les métriques d’un run, groupées par IDU (pour préchargement front / tri)."""
    rows = conn.execute(
        text(
            """
            SELECT idu, metric_key, metric_value_jsonb, updated_at
            FROM ecocompensation_results.parcelles_pool_metrics
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = CAST(:run_id AS uuid)
            ORDER BY idu, metric_key
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    by_idu: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        idu = str(r["idu"])
        by_idu.setdefault(idu, []).append(
            {
                "metric_key": r["metric_key"],
                "metric_value_jsonb": r["metric_value_jsonb"],
                "updated_at": r["updated_at"],
            }
        )
    return by_idu


def purge_old_runs(conn, project_id: str, keep_last: int = 5) -> None:
    old_rows = conn.execute(
        text(
            """
            SELECT id
            FROM ecocompensation_results.parcelles_pool_runs
            WHERE project_id = CAST(:project_id AS uuid)
            ORDER BY created_at DESC
            OFFSET :keep_last
            """
        ),
        {"project_id": project_id, "keep_last": keep_last},
    ).mappings().all()
    old_ids = [str(r["id"]) for r in old_rows]
    if not old_ids:
        return
    conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.parcelles_pool_metrics
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = ANY(CAST(:run_ids AS uuid[]))
            """
        ),
        {"project_id": project_id, "run_ids": old_ids},
    )
    conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.parcelles_pool
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = ANY(CAST(:run_ids AS uuid[]))
            """
        ),
        {"project_id": project_id, "run_ids": old_ids},
    )
    conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.parcelles_pool_runs
            WHERE project_id = CAST(:project_id AS uuid)
              AND id = ANY(CAST(:run_ids AS uuid[]))
            """
        ),
        {"project_id": project_id, "run_ids": old_ids},
    )


def persist_parcelles_pool_run(
    engine,
    *,
    project_id: str,
    options_json: dict[str, Any],
    parcelles: list[dict[str, Any]],
    scope: str = "parcelles",
    keep_last: int = 5,
) -> str:
    with engine.begin() as conn:
        ensure_tables(conn)
        run_id = create_run(
            conn,
            project_id=project_id,
            scope=scope,
            options_json=options_json,
            total_count=len(parcelles),
        )
        insert_pool_parcelles(conn, project_id=project_id, run_id=run_id, parcelles=parcelles)
        # Les profilers / métriques sont calculés dans un second temps (voir
        # POST .../pool/runs/{run_id}/recompute-metrics) pour découpler la durée du filtrage
        # de la phase souvent plus lourde.
        purge_old_runs(conn, project_id=project_id, keep_last=keep_last)
    return run_id

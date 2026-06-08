from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from sqlalchemy import bindparam, text


def _run_optional_in_savepoint(conn, savepoint_name: str, fn) -> None:
    """
    Exécute fn() dans un SAVEPOINT : en cas d’erreur, rollback partiel pour que la
    transaction parente reste utilisable (évite InFailedSqlTransaction sur la suite).
    """
    sp = savepoint_name.replace("-", "_").replace(" ", "_")[:63]
    conn.execute(text(f"SAVEPOINT {sp}"))
    try:
        fn()
        conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
    except Exception:
        conn.execute(text(f"ROLLBACK TO SAVEPOINT {sp}"))


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
    dist_hydro_m double precision NULL,
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

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_pool_indesirables (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idu)
);

CREATE INDEX IF NOT EXISTS idx_pool_indesirables_project_run
ON ecocompensation_results.parcelles_pool_indesirables(project_id, run_id);

CREATE TABLE IF NOT EXISTS ecocompensation_results.parcelles_project_indesirables (
    project_id uuid NOT NULL,
    idu text NOT NULL,
    source_run_id uuid NULL,
    rank integer NULL,
    code_insee text NULL,
    section text NULL,
    numero text NULL,
    surface_ha double precision NULL,
    miller double precision NULL,
    distance_km double precision NULL,
    dist_hydro_m double precision NULL,
    metrics_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, idu)
);

CREATE INDEX IF NOT EXISTS idx_project_indesirables_project_updated
ON ecocompensation_results.parcelles_project_indesirables(project_id, updated_at DESC);
"""

_ensure_tables_lock = threading.Lock()
_ensure_tables_done = False


def ensure_tables(conn) -> None:
    global _ensure_tables_done
    if _ensure_tables_done:
        return
    with _ensure_tables_lock:
        if _ensure_tables_done:
            return
    # CREATE INDEX sur une table déjà volumineuse peut dépasser le statement_timeout par défaut (Supabase).
        _run_optional_in_savepoint(
            conn,
            "sp_pool_stmt_timeout",
            lambda: conn.execute(text("SET LOCAL statement_timeout = '15min'")),
        )
        for stmt in [s.strip() for s in CREATE_TABLES_SQL.split(";") if s.strip()]:
            conn.execute(text(stmt))
        # Colonnes ajoutées après création initiale des tables (migrations légères).
        _run_optional_in_savepoint(
            conn,
            "sp_pool_result_summary_col",
            lambda: conn.execute(
                text(
                    """
                    ALTER TABLE ecocompensation_results.parcelles_pool_runs
                    ADD COLUMN IF NOT EXISTS result_summary jsonb NOT NULL DEFAULT '{}'::jsonb
                    """
                )
            ),
        )
        _run_optional_in_savepoint(
            conn,
            "sp_pool_profiling_progress_col",
            lambda: conn.execute(
                text(
                    """
                    ALTER TABLE ecocompensation_results.parcelles_pool_runs
                    ADD COLUMN IF NOT EXISTS profiling_progress jsonb NOT NULL DEFAULT '{}'::jsonb
                    """
                )
            ),
        )
        _run_optional_in_savepoint(
            conn,
            "sp_pool_dist_hydro_col",
            lambda: conn.execute(
                text(
                    """
                    ALTER TABLE ecocompensation_results.parcelles_pool
                    ADD COLUMN IF NOT EXISTS dist_hydro_m double precision NULL
                    """
                )
            ),
        )
        _ensure_tables_done = True


def _coerce_json_mapping(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            o = json.loads(val)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


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
                    (run_id, project_id, idu, rank, surface_ha, miller, distance_km, dist_hydro_m)
                VALUES
                    (
                        CAST(:run_id AS uuid),
                        CAST(:project_id AS uuid),
                        :idu,
                        :rank,
                        :surface_ha,
                        :miller,
                        :distance_km,
                        :dist_hydro_m
                    )
                ON CONFLICT (run_id, idu) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    surface_ha = EXCLUDED.surface_ha,
                    miller = EXCLUDED.miller,
                    distance_km = EXCLUDED.distance_km,
                    dist_hydro_m = EXCLUDED.dist_hydro_m,
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
                "dist_hydro_m": p.get("dist_hydro_m"),
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


def list_runs(conn, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
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
        {"project_id": project_id, "limit": lim},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if d.get("id") is not None:
            d["id"] = str(d["id"])
        if d.get("project_id") is not None:
            d["project_id"] = str(d["project_id"])
        out.append(d)
    return out


def _parcelles_pool_runs_has_result_summary(conn) -> bool:
    r = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'ecocompensation_results'
              AND table_name = 'parcelles_pool_runs'
              AND column_name = 'result_summary'
            LIMIT 1
            """
        )
    ).first()
    return r is not None


def get_run_meta(conn, project_id: str, run_id: str) -> dict[str, Any] | None:
    has_rs = _parcelles_pool_runs_has_result_summary(conn)
    if has_rs:
        row = conn.execute(
            text(
                """
                SELECT
                    id,
                    project_id,
                    scope,
                    options_json,
                    total_count,
                    created_at,
                    COALESCE(result_summary, '{}'::jsonb) AS result_summary
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE id = CAST(:run_id AS uuid)
                  AND project_id = CAST(:project_id AS uuid)
                """
            ),
            {"run_id": run_id, "project_id": project_id},
        ).mappings().first()
    else:
        row = conn.execute(
            text(
                """
                SELECT
                    id,
                    project_id,
                    scope,
                    options_json,
                    total_count,
                    created_at
                FROM ecocompensation_results.parcelles_pool_runs
                WHERE id = CAST(:run_id AS uuid)
                  AND project_id = CAST(:project_id AS uuid)
                """
            ),
            {"run_id": run_id, "project_id": project_id},
        ).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    d["project_id"] = str(d["project_id"])
    d["options_json"] = _coerce_json_mapping(d.get("options_json"))
    d["result_summary"] = _coerce_json_mapping(d.get("result_summary")) if has_rs else {}
    return d


def get_parcelles_for_run_results(
    conn, project_id: str, run_id: str, aoi_id_str: str
) -> list[dict[str, Any]]:
    """
    Parcelles du pool pour un run, enrichies avec code_insee / section / numero depuis
    ecocompensation_results.parcelles quand c’est possible. dist_hydro_m n’est pas stocké
    sur le pool : absent ici (null).
    """
    aoi = str(aoi_id_str or "").strip()
    rows = conn.execute(
        text(
            """
            SELECT
                pp.idu,
                pp.rank,
                pp.surface_ha,
                pp.miller,
                pp.distance_km,
                pp.dist_hydro_m,
                p.code_insee,
                p.section,
                p.numero
            FROM ecocompensation_results.parcelles_pool pp
            LEFT JOIN LATERAL (
                SELECT p0.code_insee, p0.section, p0.numero
                FROM ecocompensation_results.parcelles p0
                WHERE p0.idu = pp.idu
                  AND (
                    p0.project_id = CAST(:project_id AS uuid)
                    OR (
                        CAST(:aoi AS text) <> ''
                        AND p0.aoi_id IS NOT NULL
                        AND CAST(p0.aoi_id AS text) = CAST(:aoi AS text)
                    )
                  )
                ORDER BY CASE WHEN p0.project_id = CAST(:project_id AS uuid) THEN 0 ELSE 1 END
                LIMIT 1
            ) p ON TRUE
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
            ORDER BY pp.rank NULLS LAST, pp.idu
            """
        ),
        {"project_id": project_id, "run_id": run_id, "aoi": aoi},
    ).mappings().all()
    parcelles: list[dict[str, Any]] = []
    for r in rows:
        idu = str(r["idu"] or "")
        raw = (idu or "").strip()
        cinsee = r.get("code_insee") or (raw[:5] if len(raw) >= 5 else "")
        section = r.get("section") or (raw[8:10] if len(raw) >= 10 else "")
        numero = r.get("numero") or (raw[-4:] if len(raw) >= 4 else "")
        parcelles.append(
            {
                "idu": idu,
                "rank": int(r["rank"] or 0),
                "code_insee": str(cinsee or ""),
                "section": str(section or ""),
                "numero": str(numero or ""),
                "surface_ha": round(float(r["surface_ha"] or 0), 2),
                "miller": round(float(r["miller"] or 0), 4),
                "distance_km": round(float(r["distance_km"] or 0), 2),
                "dist_hydro_m": (
                    round(float(r["dist_hydro_m"]), 1)
                    if r.get("dist_hydro_m") is not None
                    else None
                ),
            }
        )
    return parcelles


def get_parcelles_geometries_for_run(
    conn, project_id: str, run_id: str
) -> list[dict[str, Any]]:
    """
    Géométries WGS84 des parcelles d'un run pool.
    Jointure parcelles_pool → parcelles (geom_2154) par project_id + idu.
    """
    rows = conn.execute(
        text(
            """
            SELECT
                pp.idu,
                pp.rank,
                ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326))::json AS geometry
            FROM ecocompensation_results.parcelles_pool pp
            INNER JOIN ecocompensation_results.parcelles p
                ON p.project_id = pp.project_id
               AND p.idu = pp.idu
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
              AND p.geom_2154 IS NOT NULL
            ORDER BY pp.rank NULLS LAST, pp.idu
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_parcelles_geometries_by_idus(
    conn, project_id: str, idus: list[str]
) -> list[dict[str, Any]]:
    """Géométries WGS84 pour une liste d'IDU (fallback last_results sans run_id)."""
    if not idus:
        return []
    stmt = text(
        """
        SELECT
            p.idu,
            ST_AsGeoJSON(ST_Transform(p.geom_2154, 4326))::json AS geometry
        FROM ecocompensation_results.parcelles p
        WHERE p.project_id = CAST(:project_id AS uuid)
          AND p.idu IN :idus
          AND p.geom_2154 IS NOT NULL
        """
    ).bindparams(bindparam("idus", expanding=True))
    rows = conn.execute(
        stmt,
        {"project_id": project_id, "idus": idus},
    ).mappings().all()
    return [dict(r) for r in rows]


def build_filter_snapshot_from_run(
    conn, project_id: str, run_id: str, aoi_id_str: str
) -> dict[str, Any] | None:
    """Payload compatible avec la réponse POST /filter (sans memory), enrichi des métriques pool."""
    meta = get_run_meta(conn, project_id, run_id)
    if not meta:
        return None
    if str(meta.get("scope") or "parcelles") != "parcelles":
        return None
    parcelles = get_parcelles_for_run_results(conn, project_id, run_id, aoi_id_str)
    by_idu = get_all_metrics_grouped_by_idu(conn, project_id, run_id)
    rs = meta.get("result_summary") or {}
    total = int(rs.get("total") or len(parcelles) or meta.get("total_count") or 0)
    return {
        "pool_run_id": str(meta["id"]),
        "filter_options": meta.get("options_json") or {},
        "total": total,
        "final_radius_km": float(rs.get("final_radius_km") or 0),
        "funnel": rs.get("funnel") if isinstance(rs.get("funnel"), list) else [],
        "parcelles": parcelles,
        "by_idu": by_idu,
        "total_parcelles_metrics": len(by_idu),
        "run_created_at": meta.get("created_at").isoformat() if meta.get("created_at") else None,
    }


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
            DELETE FROM ecocompensation_results.parcelles_pool_indesirables
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
    result_summary: dict[str, Any] | None = None,
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
        if result_summary:
            conn.execute(
                text(
                    """
                    UPDATE ecocompensation_results.parcelles_pool_runs
                    SET result_summary = CAST(:rs AS jsonb)
                    WHERE id = CAST(:run_id AS uuid)
                      AND project_id = CAST(:project_id AS uuid)
                    """
                ),
                {
                    "rs": json.dumps(result_summary),
                    "run_id": run_id,
                    "project_id": project_id,
                },
            )
        # Les profilers / métriques sont calculés dans un second temps (voir
        # POST .../pool/runs/{run_id}/recompute-metrics) pour découpler la durée du filtrage
        # de la phase souvent plus lourde.
        purge_old_runs(conn, project_id=project_id, keep_last=keep_last)
    return run_id


def run_belongs_to_project(conn, project_id: str, run_id: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM ecocompensation_results.parcelles_pool_runs
            WHERE id = CAST(:run_id AS uuid)
              AND project_id = CAST(:project_id AS uuid)
            LIMIT 1
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).first()
    return row is not None


def idus_in_pool(conn, project_id: str, run_id: str, idus: list[str]) -> set[str]:
    uniq = list(dict.fromkeys(str(x).strip() for x in idus if x and str(x).strip()))
    if not uniq:
        return set()
    stmt = text(
        """
        SELECT idu
        FROM ecocompensation_results.parcelles_pool
        WHERE project_id = CAST(:project_id AS uuid)
          AND run_id = CAST(:run_id AS uuid)
          AND idu IN :idus
        """
    ).bindparams(bindparam("idus", expanding=True))
    rows = conn.execute(
        stmt,
        {"project_id": project_id, "run_id": run_id, "idus": uniq},
    ).mappings().all()
    return {str(r["idu"]) for r in rows}


def list_indesirables(conn, project_id: str, run_id: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT idu
            FROM ecocompensation_results.parcelles_pool_indesirables
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = CAST(:run_id AS uuid)
            ORDER BY idu
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    return [str(r["idu"]) for r in rows]


def add_indesirables(conn, project_id: str, run_id: str, idus: list[str]) -> int:
    """Marque des parcelles comme indésirables (présentes dans le pool du run). Retourne le nombre d’insertions."""
    ok = idus_in_pool(conn, project_id, run_id, idus)
    inserted = 0
    for idu in ok:
        r = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.parcelles_pool_indesirables (run_id, project_id, idu)
                VALUES (CAST(:run_id AS uuid), CAST(:project_id AS uuid), :idu)
                ON CONFLICT (run_id, idu) DO NOTHING
                """
            ),
            {"run_id": run_id, "project_id": project_id, "idu": idu},
        )
        if getattr(r, "rowcount", None) == 1:
            inserted += 1
    return inserted


def remove_indesirable(conn, project_id: str, run_id: str, idu: str) -> bool:
    r = conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.parcelles_pool_indesirables
            WHERE project_id = CAST(:project_id AS uuid)
              AND run_id = CAST(:run_id AS uuid)
              AND idu = :idu
            """
        ),
        {"project_id": project_id, "run_id": run_id, "idu": idu},
    )
    return (getattr(r, "rowcount", 0) or 0) > 0


def count_project_indesirables(conn, project_id: str) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*)::int
                FROM ecocompensation_results.parcelles_project_indesirables
                WHERE project_id = CAST(:project_id AS uuid)
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
    )


def list_project_indesirable_idus(conn, project_id: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT idu
            FROM ecocompensation_results.parcelles_project_indesirables
            WHERE project_id = CAST(:project_id AS uuid)
            ORDER BY updated_at DESC, idu
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [str(r["idu"]) for r in rows]


def filter_parcelles_excluding_project_indesirables(
    conn,
    project_id: str,
    parcelles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retire du jeu exporté / classement les parcelles marquées indésirables (projet)."""
    excluded = set(list_project_indesirable_idus(conn, project_id))
    if not excluded:
        return parcelles
    return [p for p in parcelles if str(p.get("idu") or "") not in excluded]


def list_project_indesirables_rows(conn, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
                idu,
                source_run_id,
                rank,
                code_insee,
                section,
                numero,
                surface_ha,
                miller,
                distance_km,
                dist_hydro_m,
                COALESCE(metrics_jsonb, '{}'::jsonb) AS metrics_jsonb,
                created_at,
                updated_at
            FROM ecocompensation_results.parcelles_project_indesirables
            WHERE project_id = CAST(:project_id AS uuid)
            ORDER BY updated_at DESC, rank NULLS LAST, idu
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_project_indesirables_payload(conn, project_id: str) -> dict[str, Any]:
    rows = list_project_indesirables_rows(conn, project_id)
    parcelles: list[dict[str, Any]] = []
    by_idu: dict[str, list[dict[str, Any]]] = {}
    idus: list[str] = []
    for r in rows:
        idu = str(r.get("idu") or "")
        if not idu:
            continue
        idus.append(idu)
        parcelles.append(
            {
                "idu": idu,
                "rank": int(r.get("rank") or 0),
                "code_insee": str(r.get("code_insee") or ""),
                "section": str(r.get("section") or ""),
                "numero": str(r.get("numero") or ""),
                "surface_ha": round(float(r.get("surface_ha") or 0), 2),
                "miller": round(float(r.get("miller") or 0), 4),
                "distance_km": round(float(r.get("distance_km") or 0), 2),
                "dist_hydro_m": float(r.get("dist_hydro_m")) if r.get("dist_hydro_m") is not None else None,
            }
        )
        m = _coerce_json_mapping(r.get("metrics_jsonb"))
        by_idu[idu] = [
            {
                "metric_key": str(k),
                "metric_value_jsonb": v if isinstance(v, dict) else {},
                "updated_at": r.get("updated_at"),
            }
            for k, v in m.items()
        ]
    return {"idus": idus, "parcelles": parcelles, "by_idu": by_idu, "total": len(parcelles)}


def add_project_indesirables_from_run(conn, project_id: str, run_id: str, idus: list[str]) -> int:
    valid_idus = idus_in_pool(conn, project_id, run_id, idus)
    if not valid_idus:
        return 0
    aoi_row = conn.execute(
        text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:pid AS uuid) LIMIT 1"),
        {"pid": project_id},
    ).mappings().one_or_none()
    aoi_id_str = str(aoi_row.get("aoi_id") or "") if aoi_row else ""
    run_parcelles = get_parcelles_for_run_results(conn, project_id, run_id, aoi_id_str)
    parcelles_by_idu = {str(p.get("idu")): p for p in run_parcelles if p.get("idu")}

    stmt_metrics = text(
        """
        SELECT idu, metric_key, metric_value_jsonb
        FROM ecocompensation_results.parcelles_pool_metrics
        WHERE project_id = CAST(:project_id AS uuid)
          AND run_id = CAST(:run_id AS uuid)
          AND idu IN :idus
        ORDER BY idu, metric_key
        """
    ).bindparams(bindparam("idus", expanding=True))
    mrows = conn.execute(
        stmt_metrics,
        {"project_id": project_id, "run_id": run_id, "idus": list(valid_idus)},
    ).mappings().all()
    metrics_by_idu: dict[str, dict[str, Any]] = {}
    for r in mrows:
        idu = str(r["idu"])
        metrics_by_idu.setdefault(idu, {})[str(r["metric_key"])] = (
            r["metric_value_jsonb"] if isinstance(r["metric_value_jsonb"], dict) else {}
        )

    inserted = 0
    for idu in valid_idus:
        p = parcelles_by_idu.get(idu) or {}
        r = conn.execute(
            text(
                """
                INSERT INTO ecocompensation_results.parcelles_project_indesirables (
                    project_id, idu, source_run_id, rank, code_insee, section, numero,
                    surface_ha, miller, distance_km, dist_hydro_m, metrics_jsonb, created_at, updated_at
                ) VALUES (
                    CAST(:project_id AS uuid), :idu, CAST(:run_id AS uuid), :rank, :code_insee, :section, :numero,
                    :surface_ha, :miller, :distance_km, :dist_hydro_m, CAST(:metrics_jsonb AS jsonb), now(), now()
                )
                ON CONFLICT (project_id, idu) DO UPDATE SET
                    source_run_id = EXCLUDED.source_run_id,
                    rank = EXCLUDED.rank,
                    code_insee = EXCLUDED.code_insee,
                    section = EXCLUDED.section,
                    numero = EXCLUDED.numero,
                    surface_ha = EXCLUDED.surface_ha,
                    miller = EXCLUDED.miller,
                    distance_km = EXCLUDED.distance_km,
                    dist_hydro_m = EXCLUDED.dist_hydro_m,
                    metrics_jsonb = EXCLUDED.metrics_jsonb,
                    updated_at = now()
                """
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "idu": idu,
                "rank": p.get("rank"),
                "code_insee": p.get("code_insee"),
                "section": p.get("section"),
                "numero": p.get("numero"),
                "surface_ha": p.get("surface_ha"),
                "miller": p.get("miller"),
                "distance_km": p.get("distance_km"),
                "dist_hydro_m": p.get("dist_hydro_m"),
                "metrics_jsonb": json.dumps(metrics_by_idu.get(idu) or {}),
            },
        )
        if getattr(r, "rowcount", 0):
            inserted += 1
    return inserted


def remove_project_indesirable(conn, project_id: str, idu: str) -> bool:
    r = conn.execute(
        text(
            """
            DELETE FROM ecocompensation_results.parcelles_project_indesirables
            WHERE project_id = CAST(:project_id AS uuid)
              AND idu = :idu
            """
        ),
        {"project_id": project_id, "idu": idu},
    )
    return (getattr(r, "rowcount", 0) or 0) > 0

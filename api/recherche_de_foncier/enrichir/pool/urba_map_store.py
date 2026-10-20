"""
Persistance carto du profil urbanisme.

Une entité GPU n'est stockée qu'une fois par run, découpée sur l'union des
parcelles qui la rencontrent, chacune élargie de 100 m. Le rapport texte reste
dans parcelles_pool_metrics (urbanisme_gpu).
"""

from __future__ import annotations

import json
import logging

from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from sqlalchemy import text

logger = logging.getLogger(__name__)

_SCHEMA = "ecocompensation_results"


def _pg_text_array(values: list[str] | None) -> str:
    parts: list[str] = []
    for raw in values or []:
        escaped = str(raw).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{escaped}"')
    return "{" + ",".join(parts) + "}"


def ensure_urba_tables(conn) -> None:
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.urba_run (
                run_id uuid PRIMARY KEY,
                project_id uuid NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                settings jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                layers text[] NOT NULL DEFAULT '{{}}'
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.urba_entite (
                run_id uuid NOT NULL,
                fid text NOT NULL,
                layer_key text NOT NULL,
                famille text NOT NULL,
                geom_kind text NOT NULL,
                display_name text,
                code text,
                libelle_cnig text,
                libelle text,
                attributs jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                flags text[] NOT NULL DEFAULT '{{}}',
                geom_2154 geometry(Geometry, 2154),
                PRIMARY KEY (run_id, layer_key, fid)
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.urba_parcelle_entite (
                run_id uuid NOT NULL,
                cible_id text NOT NULL,
                cible_type text NOT NULL,
                layer_key text NOT NULL,
                fid text NOT NULL,
                bucket text NOT NULL,
                position text,
                surface_m2 double precision,
                pct_cible double precision,
                longueur_m double precision,
                nb_points integer,
                distance_m double precision,
                PRIMARY KEY (run_id, cible_id, cible_type, layer_key, fid)
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.urba_cible_resultat (
                run_id uuid NOT NULL,
                cible_id text NOT NULL,
                cible_type text NOT NULL,
                synthese jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                flags text[] NOT NULL DEFAULT '{{}}',
                score jsonb,
                PRIMARY KEY (run_id, cible_id, cible_type)
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS urba_entite_geom_gix
                ON {_SCHEMA}.urba_entite USING gist (geom_2154)
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS urba_parcelle_entite_run_cible_idx
                ON {_SCHEMA}.urba_parcelle_entite (run_id, cible_id)
            """
        )
    )


def _merge_clip(clips: list, previous) -> object | None:
    from shapely import make_valid

    parts = [c for c in clips if c is not None and not c.is_empty]
    if previous is not None and not previous.is_empty:
        parts.append(previous)
    if not parts:
        return None
    try:
        merged = unary_union(parts)
    except Exception:
        merged = unary_union([make_valid(p) for p in parts])
    if merged is None or merged.is_empty:
        return None
    return merged


def store_urba_context(conn, project_id: str, run_id: str, hits: list[dict], response) -> int:
    """
    Remplace les liens des parcelles de cette analyse et met à jour les géométries.

    Une entité encore liée à d'autres parcelles du même run conserve son emprise
    précédente, unie au nouveau découpage. Les entités devenues orphelines sont
    supprimées.
    """
    ensure_urba_tables(conn)
    meta = response.meta
    settings = meta.settings if isinstance(meta.settings, dict) else {}
    layers = list(meta.layers or [])
    conn.execute(
        text(
            f"""
            INSERT INTO {_SCHEMA}.urba_run (run_id, project_id, created_at, settings, layers)
            VALUES (
                CAST(:run_id AS uuid),
                CAST(:project_id AS uuid),
                now(),
                CAST(:settings AS jsonb),
                CAST(:layers AS text[])
            )
            ON CONFLICT (run_id) DO UPDATE SET
                project_id = EXCLUDED.project_id,
                created_at = now(),
                settings = EXCLUDED.settings,
                layers = EXCLUDED.layers
            """
        ),
        {
            "run_id": run_id,
            "project_id": project_id,
            "settings": json.dumps(settings, default=str),
            "layers": _pg_text_array(layers),
        },
    )

    idus = list(response.parcelles.keys())
    if idus:
        conn.execute(
            text(
                f"""
                DELETE FROM {_SCHEMA}.urba_parcelle_entite
                WHERE run_id = CAST(:run_id AS uuid)
                  AND cible_type = 'parcelle'
                  AND cible_id = ANY(CAST(:idus AS text[]))
                """
            ),
            {"run_id": run_id, "idus": _pg_text_array(idus)},
        )
        conn.execute(
            text(
                f"""
                DELETE FROM {_SCHEMA}.urba_cible_resultat
                WHERE run_id = CAST(:run_id AS uuid)
                  AND cible_type = 'parcelle'
                  AND cible_id = ANY(CAST(:idus AS text[]))
                """
            ),
            {"run_id": run_id, "idus": _pg_text_array(idus)},
        )
    conn.execute(
        text(
            f"""
            DELETE FROM {_SCHEMA}.urba_entite e
            WHERE e.run_id = CAST(:run_id AS uuid)
              AND NOT EXISTS (
                  SELECT 1
                  FROM {_SCHEMA}.urba_parcelle_entite p
                  WHERE p.run_id = e.run_id
                    AND p.layer_key = e.layer_key
                    AND p.fid = e.fid
              )
            """
        ),
        {"run_id": run_id},
    )

    existing: dict[tuple[str, str], object] = {}
    rows = conn.execute(
        text(
            f"""
            SELECT layer_key, fid, ST_AsGeoJSON(geom_2154) AS gj
            FROM {_SCHEMA}.urba_entite
            WHERE run_id = CAST(:run_id AS uuid)
              AND geom_2154 IS NOT NULL
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        try:
            existing[(row["layer_key"], row["fid"])] = shape(json.loads(row["gj"]))
        except Exception:
            logger.warning("géométrie urba illisible %s/%s", row["layer_key"], row["fid"])

    grouped: dict[tuple[str, str], list[dict]] = {}
    for hit in hits:
        clip = hit.get("clip")
        if clip is None:
            continue
        grouped.setdefault((hit["layer_key"], hit["fid"]), []).append(hit)

    upsert_entite = text(
        f"""
        INSERT INTO {_SCHEMA}.urba_entite (
            run_id, fid, layer_key, famille, geom_kind, display_name,
            code, libelle_cnig, libelle, attributs, flags, geom_2154
        ) VALUES (
            CAST(:run_id AS uuid), :fid, :layer_key, :famille, :geom_kind, :display_name,
            :code, :libelle_cnig, :libelle, CAST(:attributs AS jsonb), CAST(:flags AS text[]),
            ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:gj), 2154))
        )
        ON CONFLICT (run_id, layer_key, fid) DO UPDATE SET
            famille = EXCLUDED.famille,
            geom_kind = EXCLUDED.geom_kind,
            display_name = EXCLUDED.display_name,
            code = EXCLUDED.code,
            libelle_cnig = EXCLUDED.libelle_cnig,
            libelle = EXCLUDED.libelle,
            attributs = EXCLUDED.attributs,
            flags = EXCLUDED.flags,
            geom_2154 = EXCLUDED.geom_2154
        """
    )
    entite_count = 0
    for (layer_key, fid), group in grouped.items():
        merged = _merge_clip([h["clip"] for h in group], existing.get((layer_key, fid)))
        if merged is None:
            continue
        sample = group[0]
        conn.execute(
            upsert_entite,
            {
                "run_id": run_id,
                "fid": fid,
                "layer_key": layer_key,
                "famille": sample["famille"],
                "geom_kind": sample["geom_kind"],
                "display_name": sample.get("display_name"),
                "code": sample.get("code"),
                "libelle_cnig": sample.get("libelle_cnig"),
                "libelle": sample.get("libelle"),
                "attributs": json.dumps(sample.get("attributs") or {}, default=str),
                "flags": _pg_text_array(sample.get("flags") or []),
                "gj": json.dumps(mapping(merged)),
            },
        )
        entite_count += 1

    upsert_lien = text(
        f"""
        INSERT INTO {_SCHEMA}.urba_parcelle_entite (
            run_id, cible_id, cible_type, layer_key, fid, bucket, position,
            surface_m2, pct_cible, longueur_m, nb_points, distance_m
        ) VALUES (
            CAST(:run_id AS uuid), :cible_id, :cible_type, :layer_key, :fid, :bucket, :position,
            :surface_m2, :pct_cible, :longueur_m, :nb_points, :distance_m
        )
        ON CONFLICT (run_id, cible_id, cible_type, layer_key, fid) DO UPDATE SET
            bucket = EXCLUDED.bucket,
            position = EXCLUDED.position,
            surface_m2 = EXCLUDED.surface_m2,
            pct_cible = EXCLUDED.pct_cible,
            longueur_m = EXCLUDED.longueur_m,
            nb_points = EXCLUDED.nb_points,
            distance_m = EXCLUDED.distance_m
        """
    )
    for hit in hits:
        if hit.get("clip") is None:
            continue
        conn.execute(
            upsert_lien,
            {
                "run_id": run_id,
                "cible_id": hit["cible_id"],
                "cible_type": hit["cible_type"],
                "layer_key": hit["layer_key"],
                "fid": hit["fid"],
                "bucket": hit["bucket"],
                "position": hit.get("position"),
                "surface_m2": hit.get("surface_m2"),
                "pct_cible": hit.get("pct_cible"),
                "longueur_m": hit.get("longueur_m"),
                "nb_points": hit.get("nb_points"),
                "distance_m": hit.get("distance_m"),
            },
        )

    upsert_cible = text(
        f"""
        INSERT INTO {_SCHEMA}.urba_cible_resultat (
            run_id, cible_id, cible_type, synthese, flags
        ) VALUES (
            CAST(:run_id AS uuid), :cible_id, 'parcelle',
            CAST(:synthese AS jsonb), CAST(:flags AS text[])
        )
        ON CONFLICT (run_id, cible_id, cible_type) DO UPDATE SET
            synthese = EXCLUDED.synthese,
            flags = EXCLUDED.flags
        """
    )
    for idu, cible in response.parcelles.items():
        conn.execute(
            upsert_cible,
            {
                "run_id": run_id,
                "cible_id": idu,
                "synthese": json.dumps(cible.synthese.model_dump(mode="json"), default=str),
                "flags": _pg_text_array(list(cible.flags or [])),
            },
        )
    return entite_count


def load_urba_geojson(conn, project_id: str, run_id: str) -> dict:
    """Entités retenues, simplifiées à 0,5 m, en WGS84. Une feature par fid."""
    ensure_urba_tables(conn)
    row = conn.execute(
        text(
            f"""
            SELECT COALESCE(
                json_build_object(
                    'type', 'FeatureCollection',
                    'features', COALESCE(json_agg(
                        json_build_object(
                            'type', 'Feature',
                            'id', e.layer_key || ':' || e.fid,
                            'geometry', ST_AsGeoJSON(
                                ST_Transform(
                                    ST_SimplifyPreserveTopology(e.geom_2154, 0.5),
                                    4326
                                )
                            )::json,
                            'properties', json_build_object(
                                'uid', e.layer_key || ':' || e.fid,
                                'fid', e.fid,
                                'layer_key', e.layer_key,
                                'display_name', e.display_name,
                                'famille', e.famille,
                                'geom_kind', e.geom_kind,
                                'code', e.code,
                                'libelle_cnig', e.libelle_cnig,
                                'libelle', e.libelle,
                                'attributs', e.attributs,
                                'flags', to_json(e.flags),
                                'idus', (
                                    SELECT COALESCE(json_agg(p.cible_id ORDER BY p.cible_id), '[]'::json)
                                    FROM {_SCHEMA}.urba_parcelle_entite p
                                    WHERE p.run_id = e.run_id
                                      AND p.layer_key = e.layer_key
                                      AND p.fid = e.fid
                                      AND p.bucket = 'retenu'
                                )
                            )
                        )
                    ), '[]'::json)
                ),
                json_build_object('type', 'FeatureCollection', 'features', '[]'::json)
            )
            FROM {_SCHEMA}.urba_entite e
            JOIN {_SCHEMA}.urba_run r ON r.run_id = e.run_id
            WHERE e.run_id = CAST(:run_id AS uuid)
              AND r.project_id = CAST(:project_id AS uuid)
              AND e.geom_2154 IS NOT NULL
              AND NOT ST_IsEmpty(e.geom_2154)
              AND EXISTS (
                  SELECT 1
                  FROM {_SCHEMA}.urba_parcelle_entite p
                  WHERE p.run_id = e.run_id
                    AND p.layer_key = e.layer_key
                    AND p.fid = e.fid
                    AND p.bucket = 'retenu'
              )
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).scalar()
    if row is None:
        return {"type": "FeatureCollection", "features": []}
    if isinstance(row, str):
        return json.loads(row)
    return row

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_ro_zones_humides_probables.py
=================================

Intersection AOI × geo.zones_humides_probables (raster de probabilité de zones humides)
→ ecocompensation_results.zones_humides_probables (polygones).

- Lit l’AOI depuis ecocompensation.aoi (geom_2154, EPSG:2154).
- Sélectionne les tuiles raster de geo.zones_humides_probables
  qui intersectent l’AOI.
- CLIP les tuiles à l’AOI (ST_Clip) puis vectorise en polygones
  via ST_DumpAsPolygons.
- Stocke le résultat dans ecocompensation_results.zones_humides_probables.
"""

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from dotenv import load_dotenv


def run(engine, project_id: str, aoi_id: str, cb=None) -> int:
    """
    Construit ecocompensation_results.zones_humides_probables pour l'AOI donnée.

    :param engine: Engine SQLAlchemy déjà connecté.
    :param project_id: Identifiant du projet (écrit dans les lignes de résultat).
    :param aoi_id: Identifiant de l'AOI pour la géométrie et les jointures.
    :param cb: Callback de log optionnel (cb(str)).
    :return: Nombre de polygones insérés.
    """
    log = cb or (lambda msg: None)

    # 1) AOI depuis la base (pour CE aoi_id)
    with engine.begin() as conn:
        row_aoi = conn.execute(
            text(
                """
                SELECT id,
                       ST_Area(geom_2154)  AS area_m2
                FROM ecocompensation.aoi
                WHERE id = :aid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

    if row_aoi is None:
        log(
            f"[ZH_PROBA] Aucune AOI trouvée dans ecocompensation.aoi pour id={aoi_id}, exécution annulée."
        )
        return 0

    aoi_area = row_aoi["area_m2"]
    log(f"[ZH_PROBA] AOI id={aoi_id}, surface ~ {aoi_area / 10_000:.2f} ha")

    # 2) Schéma + table results (vecteur)
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results;"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ecocompensation_results.zones_humides_probables (
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    project_id uuid NULL,
                    rid integer NOT NULL,
                    value integer NULL,
                    geom geometry NOT NULL,
                    created_at timestamptz NULL DEFAULT now(),
                    CONSTRAINT zones_humides_probables_pkey PRIMARY KEY (id)
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS zones_humides_probables_geom_gix
                    ON ecocompensation_results.zones_humides_probables
                    USING GIST (geom);
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS zones_humides_probables_project_idx
                    ON ecocompensation_results.zones_humides_probables (project_id);
                """
            )
        )
        # Nettoyage des anciennes géométries pour ce projet
        conn.execute(
            text(
                """
                DELETE FROM ecocompensation_results.zones_humides_probables
                WHERE project_id = :pid;
                """
            ),
            {"pid": project_id},
        )

    # 3) Comptage + liste des tuiles raster intersectantes
    with engine.begin() as conn:
        count_tiles = conn.execute(
            text(
                """
                SELECT count(*)
                FROM geo.zones_humides_probables r
                JOIN ecocompensation.aoi a
                  ON a.id = :aid
                WHERE ST_Intersects(
                    r.rast,
                    a.geom_2154
                );
                """
            ),
            {"aid": aoi_id},
        ).scalar_one()
        tile_rows = conn.execute(
            text(
                """
                SELECT r.rid
                FROM geo.zones_humides_probables r
                JOIN ecocompensation.aoi a
                  ON a.id = :aid
                WHERE ST_Intersects(
                    r.rast,
                    a.geom_2154
                )
                ORDER BY r.rid;
                """
            ),
            {"aid": aoi_id},
        ).mappings().all()
        tile_rids = [int(r["rid"]) for r in tile_rows]

    log(
        f"[ZH_PROBA] {count_tiles} tuiles raster intersectent l'AOI (avant vectorisation)."
    )

    if count_tiles == 0:
        log("[ZH_PROBA] Rien à vectoriser.")
        return 0

    # 4) Vectorisation par tuile : ST_Clip + ST_DumpAsPolygons
    t0 = time.perf_counter()
    rows = 0
    n_empty_tiles = 0
    n_non_empty_tiles = 0
    total_tiles = len(tile_rids)
    for i, rid in enumerate(tile_rids, 1):
        tile_t0 = time.perf_counter()
        with engine.begin() as conn:
            # La vectorisation raster peut être longue sur de grandes AOI :
            # on relève le timeout local à la transaction.
            conn.execute(text("SET LOCAL statement_timeout = '15min';"))
            res = conn.execute(
                text(
                    """
                    INSERT INTO ecocompensation_results.zones_humides_probables (project_id, rid, value, geom)
                    SELECT
                        :project_id AS project_id,
                        r.rid,
                        (p).val::integer AS value,
                        (p).geom::geometry(Polygon, 2154) AS geom
                    FROM geo.zones_humides_probables r
                    JOIN ecocompensation.aoi a
                      ON a.id = :aoi_id
                    CROSS JOIN LATERAL ST_DumpAsPolygons(
                        ST_Clip(
                            r.rast,
                            1,
                            a.geom_2154,
                            true
                        )
                    ) AS p
                    WHERE r.rid = :rid
                      AND (p).val IS NOT NULL
                      AND (p).val <> ST_BandNoDataValue(r.rast, 1);
                    """
                ),
                {"project_id": project_id, "aoi_id": aoi_id, "rid": rid},
            )
        n_tile = res.rowcount or 0
        rows += n_tile
        if n_tile == 0:
            n_empty_tiles += 1
            log(
                f"[ZH_PROBA] Tuile {i}/{total_tiles} (rid={rid}) : vide après clip "
                f"(0 polygone) en {time.perf_counter() - tile_t0:.2f} s "
                f"(tuiles avec données={n_non_empty_tiles}, vides={n_empty_tiles}, cumul={rows})."
            )
        else:
            n_non_empty_tiles += 1
            log(
                f"[ZH_PROBA] Tuile {i}/{total_tiles} (rid={rid}) vectorisée : "
                f"{n_tile} polygones en {time.perf_counter() - tile_t0:.2f} s "
                f"(tuiles avec données={n_non_empty_tiles}, vides={n_empty_tiles}, cumul={rows})."
            )
    t1 = time.perf_counter()

    log(
        f"[RESULTS ZH_PROBA] {rows} polygones insérés dans ecocompensation_results.zones_humides_probables "
        f"(en {t1 - t0:.2f} s) | tuiles avec données={n_non_empty_tiles}, tuiles vides={n_empty_tiles}."
    )
    return rows


def main():
    """
    Entrée CLI : construit son propre engine et utilise la dernière AOI.
    """
    BASE_DIR = Path(__file__).resolve().parents[2]
    load_dotenv(BASE_DIR / ".env")

    SUPABASE_HOST = os.getenv("SUPABASE_HOST")
    SUPABASE_PORT = os.getenv("SUPABASE_PORT", "6543")
    SUPABASE_DB = os.getenv("SUPABASE_DB", "postgres")
    SUPABASE_USER = os.getenv("SUPABASE_USER")
    SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")

    if not all([SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD]):
        raise RuntimeError(
            "Variables de connexion à la base manquantes dans le .env (ZH_PROBA)."
        )

    password_quoted = quote_plus(SUPABASE_PASSWORD)
    db_url = (
        f"postgresql+psycopg://{SUPABASE_USER}:{password_quoted}"
        f"@{SUPABASE_HOST}:{SUPABASE_PORT}/{SUPABASE_DB}"
    )
    engine = create_engine(db_url)

    # Dernier projet et son AOI
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT id, aoi_id FROM ecocompensation.projects "
                "WHERE aoi_id IS NOT NULL ORDER BY created_at DESC LIMIT 1;"
            )
        ).mappings().one_or_none()
    if not row:
        print("Aucun projet avec AOI trouvé.")
        return
    project_id = str(row["id"])
    aoi_id = str(row["aoi_id"])

    n = run(engine, project_id, aoi_id, cb=print)
    print(f"Total polygones de zones humides probables insérés : {n}")


if __name__ == "__main__":
    main()


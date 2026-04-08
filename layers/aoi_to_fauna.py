#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aoi_to_fauna.py
==============

Pipeline "FAUNA v2" :
- Lit la table source `ecocompensation.fauna`
- Joint `ecocompensation.fauna_taxa_ref` sur `nom_vernaculaire` = `tax` pour
  `niveau_patrimonialite` et `protection_nationale`
- (optionnel) filtre par liste d'espèces (`nom_vernaculaire`)
- Insère dans `ecocompensation_results.fauna` (une seule table)
- Conserve tous les types de géométries (ponctuel/linéaire/surfacique + multi-*)

Signature attendue par le moteur de couches :
    run(engine, project_id: str, aoi_id: str, cb=None, species_list: list[str] | None = None) -> int
"""

from __future__ import annotations

from sqlalchemy import text


def _ensure_results_table(conn, dst_table: str) -> None:
    # 1) Créer le schéma / table
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS ecocompensation_results"))

    # On copie la structure de la table source (colonnes + types).
    # Ensuite on renomme `geometry` -> `geom_2154` pour coller aux conventions
    # de nos filtres (ST_Intersects(..., f.geom_2154)).
    conn.execute(
        text(f"CREATE TABLE IF NOT EXISTS {dst_table} (LIKE ecocompensation.fauna INCLUDING DEFAULTS)")
    )

    # 2) Normaliser le nom de colonne géométrique : geometry -> geom_2154
    has_geom_2154 = conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'ecocompensation_results'
                  AND table_name = :t
                  AND lower(column_name) = 'geom_2154'
            )
            """
        ),
        {"t": dst_table.split(".")[-1]},
    ).scalar_one()

    has_geometry = conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'ecocompensation_results'
                  AND table_name = :t
                  AND lower(column_name) = 'geometry'
            )
            """
        ),
        {"t": dst_table.split(".")[-1]},
    ).scalar_one()

    if not has_geom_2154 and has_geometry:
        conn.execute(text(f"ALTER TABLE {dst_table} RENAME COLUMN geometry TO geom_2154"))

    # 3) Colonnes de suivi
    conn.execute(
        text(
            f"""
            ALTER TABLE {dst_table}
            ADD COLUMN IF NOT EXISTS id uuid DEFAULT gen_random_uuid(),
            ADD COLUMN IF NOT EXISTS project_id uuid,
            ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now()
            """
        )
    )

    # 4) Index
    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{dst_table.split('.')[-1]}_project_id
            ON {dst_table}(project_id)
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{dst_table.split('.')[-1]}_geom
            ON {dst_table}
            USING GIST (geom_2154)
            """
        )
    )

    # Colonnes enrichies depuis fauna_taxa_ref (pas présentes dans ecocompensation.fauna)
    conn.execute(
        text(
            f"""
            ALTER TABLE {dst_table}
            ADD COLUMN IF NOT EXISTS niveau_patrimonialite text,
            ADD COLUMN IF NOT EXISTS protection_nationale character varying(50)
            """
        )
    )


def run(
    engine,
    project_id: str,
    aoi_id: str,
    cb=None,
    *,
    species_list: list[str] | None = None,
) -> int:
    """
    Retourne : nombre de lignes insérées.
    """

    log = cb or (lambda _msg: None)
    dst_table = "ecocompensation_results.fauna"

    normalized_species: list[str] = []
    if species_list:
        normalized_species = [s.strip().lower() for s in species_list if s and s.strip()]

    with engine.begin() as conn:
        has_fauna_taxa_ref = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL").execution_options(no_prepare=True),
            {"r": "ecocompensation.fauna_taxa_ref"},
        ).scalar_one()

        aoi = conn.execute(
            text(
                """
                SELECT ST_AsText(geom_2154) AS wkt_aoi
                FROM ecocompensation.aoi
                WHERE id = :aid
                """
            ),
            {"aid": aoi_id},
        ).mappings().one_or_none()

        if not aoi:
            log(f"[FAUNA] AOI {aoi_id} introuvable.")
            return 0

        _ensure_results_table(conn, dst_table)

        # Evite doublons si on relance le fetch pour le même projet.
        conn.execute(
            text(f"DELETE FROM {dst_table} WHERE project_id = :pid"),
            {"pid": project_id},
        )

        # Liste de colonnes à insérer (structure de ecocompensation.fauna)
        # NB: `geometry` a été renommée en `geom_2154` dans la table de résultats.
        insert_cols = """
            id_releve, id_obs, date_debut, date_fin,
            classe, ordre, famille, cd_ref,
            nom_cite, nom_taxref, nom_vernaculaire,
            niveau_patrimonialite,
            protection_nationale,
            geom_id, geom_type, geom_2154,
            annee_obs, lon, lat,
            project_id
        """

        if has_fauna_taxa_ref:
            ref_niveau = "tr.niveau_patrimonialite"
            ref_prot = "tr.protection_nationale"
            from_join = """
                FROM ecocompensation.fauna f
                CROSS JOIN aoi
                LEFT JOIN ecocompensation.fauna_taxa_ref tr
                  ON lower(btrim(f.nom_vernaculaire::text)) = lower(btrim(tr.tax::text))
            """
        else:
            ref_niveau = "NULL::text"
            ref_prot = "NULL::character varying"
            from_join = """
                FROM ecocompensation.fauna f
                CROSS JOIN aoi
            """

        select_cols = f"""
            f.id_releve, f.id_obs, f.date_debut, f.date_fin,
            f.classe, f.ordre, f.famille, f.cd_ref,
            f.nom_cite, f.nom_taxref, f.nom_vernaculaire,
            {ref_niveau},
            {ref_prot},
            f.geom_id, f.geom_type, f.geometry AS geom_2154,
            f.annee_obs, f.lon, f.lat,
            :pid
        """

        species_where_sql = ""
        params = {"pid": project_id, "wkt_aoi": aoi["wkt_aoi"]}
        if normalized_species:
            # Filtrage case-insensitive sur le nom vernaculaire.
            species_where_sql = """
                AND lower(btrim(f.nom_vernaculaire::text)) = ANY(CAST(:species_list AS text[]))
            """
            params["species_list"] = normalized_species

        log("[FAUNA] Insertion ecocompensation.fauna -> ecocompensation_results.fauna (intersection AOI)…")
        res = conn.execute(
            text(
                f"""
                WITH aoi AS (
                    SELECT ST_GeomFromText(:wkt_aoi, 2154) AS geom
                )
                INSERT INTO {dst_table} ({insert_cols})
                SELECT {select_cols}
                {from_join}
                WHERE f.geometry && aoi.geom
                  AND ST_Intersects(f.geometry, aoi.geom)
                  {species_where_sql}
                """
            ),
            params,
        )

    return int(res.rowcount or 0)


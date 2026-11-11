"""
tables.py — tables de travail et de résultat des unités foncières.

uf_parcelles : une ligne par parcelle candidate (PM de l'AOI ou ajout manuel),
               avec ses booléens de critères et, si elle est retirée, la raison.
uf_terrains  : une ligne par terrain évalué (retenu ou non) : verdict + détail.

Les anciennes tables unites_foncieres / sous_ensembles ne sont plus écrites.
"""
from __future__ import annotations

import json

from sqlalchemy import text

P = "ecocompensation_results.uf_parcelles"
T = "ecocompensation_results.uf_terrains"

DDL = f"""
CREATE SCHEMA IF NOT EXISTS ecocompensation_results;

CREATE TABLE IF NOT EXISTS {P} (
    id              bigserial PRIMARY KEY,
    project_id      uuid NOT NULL,
    source          text NOT NULL DEFAULT 'pm',          -- 'pm' | 'manuel'
    uf_id           text,
    idu             text NOT NULL,
    siren           text,
    denomination    text,
    forme_juridique text,
    surface_m2      double precision NOT NULL DEFAULT 0,
    geom_2154       geometry(Geometry, 2154) NOT NULL,
    eliminee_par    text,                                -- NULL = parcelle active
    criteres        jsonb NOT NULL DEFAULT '{{}}',
    zh_m2           double precision NOT NULL DEFAULT 0,
    fauna_distances jsonb NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_ufp_project_source ON {P} (project_id, source);
CREATE INDEX IF NOT EXISTS idx_ufp_project_uf     ON {P} (project_id, uf_id);
CREATE INDEX IF NOT EXISTS idx_ufp_geom           ON {P} USING GIST (geom_2154);

CREATE TABLE IF NOT EXISTS {T} (
    project_id      uuid NOT NULL,
    uf_id           text NOT NULL,
    source          text NOT NULL DEFAULT 'pm',
    siren           text,
    denomination    text,
    forme_juridique text,
    nb_parcelles    integer,
    idus            text[] NOT NULL DEFAULT '{{}}',
    surface_ha      double precision,
    miller          double precision,
    dist_centre_m   double precision,
    geom_2154       geometry(Geometry, 2154),
    verdict         boolean NOT NULL DEFAULT false,
    motif_rejet     text,
    criteres        jsonb NOT NULL DEFAULT '{{}}',
    veg_libelles    text[] NOT NULL DEFAULT '{{}}',
    veg_profile     jsonb NOT NULL DEFAULT '{{}}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, uf_id)
);
CREATE INDEX IF NOT EXISTS idx_uft_geom ON {T} USING GIST (geom_2154);
"""

# Contiguïté : partage de bord ou de sommet. (L'ancien « OR ST_Relate 'F***1****' »
# était redondant : c'est un cas particulier de ST_Touches.)
CONTACT_SQL = "ST_Touches(a.geom_2154, b.geom_2154)"

UPSERT_TERRAINS_SQL = f"""
INSERT INTO {T} (project_id, uf_id, source, siren, denomination, forme_juridique,
                 nb_parcelles, idus, surface_ha, miller, dist_centre_m, geom_2154,
                 verdict, motif_rejet, criteres)
SELECT CAST(:pid AS uuid), r.uf_id, r.source, r.siren, r.denomination, r.forme_juridique,
       r.nb_parcelles, ARRAY(SELECT jsonb_array_elements_text(r.idus)),
       r.surface_ha, r.miller, r.dist_centre_m, ST_GeomFromText(r.wkt, 2154),
       r.verdict, r.motif_rejet, r.criteres
FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
    uf_id text, source text, siren text, denomination text, forme_juridique text,
    nb_parcelles int, idus jsonb, surface_ha float8, miller float8, dist_centre_m float8,
    wkt text, verdict bool, motif_rejet text, criteres jsonb)
ON CONFLICT (project_id, uf_id) DO UPDATE SET
    nb_parcelles = EXCLUDED.nb_parcelles, idus = EXCLUDED.idus,
    surface_ha = EXCLUDED.surface_ha, miller = EXCLUDED.miller,
    dist_centre_m = EXCLUDED.dist_centre_m, geom_2154 = EXCLUDED.geom_2154,
    verdict = EXCLUDED.verdict, motif_rejet = EXCLUDED.motif_rejet,
    criteres = EXCLUDED.criteres
"""


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(DDL))


def exec_recordset(conn, sql: str, rows: list[dict], extra: dict | None = None,
                   batch: int = 1000) -> None:
    """Un INSERT/UPDATE par lot via jsonb_to_recordset (pas d'executemany : pgBouncer)."""
    for i in range(0, len(rows), batch):
        conn.execute(text(sql), {**(extra or {}),
                                 "rows": json.dumps(rows[i:i + batch], default=str)})


def set_statement_timeout(conn, value: str = "5min") -> None:
    conn.execute(text(f"SET LOCAL statement_timeout = '{value}'"))

-- ─── Référence nationale : ecocompensation.remontee_de_nappes ───────────────
-- Migré depuis geo.remontees_nappes (adapter le SRID si geometry n’est pas en 2154).
-- Vérifier après migration : SELECT DISTINCT classefiab FROM ecocompensation.remontee_de_nappes;

CREATE SCHEMA IF NOT EXISTS ecocompensation;

CREATE TABLE IF NOT EXISTS ecocompensation.remontee_de_nappes (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  source_index bigint NULL,
  classe text NULL,
  fiab_mnt text NULL,
  fiab_eso text NULL,
  fiab_tot text NULL,
  classefiab text NULL,
  gridcode bigint NULL,
  geom_2154 geometry(Geometry, 2154) NOT NULL,
  created_at timestamptz NULL DEFAULT now(),
  CONSTRAINT remontee_de_nappes_pkey PRIMARY KEY (id)
) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_remontee_de_nappes_geom
  ON ecocompensation.remontee_de_nappes USING gist (geom_2154) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_remontee_de_nappes_source_index
  ON ecocompensation.remontee_de_nappes USING btree (source_index) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_remontee_de_nappes_classefiab
  ON ecocompensation.remontee_de_nappes USING btree (classefiab) TABLESPACE pg_default;

-- Migration depuis geo.remontees_nappes (à lancer une fois les données sources prêtes)
-- Ajuster la ligne geom_2154 si le SRID source est connu (ex. 2154, 4171, etc.).
/*
INSERT INTO ecocompensation.remontee_de_nappes (
  source_index,
  classe,
  fiab_mnt,
  fiab_eso,
  fiab_tot,
  classefiab,
  gridcode,
  geom_2154
)
SELECT
  rn.index,
  rn."CLASSE",
  rn."FIAB_MNT",
  rn."FIAB_ESO",
  rn."FIAB_TOT",
  rn."CLASSEFIAB",
  rn.gridcode,
  CASE
    WHEN rn.geometry IS NULL THEN NULL::geometry
    WHEN ST_SRID(rn.geometry) = 2154 THEN rn.geometry::geometry(Geometry, 2154)
    WHEN ST_SRID(rn.geometry) = 0 THEN ST_SetSRID(rn.geometry, 2154)::geometry(Geometry, 2154)
    ELSE ST_Transform(rn.geometry, 2154)::geometry(Geometry, 2154)
  END AS geom_2154
FROM geo.remontees_nappes rn
WHERE rn.geometry IS NOT NULL
  AND NOT ST_IsEmpty(rn.geometry);
*/

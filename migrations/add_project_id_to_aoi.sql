-- Migration: rattacher AOI au projet via project_id
-- Objectif:
-- 1) Ajouter ecocompensation.aoi.project_id
-- 2) Backfill depuis ecocompensation.projects.aoi_id
-- 3) Poser une contrainte d'unicité (1 AOI <-> 1 projet) quand possible

ALTER TABLE ecocompensation.aoi
  ADD COLUMN IF NOT EXISTS project_id uuid;

UPDATE ecocompensation.aoi a
SET project_id = p.id
FROM ecocompensation.projects p
WHERE p.aoi_id = a.id
  AND a.project_id IS NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'aoi_project_id_fkey'
  ) THEN
    ALTER TABLE ecocompensation.aoi
      ADD CONSTRAINT aoi_project_id_fkey
      FOREIGN KEY (project_id) REFERENCES ecocompensation.projects(id)
      ON DELETE CASCADE;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_aoi_project_id
  ON ecocompensation.aoi (project_id)
  WHERE project_id IS NOT NULL;

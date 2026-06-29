-- Migration : typologie d'étude (faune buffer vs zones humides intra-foncier)
-- À exécuter manuellement sur Supabase / PostgreSQL.

ALTER TABLE ecocompensation.projects
    ADD COLUMN IF NOT EXISTS study_type text NOT NULL DEFAULT 'faune_buffer';

ALTER TABLE ecocompensation.projects
    DROP CONSTRAINT IF EXISTS projects_study_type_check;

ALTER TABLE ecocompensation.projects
    ADD CONSTRAINT projects_study_type_check
    CHECK (study_type IN ('faune_buffer', 'zones_humides_intra'));

COMMENT ON COLUMN ecocompensation.projects.study_type IS
    'Méthodologie : faune_buffer = recherche dans un buffer autour du foncier ; zones_humides_intra = recherche à l''intérieur du foncier uploadé.';

CREATE INDEX IF NOT EXISTS idx_projects_study_type
    ON ecocompensation.projects (study_type);

-- Projets existants : conserver faune_buffer (défaut de la colonne).

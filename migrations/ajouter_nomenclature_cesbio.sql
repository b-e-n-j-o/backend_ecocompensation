-- 1. Ajout de la colonne pour le libellé si elle n'existe pas encore
ALTER TABLE ecocompensation.cesbio 
ADD COLUMN IF NOT EXISTS libelle_classe text;

-- 2. Mise à jour massive de l'étiquetage
UPDATE ecocompensation.cesbio
SET libelle_classe = CASE classe
    WHEN 1  THEN 'Bâtis denses'
    WHEN 2  THEN 'Bâtis diffus'
    WHEN 3  THEN 'Zones industrielles et commerciales'
    WHEN 4  THEN 'Surfaces routes'
    WHEN 5  THEN 'Colza'
    WHEN 6  THEN 'Céréales à pailles'
    WHEN 7  THEN 'Protéagineux'
    WHEN 8  THEN 'Soja'
    WHEN 9  THEN 'Tournesol'
    WHEN 10 THEN 'Maïs'
    WHEN 11 THEN 'Riz'
    WHEN 12 THEN 'Tubercules/racines'
    WHEN 13 THEN 'Prairies'
    WHEN 14 THEN 'Vergers'
    WHEN 15 THEN 'Vignes'
    WHEN 16 THEN 'Forêts de feuillus'
    WHEN 17 THEN 'Forêts de conifères'
    WHEN 18 THEN 'Pelouses'
    WHEN 19 THEN 'Landes ligneuses'
    WHEN 20 THEN 'Surfaces minérales'
    WHEN 21 THEN 'Plages et dunes'
    WHEN 22 THEN 'Glaciers ou neiges' -- Ajouté pour la cohérence de la suite 1-24
    WHEN 23 THEN 'Eau'
    WHEN 24 THEN 'Autres'
    ELSE 'Inconnu'
END;

-- 3. Indexation de la nouvelle colonne pour accélérer les futurs filtrages
CREATE INDEX IF NOT EXISTS idx_ocs_libelle_classe ON ecocompensation.cesbio (libelle_classe);
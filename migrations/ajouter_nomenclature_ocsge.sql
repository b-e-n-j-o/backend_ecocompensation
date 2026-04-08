-- 1. Ajout des colonnes de libellés
ALTER TABLE ecocompensation.ocs_ge 
ADD COLUMN IF NOT EXISTS libelle_cs text,
ADD COLUMN IF NOT EXISTS libelle_us text;

-- 2. Mise à jour unique (CS et US en même temps)
UPDATE ecocompensation.ocs_ge
SET 
    libelle_cs = CASE 
        -- Couverture du Sol (CS)
        WHEN code_cs = 'CS1.1.1.1' THEN 'Zones bâties'
        WHEN code_cs = 'CS1.1.1.2' THEN 'Zones non bâties'
        WHEN code_cs = 'CS1.1.2.1' THEN 'Matériaux minéraux'
        WHEN code_cs = 'CS1.1.2.2' THEN 'Matériaux composites'
        WHEN code_cs = 'CS1.2.1'   THEN 'Sols nus'
        WHEN code_cs = 'CS1.2.2'   THEN 'Surfaces d''eau'
        WHEN code_cs = 'CS1.2.3'   THEN 'Névés et glaciers'
        WHEN code_cs = 'CS2.1.1.1' THEN 'Feuillus'
        WHEN code_cs = 'CS2.1.1.2' THEN 'Conifères'
        WHEN code_cs = 'CS2.1.1.3' THEN 'Mixte'
        WHEN code_cs = 'CS2.1.2'   THEN 'Formations arbustives, sous-arbrisseaux'
        WHEN code_cs = 'CS2.1.3'   THEN 'Autres formations ligneuses'
        WHEN code_cs = 'CS2.2.1'   THEN 'Formations herbacées'
        WHEN code_cs = 'CS2.2.2'   THEN 'Autres formations non ligneuses'
        ELSE 'Inconnu (' || code_cs || ')'
    END,
    libelle_us = CASE 
        -- Usage du Sol (US)
        WHEN code_us = 'US1.1'   THEN 'Agriculture'
        WHEN code_us = 'US1.2'   THEN 'Sylviculture'
        WHEN code_us = 'US1.3'   THEN 'Activité d''extraction'
        WHEN code_us = 'US1.4'   THEN 'Pêche et aquaculture'
        WHEN code_us = 'US1.5'   THEN 'Autres prod. primaires'
        WHEN code_us = 'US2'     THEN 'Production secondaire'
        WHEN code_us = 'US3'     THEN 'Production tertiaire'
        WHEN code_us = 'US235'   THEN 'Usage mixte (US 2, US 3, US 5)'
        WHEN code_us = 'US4.1.1' THEN 'Routier'
        WHEN code_us = 'US4.1.2' THEN 'Ferré'
        WHEN code_us = 'US4.1.3' THEN 'Aérien'
        WHEN code_us = 'US4.1.4' THEN 'Navigable'
        WHEN code_us = 'US4.1.5' THEN 'Autres réseaux'
        WHEN code_us = 'US4.2'   THEN 'Services logistiques et de stockage'
        WHEN code_us = 'US4.3'   THEN 'Réseaux d''utilité publique'
        WHEN code_us = 'US5'     THEN 'Usage résidentiel'
        WHEN code_us = 'US6.1'   THEN 'Zones en transition'
        WHEN code_us = 'US6.2'   THEN 'Zones abandonnées'
        WHEN code_us = 'US6.3'   THEN 'Sans usage'
        WHEN code_us = 'US6.6'   THEN 'Inconnu'
        ELSE 'Inconnu (' || code_us || ')'
    END;

-- 3. Indexation pour les performances de filtrage futur
CREATE INDEX IF NOT EXISTS idx_ocs_ge_libelle_cs ON ecocompensation.ocs_ge (libelle_cs);
CREATE INDEX IF NOT EXISTS idx_ocs_ge_libelle_us ON ecocompensation.ocs_ge (libelle_us);
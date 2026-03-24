-- Propriétaire personne morale (aligné sur ecocompensation_results.unites_foncieres)
ALTER TABLE ecocompensation_results.sous_ensembles
  ADD COLUMN IF NOT EXISTS denomination text,
  ADD COLUMN IF NOT EXISTS siren text;

COMMENT ON COLUMN ecocompensation_results.sous_ensembles.denomination IS 'Raison sociale du propriétaire moral (PM)';
COMMENT ON COLUMN ecocompensation_results.sous_ensembles.siren IS 'SIREN du propriétaire moral';

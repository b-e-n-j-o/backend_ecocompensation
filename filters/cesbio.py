"""
Nomenclature CESBIO (OCS-GE) — libellés alignés sur
``migrations/ajouter_nomenclature_cesbio.sql`` (CASE classe → libelle_classe).

Utilisé par le filtre « Couverture sol CESBIO » (intersection parcelle / sous-ensemble).
"""
from __future__ import annotations

# Ordre stable pour UI (liste déroulante, etc.)
CESBIO_LIBELLES: tuple[str, ...] = (
    "Bâtis denses",
    "Bâtis diffus",
    "Zones industrielles et commerciales",
    "Surfaces routes",
    "Colza",
    "Céréales à pailles",
    "Protéagineux",
    "Soja",
    "Tournesol",
    "Maïs",
    "Riz",
    "Tubercules/racines",
    "Prairies",
    "Vergers",
    "Vignes",
    "Forêts de feuillus",
    "Forêts de conifères",
    "Pelouses",
    "Landes ligneuses",
    "Surfaces minérales",
    "Plages et dunes",
    "Glaciers ou neiges",
    "Eau",
    "Autres",
    "Inconnu",  # ELSE du CASE + valeurs hors plage
)

# -*- coding: utf-8 -*-
"""Texte d'accompagnement glissé dans les archives SIG."""

from __future__ import annotations

import zipfile

README_NAME = "LISEZMOI.txt"

_PARCELLES = """\
Parcelles du classement
=======================

Ce dossier contient les parcelles retenues par la recherche de foncier
(les parcelles marquées indésirables n'y figurent pas).

Fichiers
--------
- parcelles.shp (et .shx, .dbf, .prj, .cpg) : une forme par parcelle.
  Les textes longs sont coupés à 254 caractères, limite du format shapefile.
- parcelles.gpkg : le même contenu, sans cette coupure, plus la zone du
  projet et l'aire d'étude quand elles existent. À ouvrir de préférence
  dans QGIS. Projection : Lambert 93 (EPSG:2154).

Ce que représente une ligne
---------------------------
Une ligne = une parcelle cadastrale du classement.

Les colonnes reprennent les informations déjà affichées dans le tableau
de l'étude : identifiant cadastral, surface, distance au projet, score,
occupation du sol, distances à l'eau et aux espèces, propriétaire
personne morale lorsque il est connu.

Les noms de colonnes sont courts (10 caractères au plus) pour rester
lisibles dans un shapefile. Le GeoPackage utilise les mêmes noms.
"""

_INDESIRABLES = """\
Parcelles indésirables
======================

Ce dossier contient les parcelles que vous avez retirées du classement.

Fichiers
--------
- indesirables.shp (et fichiers associés) : une forme par parcelle,
  textes coupés à 254 caractères.
- indesirables.gpkg : le même contenu sans cette coupure, plus la zone
  du projet et l'aire d'étude. Projection : Lambert 93 (EPSG:2154).

Une ligne = une parcelle cadastrale marquée indésirable. Les colonnes
sont les mêmes que pour l'export des parcelles du classement.
"""

_UF = """\
Unités foncières (personnes morales)
====================================

Une unité foncière regroupe les parcelles cadastrales d'une même société
(personne morale) lorsqu'elles se touchent. Ce n'est pas un îlot agricole :
l'îlot décrit l'exploitation déclarée à la PAC, l'unité foncière décrit
la propriété.

Fichiers shapefile (textes coupés à 254 caractères)
---------------------------------------------------
- unites_foncieres.shp : une forme par unité. C'est l'union des parcelles
  de la société.
- parcelles.shp : une forme par parcelle cadastrale membre d'une unité.
  La colonne uf_id relie la parcelle à son unité.

Fichier GeoPackage (uf.gpkg), à privilégier dans QGIS
-----------------------------------------------------
Projection : Lambert 93 (EPSG:2154).

- unites_foncieres : l'unité, une ligne.
- parcelles : les parcelles qui la composent (mêmes colonnes que l'export
  parcelles, plus uf_id, uf_rang et uf_type).
- dictionnaire : table sans carte. Elle explique chaque colonne de
  l'unité (surface, occupation du sol, distances, scores) et comment
  la valeur a été obtenue à partir des parcelles.
- zone_projet et aire_etude, si elles existent.

Comment lire les chiffres de l'unité
-------------------------------------
Ils portent sur l'ensemble des parcelles réunies, pas sur une moyenne
brute.

- Les surfaces (et les surfaces en zone humide ou en eau) sont additionnées.
- L'occupation du sol est recalculée en pondérant chaque parcelle par
  sa surface.
- Pour la faune et l'eau, on garde la distance la plus courte parmi
  les parcelles de l'unité.
- Le score de dureté retenu est le plus élevé (la parcelle la plus
  contrainte).
- Le score composite est une moyenne pondérée par la surface.
- Le SIREN et le nom de la société viennent du classement des unités,
  pas d'un mélange des propriétaires parcelle par parcelle.
- La forme de l'unité (indice de Miller) est recalculée sur le polygone
  fusionné.

Les colonnes de type texte long (détail des scores, par exemple) listent
le détail de chaque parcelle, précédé de son identifiant cadastral.
"""

TEXTS = {
    "parcelles": _PARCELLES,
    "indesirables": _INDESIRABLES,
    "uf": _UF,
}


def add_export_readme(zf: zipfile.ZipFile, kind: str) -> None:
    text = TEXTS.get(kind)
    if not text:
        raise ValueError(f"readme inconnu: {kind}")
    zf.writestr(README_NAME, text.encode("utf-8"))

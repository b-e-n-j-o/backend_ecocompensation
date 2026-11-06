"""Nom de la table d'occupation du sol.

``ecocompensation.vegetation_sur_cesbio`` est une partition : BD TOPO prioritaire,
CESBIO seulement là où il n'y a pas de BD TOPO. Les géométries sont des dalles
(Polygon, quelques centaines de sommets). Sur une parcelle, la somme des % de
classes fait 100 %.

La table temporaire ``vegetation_sur_cesbio_sub`` n'existe plus. Filtre, surfaces
et listes de libellés lisent ``VEG_TABLE``.
"""

VEG_TABLE = "ecocompensation.vegetation_sur_cesbio"
# Alias conservés : tout le code lit le même nom.
VEG_TABLE_CALC = VEG_TABLE
VEG_TABLE_DISPLAY = VEG_TABLE

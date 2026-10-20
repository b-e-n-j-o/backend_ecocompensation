"""
config.py — Catalogue des couches Géoportail de l'Urbanisme (GPU) interrogées
et paramètres par défaut du profiler urba.

Une "couche" = un typeName WFS (ex. wfs_du:zone_urba). Chaque couche porte :
- key           : identifiant stable côté Kerelia (utilisé dans la réponse API / le front)
- typename      : nom de la couche dans le flux WFS data.geopf.fr
- famille       : zonage | prescription | information | servitude
- geom_kind     : surf | lin | pct (pilote le calcul de mesure)
- fields        : champs conservés (dans cet ordre) -> libellé propre affiché
- group_by      : champs servant à fusionner les entités identiques (une ligne
                  de rapport par valeur), ex. une zone UA découpée en 12 polygones
- code_fields   : (type, sous-type) pour retrouver le libellé CNIG
- enabled       : couche interrogée par défaut ou non
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

WFS_URL = "https://data.geopf.fr/wfs/ows"

# Limite GPU : 5000 entités par requête -> au-delà on subdivise la bbox.
WFS_MAX_FEATURES = 5000


@dataclass(frozen=True)
class LayerDef:
    key: str
    typename: str
    display_name: str
    famille: str  # zonage | prescription | information | servitude
    geom_kind: str  # surf | lin | pct
    fields: Dict[str, str]
    group_by: Tuple[str, ...]
    code_fields: Optional[Tuple[str, str]] = None
    code_nomenclature: Optional[str] = None  # psc | inf | typezone | typesect
    enabled: bool = True


# ---------------------------------------------------------------------------
# Champs communs
# ---------------------------------------------------------------------------
_DOC_FIELDS = {
    "idurba": "Identifiant du document",
    "partition": "Partition GPU",
    "datvalid": "Date de validation",
    "insee": "INSEE",
}

_ZONE_FIELDS = {
    "libelle": "Zone",
    "libelong": "Libellé long",
    "typezone": "Type de zone",
    "destdomi": "Destination dominante",
    "formdomi": "Forme dominante",
    "destoui": "Destinations autorisées",
    "destcdt": "Destinations sous conditions",
    "destnon": "Destinations interdites",
    "nomfic": "Fichier règlement",
    "urlfic": "Lien règlement",
    "datappro": "Date d'approbation",
    **_DOC_FIELDS,
}

_PSC_FIELDS = {
    "libelle": "Libellé",
    "txt": "Étiquette",
    "typepsc": "Type",
    "stypepsc": "Sous-type",
    "nature": "Nature",
    "nomfic": "Fichier",
    "urlfic": "Lien",
    **_DOC_FIELDS,
}

_INF_FIELDS = {
    "libelle": "Libellé",
    "txt": "Étiquette",
    "typeinf": "Type",
    "stypeinf": "Sous-type",
    "nomfic": "Fichier",
    "urlfic": "Lien",
    **_DOC_FIELDS,
}

_SUP_FIELDS = {
    "suptype": "Catégorie SUP",
    "nomsuplitt": "Servitude",
    "nomass": "Nom assiette",
    "typeass": "Type assiette",
    "idass": "Id assiette",
    "idgen": "Id générateur",
    "nomreg": "Nom de l'acte",
    "urlreg": "Lien acte",
    "fichier": "Fichier source",
    "partition": "Partition GPU",
}

_PSC_GROUP = ("idurba", "typepsc", "stypepsc", "libelle", "txt")
_INF_GROUP = ("idurba", "typeinf", "stypeinf", "libelle", "txt")
_SUP_GROUP = ("suptype", "idass", "nomass")

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
LAYERS: List[LayerDef] = [
    # --- Zonage ---
    LayerDef("zonage_plu", "wfs_du:zone_urba", "Zonage PLU / PLUi", "zonage", "surf",
             _ZONE_FIELDS, ("idurba", "libelle", "typezone"),
             code_fields=("typezone", ""), code_nomenclature="typezone"),
    # --- Prescriptions ---
    LayerDef("prescriptions_surf", "wfs_du:prescription_surf", "Prescriptions surfaciques", "prescription", "surf",
             _PSC_FIELDS, _PSC_GROUP, code_fields=("typepsc", "stypepsc"), code_nomenclature="psc"),
    LayerDef("prescriptions_lin", "wfs_du:prescription_lin", "Prescriptions linéaires", "prescription", "lin",
             _PSC_FIELDS, _PSC_GROUP, code_fields=("typepsc", "stypepsc"), code_nomenclature="psc"),
    LayerDef("prescriptions_pct", "wfs_du:prescription_pct", "Prescriptions ponctuelles", "prescription", "pct",
             _PSC_FIELDS, _PSC_GROUP, code_fields=("typepsc", "stypepsc"), code_nomenclature="psc"),
    # --- Informations ---
    LayerDef("infos_surf", "wfs_du:info_surf", "Informations surfaciques", "information", "surf",
             _INF_FIELDS, _INF_GROUP, code_fields=("typeinf", "stypeinf"), code_nomenclature="inf"),
    LayerDef("infos_lin", "wfs_du:info_lin", "Informations linéaires", "information", "lin",
             _INF_FIELDS, _INF_GROUP, code_fields=("typeinf", "stypeinf"), code_nomenclature="inf"),
    LayerDef("infos_pct", "wfs_du:info_pct", "Informations ponctuelles", "information", "pct",
             _INF_FIELDS, _INF_GROUP, code_fields=("typeinf", "stypeinf"), code_nomenclature="inf"),
    # --- Servitudes d'utilité publique (assiettes = zone d'effet de la servitude) ---
    LayerDef("sup_assiette_s", "wfs_sup:assiette_sup_s", "Servitudes (assiettes surfaciques)", "servitude", "surf",
             _SUP_FIELDS, _SUP_GROUP),
    LayerDef("sup_assiette_l", "wfs_sup:assiette_sup_l", "Servitudes (assiettes linéaires)", "servitude", "lin",
             _SUP_FIELDS, _SUP_GROUP),
    # --- Générateurs SUP : objet à l'origine de la servitude (monument, ligne HT…).
    #     Désactivés par défaut : c'est l'assiette qui s'applique à la parcelle.
    LayerDef("sup_generateur_s", "wfs_sup:generateur_sup_s", "Servitudes (générateurs surfaciques)", "servitude", "surf",
             _SUP_FIELDS, ("suptype", "idgen"), enabled=False),
    LayerDef("sup_generateur_l", "wfs_sup:generateur_sup_l", "Servitudes (générateurs linéaires)", "servitude", "lin",
             _SUP_FIELDS, ("suptype", "idgen"), enabled=False),
    LayerDef("sup_generateur_p", "wfs_sup:generateur_sup_p", "Servitudes (générateurs ponctuels)", "servitude", "pct",
             _SUP_FIELDS, ("suptype", "idgen"), enabled=False),
]

LAYERS_BY_KEY: Dict[str, LayerDef] = {l.key: l for l in LAYERS}


def default_layer_keys() -> List[str]:
    return [l.key for l in LAYERS if l.enabled]


# ---------------------------------------------------------------------------
# Paramètres de calcul
# ---------------------------------------------------------------------------
@dataclass
class ProfilerSettings:
    # Couches à interroger (None = couches enabled du catalogue)
    layers: Optional[List[str]] = None

    # --- Requêtes WFS ---
    wfs_url: str = WFS_URL
    # Grille des centroïdes (Lambert-93). Une cellule non vide = un cluster.
    cluster_cell_m: float = 3000.0
    # Deux clusters dont les emprises se touchent sont fusionnés si le côté
    # résultant ne dépasse pas cette taille.
    cluster_max_side_m: float = 6000.0
    # Marge ajoutée à l'enveloppe des parcelles d'un cluster (contexte +100 m).
    anneau_zonage_m: float = 100.0
    # 1 = séquentiel. Monter à 2 seulement si le GPU suit.
    max_workers: int = 1
    timeout_s: int = 90
    # Tentatives supplémentaires après le premier essai (délais 2, 5, 15, 30 s).
    retries: int = 4
    # Intervalle minimum entre deux GetFeature, sous-tuiles comprises.
    min_interval_s: float = 1.0
    # Seconde passe : pause puis un essai, plus lent, sur les tâches en échec.
    retry_pass_wait_s: float = 30.0
    second_pass_interval_s: float = 3.0
    # Trois échecs de suite sur une couche : on la laisse reposer.
    layer_cooldown_s: float = 60.0
    max_subdivision_depth: int = 6
    # SRS de requête : "EPSG:4326" (comportement éprouvé de nos scripts ETL)
    # ou "EPSG:2154" (évite la reprojection, à valider sur le flux).
    fetch_srs: str = "EPSG:4326"

    # --- Mesures d'intersection ---
    # Tolérance de bord (m) : une entité surfacique qui ne fait que toucher
    # la parcelle dans cette bande est considérée "contact de limite" -> écartée.
    edge_tolerance_m: float = 0.5
    # Seuil surfacique : en dessous de ce % de la parcelle, l'entité est
    # écartée (artefacts de digitalisation, décalages cadastre/PLU).
    min_pct_surface: float = 0.5
    # …sauf si l'aire intersectée dépasse ce seuil absolu (grandes UF).
    min_area_keep_m2: float = 200.0
    # Linéaires / ponctuels à moins de edge_tolerance_m de la parcelle :
    # conservés avec position="limite" (haie protégée en bordure, etc.).
    keep_linear_on_boundary: bool = True

    # Voisinage : 0 = désactivé. Sinon, entités à moins de X m de la parcelle
    # (sans l'intersecter) listées à part dans `voisinage`.
    voisinage_m: float = 0.0

    # Retourner les entités écartées (contacts, slivers) pour le debug.
    include_ecartes: bool = True
    # Retourner toutes les propriétés brutes WFS de chaque entité.
    include_raw: bool = False
    # Retourner la géométrie de l'intersection (GeoJSON 4326) par élément (carto front).
    include_geometries: bool = False

    # Garde-fou : nombre max de parcelles par run (lancement manuel depuis le front).
    max_parcelles: int = 2000

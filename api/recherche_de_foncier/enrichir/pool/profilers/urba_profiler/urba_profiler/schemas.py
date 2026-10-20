"""
schemas.py — Contrats d'entrée / sortie (pydantic v2) du profiler urba.
C'est ce que le front consomme : voir README pour le détail champ par champ.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Entrée
# ---------------------------------------------------------------------------


class ParcelleIn(BaseModel):
    id: str = Field(..., description="Identifiant parcelle (IDU cadastral de préférence)")
    geometry: Dict[str, Any] = Field(..., description="Géométrie GeoJSON (Polygon/MultiPolygon)")
    srid: int = Field(2154, description="SRID de la géométrie fournie (2154 ou 4326)")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Infos libres renvoyées telles quelles (commune, section…)")


class UniteFonciereIn(BaseModel):
    id: str
    parcelle_ids: List[str] = Field(..., min_length=1, description="Ids de parcelles présentes dans `parcelles`")


class ProfilOptions(BaseModel):
    """Sous-ensemble surchargeable de config.ProfilerSettings."""

    layers: Optional[List[str]] = Field(None, description="Clés de couches (config.LAYERS). None = défaut")
    voisinage_m: Optional[float] = Field(None, ge=0, le=1000)
    min_pct_surface: Optional[float] = Field(None, ge=0, le=100)
    min_area_keep_m2: Optional[float] = Field(None, ge=0)
    edge_tolerance_m: Optional[float] = Field(None, ge=0, le=10)
    include_ecartes: Optional[bool] = None
    include_raw: Optional[bool] = None
    include_geometries: bool = Field(False, description="Renvoie la géométrie intersectée (GeoJSON 4326) par élément")


class UrbaProfilRequest(BaseModel):
    parcelles: List[ParcelleIn] = Field(..., min_length=1)
    unites_foncieres: List[UniteFonciereIn] = Field(default_factory=list)
    options: ProfilOptions = Field(default_factory=ProfilOptions)


# ---------------------------------------------------------------------------
# Sortie
# ---------------------------------------------------------------------------

Position = Literal["interieur", "limite", "voisinage"]
Status = Literal["intersected", "not_intersected", "error", "skipped"]


class Mesure(BaseModel):
    position: Position
    surface_m2: Optional[float] = None  # couches surfaciques
    pct_cible: Optional[float] = None  # % de la surface de la parcelle / UF
    longueur_m: Optional[float] = None  # couches linéaires (longueur dans la cible)
    nb_points: Optional[int] = None  # couches ponctuelles
    distance_m: Optional[float] = None  # renseignée pour limite / voisinage


class Lien(BaseModel):
    type: Literal["reglement", "acte_sup", "fichier"]
    url: str
    nom: Optional[str] = None


class Element(BaseModel):
    element_id: str  # stable dans la réponse : "<layer_key>:<n>"
    source_ids: List[str]  # gml_id des entités GPU fusionnées dans cet élément
    nb_entites: int
    attributs: Dict[str, Any]  # champs conservés (cf. Couche.colonnes)
    code: Optional[str] = None  # ex. "05-01" (psc/inf), "AUs" (typezone)
    libelle_cnig: Optional[str] = None  # libellé officiel du code (standard CNIG v2024-01)
    mesure: Mesure
    liens: List[Lien] = Field(default_factory=list)
    flags: List[str] = Field(default_factory=list)
    raison_ecart: Optional[str] = None  # seulement dans `ecartes`
    raw: Optional[List[Dict[str, Any]]] = None
    geometry: Optional[Dict[str, Any]] = None


class Couche(BaseModel):
    layer_key: str
    display_name: str
    famille: str
    geom_kind: str
    typename: str
    status: Status
    elements_count: int = 0
    elements: List[Element] = Field(default_factory=list)
    ecartes: List[Element] = Field(default_factory=list)
    voisinage: List[Element] = Field(default_factory=list)
    colonnes: Dict[str, str] = Field(default_factory=dict)  # champ -> libellé d'affichage
    error: Optional[str] = None


class DocumentUrba(BaseModel):
    idurba: Optional[str]
    partition: Optional[str] = None
    typedoc: Optional[str] = None
    date: Optional[str] = None  # date issue de l'idurba (AAAA-MM-JJ)
    datappro: Optional[str] = None
    insee: Optional[str] = None
    urls_reglement: List[str] = Field(default_factory=list)


class ZoneResume(BaseModel):
    layer_key: str
    libelle: Optional[str]
    libelong: Optional[str] = None
    type: Optional[str] = None
    pct_cible: Optional[float] = None
    surface_m2: Optional[float] = None


class Synthese(BaseModel):
    zonages: List[ZoneResume] = Field(default_factory=list)
    nb_elements_par_famille: Dict[str, int] = Field(default_factory=dict)
    couches_intersectees: List[str] = Field(default_factory=list)
    couches_en_erreur: List[str] = Field(default_factory=list)


class Cible(BaseModel):
    id: str
    type: Literal["parcelle", "unite_fonciere"]
    surface_m2: float
    parcelle_ids: List[str] = Field(default_factory=list)  # UF : composition (détail dans `parcelles`)
    meta: Dict[str, Any] = Field(default_factory=dict)
    documents: List[DocumentUrba] = Field(default_factory=list)
    synthese: Synthese
    couches: List[Couche]
    flags: List[str] = Field(default_factory=list)


class RunMeta(BaseModel):
    started_at: str
    duration_s: float
    nb_parcelles: int
    nb_unites_foncieres: int
    nb_clusters: int
    layers: List[str]
    wfs_requests_planned: int = 0
    wfs_requests: int
    wfs_cache_hits: int
    wfs_truncated_tiles: int
    wfs_subdivided_tiles: int = 0
    wfs_retries: int = 0
    # Échecs encore présents après la seconde passe : couche, cluster, type.
    failed_tasks: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)
    nomenclature: str = "Standard CNIG PLU v2024-01"


class UrbaProfilResponse(BaseModel):
    parcelles: Dict[str, Cible]
    unites_foncieres: Dict[str, Cible] = Field(default_factory=dict)
    meta: RunMeta

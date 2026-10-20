"""schemas.py — Sortie du scoring urbanistique (pydantic v2)."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Fiabilite = Literal["haute", "moyenne", "basse"]


class SousScore(BaseModel):
    valeur: float  # 0-100
    base: float  # valeur de départ (pérennité : base du zonage)
    delta: float  # somme des contributions
    definition: str = ""


class Contribution(BaseModel):
    rule_id: str
    label: str
    source: Literal["zonage", "secteur", "destination", "element", "voisinage"]
    points: Dict[str, float]  # sous-score -> points appliqués (après facteur et fiabilité)
    facteur: float  # facteur effectif cumulé (couverture x fiabilité, plafonné)
    couverture_pct: Optional[float] = None
    element_ids: List[str] = Field(default_factory=list)
    fiabilite: Fiabilite = "haute"
    conflits: List[str] = Field(default_factory=list)
    detail: Optional[str] = None
    justification: str = ""
    a_confirmer: bool = False


class VetoOut(BaseModel):
    rule_id: str
    niveau: Literal["bloquant", "majeur"]
    motif: str
    couverture_pct: Optional[float] = None
    element_ids: List[str] = Field(default_factory=list)
    fiabilite: Fiabilite = "haute"


class PointAVerifier(BaseModel):
    rule_id: str
    texte: str
    element_ids: List[str] = Field(default_factory=list)


class ElementInterprete(BaseModel):
    element_id: str
    layer_key: str
    code: Optional[str] = None
    libelle: Optional[str] = None
    rule_id: Optional[str] = None  # None = non interprété
    fiabilite: Optional[Fiabilite] = None
    source: str  # code | mots_cles | raffinement | aucun
    conflit: Optional[str] = None


class Confiance(BaseModel):
    valeur: float
    niveau: Literal["haute", "moyenne", "faible"]
    motifs: List[str] = Field(default_factory=list)


class ScoreUrba(BaseModel):
    profil: str
    version_regles: str
    score_global: float  # 0-100, après plafonnement des vetos
    score_avant_veto: float
    classe: str
    libelle_classe: str
    sous_scores: Dict[str, SousScore]
    confiance: Confiance
    vetos: List[VetoOut] = Field(default_factory=list)
    contributions: List[Contribution] = Field(default_factory=list)
    points_a_verifier: List[PointAVerifier] = Field(default_factory=list)
    interpretations: List[ElementInterprete] = Field(default_factory=list)
    resume: List[str] = Field(default_factory=list)


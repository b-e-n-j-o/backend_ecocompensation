"""
Router FastAPI — Ajout de foncier à la réserve foncière.

Endpoints
---------
POST /foncier/parcelles/search   recherche de parcelles par référence (API Carto IGN)
GET  /foncier/contacts?q=        recherche de contacts existants (réutilisation)
POST /foncier/ilots              création atomique contact (+) → îlot → parcelles

Monté dans main.py sous /api/reserve-fonciere
(le préfixe /api/foncier est déjà pris par la recherche de foncier).

Les géométries circulent en GeoJSON EPSG:4326 entre front et back ;
la reprojection en 2154 + nettoyage se fait dans PostGIS à l'insertion.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from datetime import date
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import get_engine

router = APIRouter(tags=["ajout-foncier"])
engine = get_engine()

APICARTO_PARCELLE = "https://apicarto.ign.fr/api/cadastre/parcelle"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
MAX_REFS = 200          # garde-fou par recherche
MAX_PARCELLES = 500     # garde-fou par îlot
CONCURRENCY = 6         # requêtes API Carto en parallèle

SourceType = Literal["RECHERCHE_IGN", "IMPORT_FICHIER", "ENVOI_ECOCOMP"]
Statut = Literal["A_ETUDIER", "CONTACTE", "EN_NEGOCIATION", "VALIDE", "REJETE"]


# =====================================================================
# Parsing des références saisies / collées
# =====================================================================
# Accepte : "ZB0175 / ZB0176", "ZB 175, ZB 176", "ZB 12, 13 et 14", "A12" (→ 0A),
# ignore les nombres sans section préalable ("330 287", "8,8 ha"...).
_REF_RE = re.compile(r"(?<![A-Z0-9])(?:([A-Z]{1,2}|0[A-Z])\s*[-.]?\s*)?(\d{1,4})(?![0-9])")
_STOPWORDS = {"ET", "OU", "DE", "DU", "LA", "LE", "EN", "AU", "HA", "M"}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_section(s: str) -> str:
    return s.strip().upper().rjust(2, "0")


def normalize_numero(n: str) -> str:
    return n.strip().rjust(4, "0")


def parse_refs(text: str) -> list[tuple[str, str]]:
    """Renvoie une liste ordonnée et dédoublonnée de (section, numero) normalisés."""
    t = _strip_accents(text).upper()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    current: str | None = None
    for m in _REF_RE.finditer(t):
        sec, num = m.group(1), m.group(2)
        if sec and sec in _STOPWORDS:
            sec = None
        if sec:
            current = normalize_section(sec)
        if current is None:
            continue  # nombre isolé sans section connue
        ref = (current, normalize_numero(num))
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


# =====================================================================
# Appel API Carto
# =====================================================================
async def _fetch_parcelle(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    code_insee: str,
    section: str,
    numero: str,
    com_abs: str | None,
) -> list[dict[str, Any]]:
    params = {"code_insee": code_insee, "section": section, "numero": numero, "_limit": 10}
    if com_abs and com_abs != "000":
        params["com_abs"] = com_abs
    last_err: Exception | None = None
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(APICARTO_PARCELLE, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json().get("features", []) or []
            except httpx.HTTPError as e:
                last_err = e
                await asyncio.sleep(0.8 * (attempt + 1))
    raise HTTPException(502, f"API Carto indisponible ({section}{numero}) : {last_err or 'erreur serveur'}")


def _to_parcelle_feature(raw: dict[str, Any], code_insee: str) -> dict[str, Any] | None:
    p = raw.get("properties") or {}
    geom = raw.get("geometry")
    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    section = normalize_section(str(p.get("section", "")))
    numero = normalize_numero(str(p.get("numero", "")))
    com_abs = str(p.get("com_abs") or "000").rjust(3, "0")
    idu = p.get("idu")
    if not idu:
        dep = str(p.get("code_dep") or code_insee[:2])
        com = str(p.get("code_com") or code_insee[2:]).rjust(3, "0")
        idu = f"{dep}{com}{com_abs}{section}{numero}"
    contenance = p.get("contenance")
    try:
        contenance = float(contenance) if contenance is not None else None
    except (TypeError, ValueError):
        contenance = None
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "idu": idu,
            "code_insee": str(p.get("code_insee") or code_insee),
            "nom_commune": p.get("nom_com") or p.get("nom_commune"),
            "section": section,
            "numero": numero,
            "prefixe": com_abs,
            "contenance": contenance,
        },
    }


# =====================================================================
# 1. Recherche de parcelles
# =====================================================================
class SearchIn(BaseModel):
    code_insee: str = Field(pattern=r"^\d{5}$")
    refs: str = Field(min_length=1, max_length=5000)
    prefixe: str | None = Field(default=None, pattern=r"^\d{3}$")


@router.post("/parcelles/search")
async def search_parcelles(body: SearchIn) -> dict[str, Any]:
    refs = parse_refs(body.refs)
    if not refs:
        raise HTTPException(422, "Aucune référence lisible (format attendu : ZB0175, ZB 175, A12…).")
    if len(refs) > MAX_REFS:
        raise HTTPException(422, f"Trop de références ({len(refs)} > {MAX_REFS}).")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_fetch_parcelle(client, sem, body.code_insee, s, n, body.prefixe) for s, n in refs)
        )

    features: list[dict[str, Any]] = []
    not_found: list[dict[str, str]] = []
    multiples: list[str] = []
    for (section, numero), raws in zip(refs, results):
        feats = [f for f in (_to_parcelle_feature(r, body.code_insee) for r in raws) if f]
        if not feats:
            not_found.append({"section": section, "numero": numero, "raison": "introuvable au cadastre"})
            continue
        if len(feats) > 1:
            multiples.append(f"{section}{numero}")  # plusieurs préfixes (communes associées)
        features.extend(feats)

    # Parcelles déjà présentes (actives) en réserve
    deja: list[dict[str, Any]] = []
    idus = [f["properties"]["idu"] for f in features]
    if idus:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    select idu, ilot_id, ilot_nom
                    from ecocompensation.v_reserve_fonciere
                    where idu = any(:idus)
                    """
                ),
                {"idus": idus},
            ).mappings()
            deja = [dict(r) for r in rows]

    return {
        "refs_lues": [{"section": s, "numero": n} for s, n in refs],
        "features": features,
        "not_found": not_found,
        "multiples": multiples,
        "deja_en_reserve": deja,
    }


class IntersectIn(BaseModel):
    geometries: list[dict[str, Any]] = Field(min_length=1, max_length=400)


@router.post("/parcelles/intersect")
async def intersect_parcelles(body: IntersectIn) -> dict[str, Any]:
    """Parcelles PCI qui intersectent l'emprise importée (API Carto = cadastre IGN)."""
    parts = []
    for raw in body.geometries:
        if raw.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            geom = shape(raw)
        except Exception:
            continue
        if geom.is_empty:
            continue
        if not geom.is_valid:
            geom = geom.buffer(0)
        parts.append(geom)
    if not parts:
        raise HTTPException(422, "Aucune géométrie surfacique à croiser avec le cadastre.")

    merged = unary_union(parts)
    payload = json.dumps(mapping(merged))
    if len(payload) > 12_000:
        merged = merged.simplify(0.00005, preserve_topology=True)
        payload = json.dumps(mapping(merged))

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            r = await client.get(
                APICARTO_PARCELLE,
                params={"geom": payload, "_limit": MAX_PARCELLES},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"API Carto indisponible : {e}")
        raws = r.json().get("features", []) or []

    features: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raws:
        feat = _to_parcelle_feature(raw, str((raw.get("properties") or {}).get("code_insee") or ""))
        if not feat:
            continue
        idu = feat["properties"]["idu"]
        if idu in seen:
            continue
        seen.add(idu)
        features.append(feat)

    deja: list[dict[str, Any]] = []
    idus = [f["properties"]["idu"] for f in features]
    if idus:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    select idu, ilot_id, ilot_nom
                    from ecocompensation.v_reserve_fonciere
                    where idu = any(:idus)
                    """
                ),
                {"idus": idus},
            ).mappings()
            deja = [dict(r) for r in rows]

    return {"features": features, "deja_en_reserve": deja, "nb_emprises": len(parts)}


# =====================================================================
# 2. Recherche de contacts existants
# =====================================================================
@router.get("/contacts")
def search_contacts(q: str = Query(default="", max_length=100)) -> list[dict[str, Any]]:
    like = f"%{q.strip()}%"
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                select id, nom, organisme, role, telephone, email
                from ecocompensation.rf_contact
                where :q = '%%' or nom ilike :q or organisme ilike :q or email ilike :q
                order by coalesce(organisme, nom)
                limit 15
                """
            ),
            {"q": like},
        ).mappings()
        return [dict(r) for r in rows]


# =====================================================================
# 3. Création atomique contact → îlot → parcelles
# =====================================================================
class ContactIn(BaseModel):
    id: int | None = None  # contact existant ; sinon champs ci-dessous
    nom: str | None = None
    organisme: str | None = None
    role: str | None = None
    telephone: str | None = None
    email: str | None = None
    notes: str | None = None


class IlotIn(BaseModel):
    nom: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    source_ref: str | None = None
    date_reception: date | None = None
    statut_prospection: Statut = "A_ETUDIER"
    surface_annoncee_ha: float | None = Field(default=None, gt=0)
    duree_blocage_ans: int | None = Field(default=None, ge=0)
    contraintes: str | None = None
    description: str | None = None
    attributs: dict[str, Any] = Field(default_factory=dict)


class ParcelleIn(BaseModel):
    idu: str | None = Field(default=None, max_length=14)
    section: str | None = None
    numero: str | None = None
    prefixe: str | None = None
    codeinsee: str | None = None
    nomcommune: str | None = None
    surface_cadastrale: float | None = None
    millesime_cadastre: str | None = None
    attributs_source: dict[str, Any] = Field(default_factory=dict)
    geometry: dict[str, Any]

    @field_validator("geometry")
    @classmethod
    def _poly_only(cls, g: dict[str, Any]) -> dict[str, Any]:
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("géométrie Polygon ou MultiPolygon attendue")
        return g

    @field_validator("idu")
    @classmethod
    def _idu_clean(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v or None


class CreateIlotIn(BaseModel):
    ilot: IlotIn
    contact: ContactIn | None = None
    parcelles: list[ParcelleIn] = Field(min_length=1, max_length=MAX_PARCELLES)


_INSERT_PARCELLE = text("""
with g as (
  select st_multi(
           st_collectionextract(
             st_makevalid(
               st_force2d(
                 st_transform(st_setsrid(st_geomfromgeojson(cast(:geom as text)), 4326), 2154))), 3)) as geom
)
insert into ecocompensation.reserve_fonciere (
  ilot_id, idu, section, numero, prefixe, codeinsee, nomcommune,
  surface_cadastrale, millesime_cadastre, source_type, attributs_source,
  created_by, geom_2154
)
select :ilot_id, :idu, :section, :numero, :prefixe, :codeinsee, :nomcommune,
       :surface, :millesime, :source_type, cast(:attributs as jsonb), :created_by, g.geom
from g
where not st_isempty(g.geom)
returning id
""")


@router.post("/ilots", status_code=201)
def create_ilot(body: CreateIlotIn) -> dict[str, Any]:
    idus = [p.idu for p in body.parcelles if p.idu]
    dups = sorted({i for i in idus if idus.count(i) > 1})
    if dups:
        raise HTTPException(422, {"message": "Parcelles en double dans l'îlot.", "idus": dups})

    with engine.begin() as conn:
        if idus:
            existing = conn.execute(
                text(
                    """
                    select idu from ecocompensation.reserve_fonciere
                    where idu = any(:idus) and archived_at is null
                    """
                ),
                {"idus": idus},
            ).mappings().all()
            if existing:
                raise HTTPException(
                    409,
                    {"message": "Parcelles déjà présentes en réserve foncière.",
                     "idus": [r["idu"] for r in existing]},
                )

        contact_id: int | None = None
        c = body.contact
        if c is not None:
            if c.id is not None:
                found = conn.execute(
                    text("select id from ecocompensation.rf_contact where id = :id"),
                    {"id": c.id},
                ).scalar()
                if not found:
                    raise HTTPException(404, "Contact introuvable.")
                contact_id = c.id
            elif (c.nom or "").strip() or (c.organisme or "").strip():
                contact_id = conn.execute(
                    text(
                        """
                        insert into ecocompensation.rf_contact
                          (nom, organisme, role, telephone, email, notes, created_by)
                        values (:nom, :organisme, :role, :telephone, :email, :notes, null)
                        returning id
                        """
                    ),
                    {
                        "nom": c.nom,
                        "organisme": c.organisme,
                        "role": c.role,
                        "telephone": c.telephone,
                        "email": c.email,
                        "notes": c.notes,
                    },
                ).scalar()

        i = body.ilot
        ilot_id = conn.execute(
            text(
                """
                insert into ecocompensation.rf_ilot
                  (nom, contact_id, source_type, source_ref, date_reception, statut_prospection,
                   surface_annoncee_ha, duree_blocage_ans, contraintes, description, attributs, created_by)
                values (:nom, :contact_id, :source_type, :source_ref, :date_reception, :statut,
                        :surface, :duree, :contraintes, :description, cast(:attributs as jsonb), null)
                returning id
                """
            ),
            {
                "nom": i.nom.strip(),
                "contact_id": contact_id,
                "source_type": i.source_type,
                "source_ref": i.source_ref,
                "date_reception": i.date_reception,
                "statut": i.statut_prospection,
                "surface": i.surface_annoncee_ha,
                "duree": i.duree_blocage_ans,
                "contraintes": i.contraintes,
                "description": i.description,
                "attributs": json.dumps(i.attributs),
            },
        ).scalar()

        for idx, p in enumerate(body.parcelles):
            codeinsee = p.codeinsee or (p.idu[:5] if p.idu and len(p.idu) == 14 else None)
            try:
                pid = conn.execute(
                    _INSERT_PARCELLE,
                    {
                        "geom": json.dumps(p.geometry),
                        "ilot_id": ilot_id,
                        "idu": p.idu,
                        "section": normalize_section(p.section) if p.section else None,
                        "numero": normalize_numero(p.numero) if p.numero else None,
                        "prefixe": p.prefixe or "000",
                        "codeinsee": codeinsee,
                        "nomcommune": p.nomcommune,
                        "surface": p.surface_cadastrale,
                        "millesime": p.millesime_cadastre,
                        "source_type": i.source_type,
                        "attributs": json.dumps(p.attributs_source, default=str),
                        "created_by": None,
                    },
                ).scalar()
            except IntegrityError:
                raise HTTPException(409, {"message": "Parcelle déjà en réserve.", "idus": [p.idu]})
            except Exception as e:
                raise HTTPException(422, f"Géométrie n°{idx + 1} ({p.idu or 'sans IDU'}) refusée : {e}")
            if pid is None:
                raise HTTPException(422, f"Géométrie n°{idx + 1} ({p.idu or 'sans IDU'}) vide après nettoyage.")

        stats = conn.execute(
            text(
                """
                select count(*) as n, coalesce(sum(surface_sig), 0) as surface_m2
                from ecocompensation.reserve_fonciere where ilot_id = :ilot_id
                """
            ),
            {"ilot_id": ilot_id},
        ).mappings().one()

    return {
        "ilot_id": ilot_id,
        "contact_id": contact_id,
        "nb_parcelles": stats["n"],
        "surface_sig_ha": round(float(stats["surface_m2"]) / 10_000, 4),
    }
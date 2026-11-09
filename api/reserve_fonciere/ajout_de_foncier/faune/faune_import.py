#!/usr/bin/env python3
"""Import d'observations faune vers ecocompensation.observation_faune.

Gabarit libre : la colonne du nom d'espèce est choisie à l'import.
Gabarit SINP : Nom_Taxon et Date_Deb sont proposés par défaut.
Les autres attributs partent dans details (jsonb).
"""
from __future__ import annotations

import json
import tempfile
import unicodedata
from difflib import SequenceMatcher
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy import text

from api.reserve_fonciere.ajout_de_foncier.flore.flore_import import (
    MAX_BYTES,
    MAX_FEATURES,
    MAX_ROWS,
    _fold,
    _parse_date,
    classify_new,
    fingerprint,
    mark_existing,
    read_vector,
)
from db import get_engine

router = APIRouter()

# Gabarit SINP (noms tronqués type shapefile, 10 caractères).
SINP_ESPECE = "nomtaxon"
SINP_DATE = "datedeb"
SINP_FIELDS = {
    "nomtaxon": "nom_taxon",
    "denombmi": "denombrement_min",
    "denombma": "denombrement_max",
    "objden": "objet_denombrement",
    "stadedvt": "stade_developpement",
    "sexe": "sexe",
    "proceobs": "protocole_observation",
    "indicepr": "indice_presence",
    "statubio": "statut_biologique",
    "datedeb": "date_debut",
    "datefin": "date_fin",
    "observate": "observateur",
    "descriobs": "description_observation",
    "legend": "legende",
    "statpres": "statut_presence",
    "idsource": "id_source",
    "longitude": "longitude",
    "latitude": "latitude",
    "idgeomet": "id_geometrie",
    "systcoor": "systeme_coordonnees",
    "obtgeome": "obtention_geometrie",
    "codeinsee": "code_insee",
    "commune": "commune",
}


def ensure_faune_schema(engine=None) -> None:
    eng = engine or get_engine()
    sql = (Path(__file__).resolve().parents[4] / "db" / "sql" / "observation_faune.sql").read_text()
    with eng.begin() as conn:
        conn.execute(text(sql))


def _cell(raw: Any) -> Any:
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    if hasattr(raw, "item"):
        raw = raw.item()
    if isinstance(raw, (date,)):
        return raw.isoformat()
    if isinstance(raw, (str, int, float, bool)):
        s = str(raw).strip() if isinstance(raw, str) else raw
        if isinstance(s, str) and s.lower() in ("", "nan", "none", "nat"):
            return None
        return s
    return str(raw)


_GUESS_ESPECE = ("nomtaxon", "nomespece", "nomscien", "taxon", "espece", "species", "nom")
_GUESS_DATE = ("datedeb", "dateobser", "dateobservation", "dateobs", "date")


def _guess_col(columns: list[str], keys: tuple[str, ...]) -> str | None:
    folded = [(_fold(c), c) for c in columns]
    for key in keys:
        for folded_name, col in folded:
            if folded_name == key or folded_name.startswith(key):
                return col
    return None


def _match_col(columns: list[str], wanted_fold: str | None, explicit: str | None) -> str | None:
    if explicit:
        for col in columns:
            if col == explicit or _fold(col) == _fold(explicit):
                return col
        raise ValueError(f"Colonne introuvable : {explicit}")
    if not wanted_fold:
        return None
    for col in columns:
        if _fold(col) == wanted_fold:
            return col
    return None


def prepare_import(
    path: Path,
    fichier: str | None = None,
    gabarit: str = "libre",
    colonne_espece: str | None = None,
    colonne_date: str | None = None,
) -> dict:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Fichier trop volumineux (max {MAX_BYTES // (1024 * 1024)} Mo)")
    gdf, crs_note = read_vector(path)
    if len(gdf) > MAX_ROWS:
        raise ValueError(f"Trop de lignes ({len(gdf)} > {MAX_ROWS})")
    columns = [c for c in gdf.columns if c.lower() not in ("geometry", "geom")]
    gabarit = (gabarit or "libre").lower()
    if gabarit not in ("libre", "sinp"):
        raise ValueError("gabarit = libre ou sinp")
    espece_default = SINP_ESPECE if gabarit == "sinp" else None
    date_default = SINP_DATE if gabarit == "sinp" else None
    col_espece = _match_col(columns, espece_default, colonne_espece) or (
        None if colonne_espece else _guess_col(columns, _GUESS_ESPECE)
    )
    if colonne_date or date_default:
        col_date = _match_col(columns, date_default, colonne_date)
    elif not colonne_espece:
        col_date = _guess_col(columns, _GUESS_DATE)
    else:
        col_date = None
    if not col_espece:
        return {
            "fichier": fichier or path.name,
            "crs_detecte": crs_note,
            "gabarit": gabarit,
            "colonnes": columns,
            "colonne_espece": None,
            "colonne_date": col_date,
            "nb_lignes": int(len(gdf)),
            "nb_valides": 0,
            "nb_rejetees": 0,
            "rejets": [],
            "apercu": [],
            "choix_requis": True,
            "_rows": [],
            "_geoms": [g for g in gdf.geometry if g is not None and not g.is_empty],
        }
    valides, rejets = [], []
    for i, row in gdf.iterrows():
        ligne = int(i) + 1 if isinstance(i, int) else len(valides) + len(rejets) + 1
        try:
            geom = row.geometry
            if geom is None or geom.is_empty:
                raise ValueError("géométrie vide")
            nom = _cell(row[col_espece])
            if not isinstance(nom, str) or not nom:
                raise ValueError("nom d'espèce vide")
            when = None
            if col_date:
                when = _parse_date(row[col_date])
            details: dict[str, Any] = {}
            for col in columns:
                if col in (col_espece, col_date):
                    continue
                val = _cell(row[col])
                if val is None:
                    continue
                key = SINP_FIELDS.get(_fold(col), col) if gabarit == "sinp" else col
                details[key] = val
            valides.append({
                "nom_espece": nom,
                "date_observation": when,
                "details": details,
                "empreinte": fingerprint(nom, when, None, geom),
                "geom": geom,
            })
        except Exception as e:
            rejets.append({"ligne": ligne, "raison": str(e)})
    apercu = []
    for item in valides[:5]:
        apercu.append({
            "nom_espece": item["nom_espece"],
            "date_observation": item["date_observation"].isoformat() if item["date_observation"] else None,
            "details": item["details"],
        })
    return {
        "fichier": fichier or path.name,
        "crs_detecte": crs_note,
        "gabarit": gabarit,
        "colonnes": columns,
        "colonne_espece": col_espece,
        "colonne_date": col_date,
        "nb_lignes": int(len(gdf)),
        "nb_valides": len(valides),
        "nb_rejetees": len(rejets),
        "rejets": rejets,
        "apercu": apercu,
        "choix_requis": False,
        "_rows": valides,
    }


def write_import(engine, report: dict, source: str | None = None) -> dict:
    if report.get("choix_requis") or not report.get("colonne_espece"):
        raise ValueError("Choisir la colonne du nom d'espèce")
    ensure_faune_schema(engine)
    rows = report["_rows"]
    with engine.begin() as conn:
        known = {r[0] for r in conn.execute(text("SELECT empreinte FROM ecocompensation.observation_faune")).all()}
        fresh, dups = classify_new(rows, known)
        import_id = conn.execute(
            text("""
                INSERT INTO ecocompensation.faune_import
                    (fichier, gabarit, source, nb_lignes, nb_inserees, nb_doublons, nb_rejetees, rapport)
                VALUES (:fichier, :gabarit, :source, :nb, 0, :dups, :rej, CAST(:rapport AS jsonb))
                RETURNING id
            """),
            {
                "fichier": report["fichier"],
                "gabarit": report["gabarit"],
                "source": source,
                "nb": report["nb_lignes"],
                "dups": dups,
                "rej": report["nb_rejetees"],
                "rapport": json.dumps({k: v for k, v in report.items() if k != "_rows"}, default=str),
            },
        ).scalar_one()
        for row in fresh:
            conn.execute(
                text("""
                    INSERT INTO ecocompensation.observation_faune
                        (import_id, nom_espece, date_observation, details, empreinte, geom)
                    VALUES (
                        :import_id, :nom_espece, :date_observation, CAST(:details AS jsonb), :empreinte,
                        ST_Force2D(ST_SetSRID(ST_GeomFromText(:wkt), 2154))
                    )
                    ON CONFLICT (empreinte) DO NOTHING
                """),
                {
                    "import_id": import_id,
                    "nom_espece": row["nom_espece"],
                    "date_observation": row["date_observation"],
                    "details": json.dumps(row["details"], default=str),
                    "empreinte": row["empreinte"],
                    "wkt": row["geom"].wkt,
                },
            )
        inserted = conn.execute(
            text("SELECT COUNT(*) FROM ecocompensation.observation_faune WHERE import_id = :id"),
            {"id": import_id},
        ).scalar_one()
        conn.execute(
            text("""
                UPDATE ecocompensation.faune_import
                SET nb_inserees = :n, nb_doublons = :d
                WHERE id = :id
            """),
            {"n": int(inserted), "d": dups + (len(fresh) - int(inserted)), "id": import_id},
        )
    public = {k: v for k, v in report.items() if k != "_rows"}
    public["import_id"] = int(import_id)
    public["nb_inserees"] = int(inserted)
    public["nb_doublons"] = dups + (len(fresh) - int(inserted))
    return public


@router.post("/import")
async def import_faune(
    dry_run: bool = Query(True),
    gabarit: str = Query("libre"),
    colonne_espece: str | None = Query(None),
    colonne_date: str | None = Query(None),
    source: str | None = Query(None),
    file: UploadFile = File(...),
):
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "Fichier trop volumineux")
    suffix = Path(file.filename or "obs.geojson").suffix or ".geojson"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        report = prepare_import(
            tmp_path,
            fichier=file.filename,
            gabarit=gabarit,
            colonne_espece=colonne_espece,
            colonne_date=colonne_date,
        )
        if dry_run:
            mark_existing(report, "ecocompensation.observation_faune")
            report.pop("_rows", None)
            report.pop("_geoms", None)
            report["dry_run"] = True
            return report
        return write_import(get_engine(), report, source)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/observations")
def observations(bbox: str | None = Query(None)):
    ensure_faune_schema()
    limit = 20_000 if not bbox else MAX_FEATURES
    params: dict = {"lim": limit + 1}
    where = ""
    if bbox:
        try:
            minx, miny, maxx, maxy = [float(x) for x in bbox.split(",")]
        except ValueError as e:
            raise HTTPException(400, "bbox = minLon,minLat,maxLon,maxLat") from e
        where = "WHERE geom && ST_Transform(ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326), 2154)"
        params.update({"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy})
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT nom_espece, date_observation, details,
                       ST_AsGeoJSON(ST_Transform(geom, 4326), 6) AS geojson
                FROM ecocompensation.observation_faune
                {where}
                LIMIT :lim
            """),
            params,
        ).mappings().all()
    truncated = len(rows) > limit
    features = []
    for r in rows[:limit]:
        if not r["geojson"]:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "nom_espece": r["nom_espece"],
                "date_observation": r["date_observation"].isoformat() if r["date_observation"] else None,
                "details": r["details"],
            },
            "geometry": json.loads(r["geojson"]),
        })
    return {"type": "FeatureCollection", "truncated": truncated, "features": features}


def _fold_name(value: str) -> str:
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


@router.get("/noms-proches")
def noms_proches(q: str = Query(..., min_length=2)):
    """Noms importés les plus proches d'un nom du référentiel faune. Aucun n'est retenu sans confirmation."""
    ensure_faune_schema()
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT nom_espece, COUNT(*)::int AS n
            FROM ecocompensation.observation_faune
            WHERE nom_espece IS NOT NULL AND btrim(nom_espece) <> ''
            GROUP BY nom_espece
        """)).all()
    needle = _fold_name(q)
    scored = []
    for name, n in rows:
        folded = _fold_name(str(name))
        if folded == needle:
            score = 1.0
        elif needle in folded or folded in needle:
            score = 0.85
        else:
            score = SequenceMatcher(None, needle, folded).ratio()
        if score >= 0.45:
            scored.append({"nom": str(name), "nb": int(n), "score": round(score, 2)})
    scored.sort(key=lambda item: (-item["score"], -item["nb"], item["nom"]))
    return scored[:8]


@router.get("/imports")
def list_imports():
    ensure_faune_schema()
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, fichier, gabarit, source, nb_lignes, nb_inserees, nb_doublons, nb_rejetees, created_at
            FROM ecocompensation.faune_import
            ORDER BY created_at DESC
            LIMIT 100
        """)).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        item["created_at"] = item["created_at"].isoformat() if item.get("created_at") else None
        out.append(item)
    return out


@router.delete("/imports/{import_id}")
def delete_import(import_id: int):
    ensure_faune_schema()
    with get_engine().begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation.faune_import WHERE id = :id"),
            {"id": import_id},
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Import introuvable")
    return {"status": "ok", "id": import_id}

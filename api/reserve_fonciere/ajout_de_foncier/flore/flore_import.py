#!/usr/bin/env python3
"""Import d'observations flore (SHP/ZIP, GPKG, GeoJSON) vers ecocompensation.flore_observation.

CLI :
  python flore_import.py --file observations.zip --dry-run -v
  python flore_import.py --file observations.zip -v
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import tempfile
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from shapely.geometry import shape
from pyproj import CRS
from shapely.geometry import mapping
from shapely.ops import transform
from sqlalchemy import text

from api.recherche_de_foncier.routers.flore_router import ensure_flore_schema
from db import get_engine

log = logging.getLogger("kerfoncier.flore_import")

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
MAX_FEATURES = 5_000

# Noms SHP tronqués à 10 caractères → colonne base.
# Hypothèses : Nom_tax_v stocké tel quel (affiché comme vernaculaire) ;
# Code_nom_v = code TAXREF nullable ; Surf_popul reste du texte.
COLUMN_TARGETS = {
    "id_donnee": "id_donnee",
    "observateur": "observateur",
    "nom_taxon_cite": "nom_taxon_cite",
    "cd_nom_valide": "cd_nom_valide",
    "nom_taxon_v": "nom_taxon_v",
    "denombrement_min": "denombrement_min",
    "denombrement_max": "denombrement_max",
    "stade_developpement": "stade_developpement",
    "surface_population": "surface_population",
    "spontaneite": "spontaneite",
    "remarque_observation": "remarque_observation",
    "date_observation": "date_observation",
    "milieu": "milieu",
    "remarque_milieu": "remarque_milieu",
}

_ALIASES = {
    "iddonnee": "id_donnee",
    "observateu": "observateur",
    "observateur": "observateur",
    "nomtaxci": "nom_taxon_cite",
    "nomtaxonc": "nom_taxon_cite",
    "nomtaxoncite": "nom_taxon_cite",
    "codenomv": "cd_nom_valide",
    "cdnom": "cd_nom_valide",
    "cdnomvalide": "cd_nom_valide",
    "nomtaxv": "nom_taxon_v",
    "nomtaxonv": "nom_taxon_v",
    "denommini": "denombrement_min",
    "denombrementmin": "denombrement_min",
    "denommax": "denombrement_max",
    "denombrementmax": "denombrement_max",
    "stadedvlp": "stade_developpement",
    "stadedeveloppement": "stade_developpement",
    "surfpopul": "surface_population",
    "surfacepopulation": "surface_population",
    "spontaneit": "spontaneite",
    "spontaneite": "spontaneite",
    "rqobserva": "remarque_observation",
    "remarqueobservation": "remarque_observation",
    "dateobser": "date_observation",
    "dateobservation": "date_observation",
    "milieu": "milieu",
    "rqmilieu": "remarque_milieu",
    "remarquemilieu": "remarque_milieu",
}

router = APIRouter()


def _fold(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def map_columns(columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Retourne {colonne_source: champ_base} et les colonnes inconnues."""
    mapping_cols: dict[str, str] = {}
    unknown: list[str] = []
    for col in columns:
        if col.lower() in ("geometry", "geom", "wkt_geom"):
            continue
        target = _ALIASES.get(_fold(col))
        if target:
            mapping_cols[col] = target
        else:
            unknown.append(col)
    return mapping_cols, unknown


def _parse_date(raw: Any) -> date | None:
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s or s.lower() in ("none", "nan", "nat"):
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"date illisible : {s}")


def _parse_int(raw: Any) -> int | None:
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("none", "nan"):
        return None
    return int(float(s))


def normalize_taxon(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def fingerprint(nom: str, when: date | None, observateur: str | None, geom) -> str:
    """Nom + géométrie arrondie au cm. La date et l'observateur ne comptent pas : ils sont souvent absents."""
    del when, observateur
    payload = "|".join([normalize_taxon(nom), geom_key(geom)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def geom_key(geom) -> str:
    if geom is None or geom.is_empty:
        return ""
    def repl(m):
        return f"{round(float(m.group(0)), 2):.2f}"
    return re.sub(r"-?\d+\.\d+", repl, geom.wkt)


def _assume_lambert(gdf: gpd.GeoDataFrame) -> bool:
    xs, ys = [], []
    for g in gdf.geometry:
        if g is None or g.is_empty:
            continue
        xs.append(g.centroid.x)
        ys.append(g.centroid.y)
        if len(xs) >= 20:
            break
    if not xs:
        return False
    return all(100_000 <= x <= 1_300_000 for x in xs) and all(6_000_000 <= y <= 7_200_000 for y in ys)


def read_vector(path: Path) -> tuple[gpd.GeoDataFrame, str]:
    suffix = path.suffix.lower()
    if suffix in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
        raise ValueError("Shapefile incomplet : déposez un ZIP contenant .shp, .shx et .dbf")
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                shp = next((n for n in names if n.lower().endswith(".shp")), None)
                if not shp:
                    raise ValueError("ZIP sans fichier .shp")
            gdf = gpd.read_file(f"zip://{path}")
        else:
            gdf = gpd.read_file(path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Fichier illisible ({suffix or 'sans extension'}) : {e}") from e
    crs_note = "inconnu"
    if gdf.crs is None:
        if _assume_lambert(gdf):
            gdf = gdf.set_crs(2154)
            crs_note = "suppose_2154"
        else:
            raise ValueError("CRS absent et coordonnées non plausibles en Lambert-93")
    else:
        crs_note = CRS.from_user_input(gdf.crs).to_string()
        if gdf.crs.to_epsg() != 2154:
            gdf = gdf.to_crs(2154)
    return gdf, crs_note


def prepare_import(path: Path, fichier: str | None = None, colonne_nom: str | None = None) -> dict:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Fichier trop volumineux (max {MAX_BYTES // (1024*1024)} Mo)")
    gdf, crs_note = read_vector(path)
    if len(gdf) > MAX_ROWS:
        raise ValueError(f"Trop de lignes ({len(gdf)} > {MAX_ROWS})")
    columns = [c for c in gdf.columns if str(c).lower() not in ("geometry", "geom")]
    mapping_cols, unknown = map_columns(list(gdf.columns))
    if colonne_nom:
        match = next((c for c in columns if c == colonne_nom or _fold(c) == _fold(colonne_nom)), None)
        if not match:
            raise ValueError(f"Colonne introuvable : {colonne_nom}")
        mapping_cols = {k: v for k, v in mapping_cols.items() if v != "nom_taxon_cite"}
        mapping_cols[match] = "nom_taxon_cite"
        unknown = [c for c in unknown if c != match]
    elif "nom_taxon_cite" not in mapping_cols.values():
        return {
            "fichier": fichier or path.name,
            "crs_detecte": crs_note,
            "colonnes": columns,
            "colonne_nom": None,
            "choix_requis": True,
            "mapping_colonnes": mapping_cols,
            "colonnes_inconnues": unknown,
            "nb_lignes": int(len(gdf)),
            "nb_valides": 0,
            "nb_rejetees": 0,
            "rejets": [],
            "apercu": [],
            "_rows": [],
            "_geoms": [g for g in gdf.geometry if g is not None and not g.is_empty],
        }
    inv = {v: k for k, v in mapping_cols.items()}
    valides = []
    rejets = []
    for i, row in gdf.iterrows():
        ligne = int(i) + 1 if isinstance(i, int) else len(valides) + len(rejets) + 1
        try:
            geom = row.geometry
            if geom is None or geom.is_empty:
                raise ValueError("géométrie vide")
            geom = geom.buffer(0) if geom.geom_type in ("Polygon", "MultiPolygon") else geom
            if not geom.is_valid:
                from shapely.validation import make_valid
                geom = make_valid(geom)
            if geom.has_z:
                geom = transform(lambda x, y, z=None: (x, y), geom)
            gtype = geom.geom_type.upper()
            if gtype == "GEOMETRYCOLLECTION":
                raise ValueError("geometry collection non éclatée")
            allowed = {"POINT", "MULTIPOINT", "LINESTRING", "MULTILINESTRING", "POLYGON", "MULTIPOLYGON"}
            if gtype not in allowed:
                raise ValueError(f"type de géométrie refusé : {gtype}")
            nom = str(row[inv["nom_taxon_cite"]] or "").strip()
            if not nom or nom.lower() == "nan":
                raise ValueError("nom_taxon_cite vide")
            when = _parse_date(row[inv["date_observation"]]) if "date_observation" in inv else None
            obs = str(row[inv["observateur"]]).strip() if "observateur" in inv and row[inv["observateur"]] == row[inv["observateur"]] else None
            if obs in ("", "nan", "None"):
                obs = None
            cd = _parse_int(row[inv["cd_nom_valide"]]) if "cd_nom_valide" in inv else None
            dmin = _parse_int(row[inv["denombrement_min"]]) if "denombrement_min" in inv else None
            dmax = _parse_int(row[inv["denombrement_max"]]) if "denombrement_max" in inv else None
            extras = {}
            for col in unknown:
                val = row[col]
                if val is None or (isinstance(val, float) and val != val):
                    continue
                extras[col] = val if isinstance(val, (str, int, float)) else str(val)
            def txt(field: str) -> str | None:
                if field not in inv:
                    return None
                val = row[inv[field]]
                if val is None or (isinstance(val, float) and val != val):
                    return None
                s = str(val).strip()
                return None if s.lower() in ("", "nan", "none") else s
            item = {
                "id_donnee": txt("id_donnee"),
                "observateur": obs,
                "nom_taxon_cite": nom,
                "cd_nom_valide": cd,
                "nom_taxon_v": txt("nom_taxon_v"),
                "denombrement_min": dmin,
                "denombrement_max": dmax,
                "stade_developpement": txt("stade_developpement"),
                "surface_population": txt("surface_population"),
                "spontaneite": txt("spontaneite"),
                "remarque_observation": txt("remarque_observation"),
                "date_observation": when,
                "milieu": txt("milieu"),
                "remarque_milieu": txt("remarque_milieu"),
                "attributs_source": extras,
                "empreinte": fingerprint(nom, when, obs, geom),
                "geom": geom,
            }
            valides.append(item)
        except Exception as e:
            rejets.append({"ligne": ligne, "raison": str(e)})
    apercu = []
    for item in valides[:5]:
        apercu.append({k: (v.isoformat() if isinstance(v, date) else v) for k, v in item.items() if k != "geom"})
    return {
        "fichier": fichier or path.name,
        "crs_detecte": crs_note,
        "colonnes": columns,
        "colonne_nom": next((k for k, v in mapping_cols.items() if v == "nom_taxon_cite"), None),
        "choix_requis": False,
        "mapping_colonnes": mapping_cols,
        "colonnes_inconnues": unknown,
        "nb_lignes": int(len(gdf)),
        "nb_valides": len(valides),
        "nb_rejetees": len(rejets),
        "rejets": rejets,
        "apercu": apercu,
        "_rows": valides,
    }


_IMPORT_TABLE = {
    "ecocompensation.flore_observation": "ecocompensation.flore_import",
    "ecocompensation.observation_faune": "ecocompensation.faune_import",
}


def count_known(engine, table: str, rows: list[dict]) -> tuple[int, list[str], bool]:
    """Géométries déjà en base (tolérance 5 cm) et fichiers d'import d'origine."""
    if table not in _IMPORT_TABLE:
        raise ValueError(table)
    geoms = [r["geom"] for r in rows if r.get("geom") is not None and not r["geom"].is_empty]
    if not geoms:
        return 0, [], False
    wkts = [g.wkt for g in geoms]
    import_table = _IMPORT_TABLE[table]
    sql = text(f"""
        SELECT inc.wkt, COALESCE(i.fichier, '') AS fichier
        FROM unnest(CAST(:wkts AS text[])) AS inc(wkt)
        JOIN {table} o
          ON ST_DWithin(o.geom, ST_SetSRID(ST_GeomFromText(inc.wkt), 2154), 0.05)
        LEFT JOIN {import_table} i ON i.id = o.import_id
    """)
    with engine.connect() as conn:
        hits = conn.execute(sql, {"wkts": wkts}).mappings().all()
    matched = {h["wkt"] for h in hits}
    fichiers: list[str] = []
    for h in hits:
        name = (h["fichier"] or "").strip()
        if name and name not in fichiers:
            fichiers.append(name)
    known = sum(1 for w in wkts if w in matched)
    tous = bool(geoms) and all(g.wkt in matched for g in geoms)
    return known, fichiers, tous


def mark_existing(report: dict, table: str) -> dict:
    rows = report.get("_rows") or [{"geom": g} for g in (report.get("_geoms") or [])]
    try:
        known, fichiers, tous = count_known(get_engine(), table, rows)
    except Exception:
        known, fichiers, tous = 0, [], False
    report["nb_doublons"] = known
    report["deja_en_base"] = tous
    report["fichiers_deja"] = fichiers
    return report


def classify_new(rows: list[dict], existing: set[str]) -> tuple[list[dict], int]:
    fresh, dups = [], 0
    seen = set(existing)
    for row in rows:
        if row["empreinte"] in seen:
            dups += 1
        else:
            seen.add(row["empreinte"])
            fresh.append(row)
    return fresh, dups


def write_import(engine, report: dict, source: str | None = None) -> dict:
    ensure_flore_schema(engine)
    rows = report["_rows"]
    with engine.begin() as conn:
        known = {
            r[0]
            for r in conn.execute(text("SELECT empreinte FROM ecocompensation.flore_observation")).all()
        }
        fresh, dups = classify_new(rows, known)
        import_id = conn.execute(
            text("""
                INSERT INTO ecocompensation.flore_import
                    (fichier, source, nb_lignes, nb_inserees, nb_doublons, nb_rejetees, rapport)
                VALUES (:fichier, :source, :nb, 0, :dups, :rej, CAST(:rapport AS jsonb))
                RETURNING id
            """),
            {
                "fichier": report["fichier"],
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
                    INSERT INTO ecocompensation.flore_observation (
                        import_id, id_donnee, observateur, nom_taxon_cite, cd_nom_valide, nom_taxon_v,
                        denombrement_min, denombrement_max, stade_developpement, surface_population,
                        spontaneite, remarque_observation, date_observation, milieu, remarque_milieu,
                        attributs_source, empreinte, geom
                    ) VALUES (
                        :import_id, :id_donnee, :observateur, :nom_taxon_cite, :cd_nom_valide, :nom_taxon_v,
                        :denombrement_min, :denombrement_max, :stade_developpement, :surface_population,
                        :spontaneite, :remarque_observation, :date_observation, :milieu, :remarque_milieu,
                        CAST(:attributs_source AS jsonb), :empreinte,
                        ST_Force2D(ST_SetSRID(ST_GeomFromText(:wkt), 2154))
                    )
                    ON CONFLICT (empreinte) DO NOTHING
                """),
                {
                    **{k: row[k] for k in row if k not in ("geom", "attributs_source")},
                    "import_id": import_id,
                    "attributs_source": json.dumps(row["attributs_source"]),
                    "wkt": row["geom"].wkt,
                },
            )
        inserted = conn.execute(
            text("SELECT COUNT(*) FROM ecocompensation.flore_observation WHERE import_id = :id"),
            {"id": import_id},
        ).scalar_one()
        conn.execute(
            text("""
                UPDATE ecocompensation.flore_import
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


def import_path(path: Path, dry_run: bool = True, source: str | None = None, engine=None) -> dict:
    report = prepare_import(path)
    if dry_run:
        report.pop("_rows", None)
        report["nb_doublons"] = 0
        report["dry_run"] = True
        return report
    return write_import(engine or get_engine(), {**report, "_rows": report.get("_rows") or prepare_import(path)["_rows"]}, source)


@router.post("/import")
async def import_flore(
    dry_run: bool = Query(True),
    source: str | None = Query(None),
    colonne_nom: str | None = Query(None),
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
        report = prepare_import(tmp_path, fichier=file.filename, colonne_nom=colonne_nom)
        if dry_run:
            mark_existing(report, "ecocompensation.flore_observation")
            report.pop("_rows", None)
            report.pop("_geoms", None)
            report["dry_run"] = True
            return report
        return write_import(get_engine(), report, source)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp_path.unlink(missing_ok=True)


MAP_ALL_LIMIT = 20_000


@router.get("/observations")
def observations(bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat. Absent = toute la couche.")):
    ensure_flore_schema()
    limit = MAP_ALL_LIMIT if not bbox else MAX_FEATURES
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
                SELECT nom_taxon_cite, nom_taxon_v, date_observation, observateur, milieu, taxon_key,
                       ST_AsGeoJSON(ST_Transform(geom, 4326), 6) AS geojson
                FROM ecocompensation.flore_observation
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
                "nom_scientifique": r["nom_taxon_cite"],
                "nom_vernaculaire": r["nom_taxon_v"],
                "date_observation": r["date_observation"].isoformat() if r["date_observation"] else None,
                "observateur": r["observateur"],
                "milieu": r["milieu"],
                "taxon_key": r["taxon_key"],
            },
            "geometry": json.loads(r["geojson"]),
        })
    return {"type": "FeatureCollection", "truncated": truncated, "features": features}


@router.get("/imports")
def list_imports():
    ensure_flore_schema()
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, fichier, source, nb_lignes, nb_inserees, nb_doublons, nb_rejetees, created_at
            FROM ecocompensation.flore_import
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
    ensure_flore_schema()
    with get_engine().begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM ecocompensation.flore_import WHERE id = :id"),
            {"id": import_id},
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Import introuvable")
    return {"status": "ok", "id": import_id}


class ZonePointsBody(BaseModel):
    names: list[str] = Field(default_factory=list)
    buffer_m: float = Field(3000, ge=0, le=50_000)
    geometry: dict


@router.post("/zone-points")
def zone_points(body: ZonePointsBody):
    """Observations flore de ces taxons, dans la zone bufferisée."""
    names = [n.strip() for n in body.names if n and n.strip()]
    if not names:
        return {"type": "FeatureCollection", "features": []}
    try:
        geom = shape(body.geometry)
    except Exception as e:
        raise HTTPException(400, "Géométrie illisible") from e
    if geom.is_empty:
        return {"type": "FeatureCollection", "features": []}
    srid = 2154 if abs(geom.bounds[0]) > 1000 else 4326
    zone = """
        CASE WHEN :srid = 2154
             THEN ST_SetSRID(ST_GeomFromGeoJSON(:gj), 2154)
             ELSE ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:gj), 4326), 2154)
        END
    """
    sql = text(f"""
        SELECT COALESCE(taxon_key, nom_taxon_cite) AS nom,
               ST_AsGeoJSON(ST_Transform(ST_Centroid(geom), 4326)) AS geojson
        FROM ecocompensation.flore_observation
        WHERE (taxon_key = ANY(:names) OR nom_taxon_cite = ANY(:names))
          AND ST_DWithin(geom, {zone}, :buf)
        LIMIT 2000
    """)
    with get_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '12s'"))
        rows = conn.execute(sql, {
            "names": names,
            "gj": json.dumps(body.geometry),
            "srid": srid,
            "buf": float(body.buffer_m),
        }).mappings().all()
    features = []
    for r in rows:
        if not r["geojson"]:
            continue
        features.append({
            "type": "Feature",
            "properties": {"source": "flore", "nom": r["nom"]},
            "geometry": json.loads(r["geojson"]),
        })
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    import os
    from sqlalchemy import create_engine

    ap = argparse.ArgumentParser(description="Import observations flore")
    ap.add_argument("--file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    path = Path(a.file)
    if a.dry_run:
        report = prepare_import(path)
        report.pop("_rows", None)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    engine = create_engine(os.environ["DATABASE_URL"])
    report = prepare_import(path)
    print(json.dumps(write_import(engine, report, a.source), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

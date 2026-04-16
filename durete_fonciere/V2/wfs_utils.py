# -*- coding: utf-8 -*-
"""
Utils WFS → GeoDataFrame → Supabase (PostGIS)
- Parsing URL WFS (base_url, typename)
- Harvest avec carroyage adaptatif (logs par tuile)
- Dédup (ID ou géométrie)
- Estimation de taille mémoire / géométrie
- Connexion Supabase (5432 ou 6543)
- Push DB (full refresh ou upsert)
"""

import os, logging, tempfile, time, hashlib
import unicodedata
from typing import Tuple, List, Dict, Any
from urllib.parse import urlsplit, parse_qs, quote_plus

import geopandas as gpd
import pandas as pd
from owslib.wfs import WebFeatureService
from sqlalchemy import create_engine, text

# -----------------------------
# Sécurisation identifiants PostgreSQL
# -----------------------------
def _slug_pg(name: str, maxlen: int = 63) -> str:
    """
    Transforme un nom en identifiant PostgreSQL sûr :
    - retire les accents
    - garde [a-z0-9_]
    - tronque à maxlen
    - ajoute un hash si nécessaire pour l'unicité
    """
    if not name:
        return "t"
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = name.lower()
    name = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name)
    name = name.replace("-", "_")
    if len(name) <= maxlen:
        return name
    h = hashlib.blake2b(name.encode("utf-8"), digest_size=6).hexdigest()
    keep = maxlen - (1 + len(h))
    return f"{name[:keep]}_{h}"

def safe_ident(name: str, maxlen: int = 63) -> str:
    return _slug_pg(name, maxlen)

# -----------------------------
# Parsing d'URL WFS
# -----------------------------
def parse_service_and_typename(url_complete: str) -> tuple[str, str]:
    u = urlsplit(url_complete)
    base_url = f"{u.scheme}://{u.netloc}{u.path}"
    qs = parse_qs(u.query)
    for key in ("typeNames", "typename", "typeName"):
        if key in qs and len(qs[key]) > 0:
            return base_url, qs[key][0]
    raise ValueError(f"Impossible de trouver 'typename' dans l'URL: {url_complete}")

def normalize_table_name(name: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.lower())
    out = out.replace("-", "_")
    return safe_ident(out, maxlen=63)

# -----------------------------
# I/O temporaires
# -----------------------------
def _write_temp(content: bytes, suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    with open(f.name, "wb") as fh:
        fh.write(content)
    return f.name

# -----------------------------
# Fetch WFS (avec variantes OWSLib)
# -----------------------------
def fetch_wfs_bbox(wfs_url, layer, bbox, srs='EPSG:4326'):
    """
    GetFeature sur BBOX via OWSLib avec variantes (typename/typeName, srsname/srsName,
    ou CRS suffixé dans bbox). Retourne (GeoDataFrame, format_utilisé) ou (vide,None).
    """
    minx, miny, maxx, maxy = bbox
    try:
        wfs = WebFeatureService(wfs_url, version='2.0.0')
    except Exception as e:
        logging.error(f"❌ Init WFS KO: {e}")
        return gpd.GeoDataFrame(), None

    formats = ['application/json', 'json', 'text/xml; subtype=gml/3.2']
    key_variants = [
        {"type_key": "typename", "srs_key": "srsname", "bbox_with_crs": False},
        {"type_key": "typeName", "srs_key": "srsname", "bbox_with_crs": False},
        {"type_key": "typename", "srs_key": "srsName", "bbox_with_crs": False},
        {"type_key": "typeName", "srs_key": "srsName", "bbox_with_crs": False},
        {"type_key": "typename", "srs_key": None, "bbox_with_crs": True},
        {"type_key": "typeName", "srs_key": None, "bbox_with_crs": True},
    ]

    last_err = None
    for fmt in formats:
        for kv in key_variants:
            try:
                kwargs = {
                    kv["type_key"]: layer,
                    "bbox": (minx, miny, maxx, maxy) if not kv["bbox_with_crs"] else (minx, miny, maxx, maxy, srs),
                    "outputFormat": fmt
                }
                if kv["srs_key"] is not None:
                    kwargs[kv["srs_key"]] = srs

                logging.debug(f"GetFeature fmt='{fmt}' {kv['type_key']} / {kv['srs_key'] or 'bbox+CRS'} bbox={kwargs['bbox']} …")
                resp = wfs.getfeature(**kwargs)

                if 'json' in fmt.lower():
                    tmp = _write_temp(resp.read() if hasattr(resp, 'read') else resp, '.json')
                    try:
                        gdf = gpd.read_file(tmp).set_crs(srs, allow_override=True)
                        logging.debug(f"✅ OK ({fmt})")
                        return gdf, fmt
                    finally:
                        os.remove(tmp)
                else:
                    tmp = _write_temp(resp.read() if hasattr(resp, 'read') else resp, '.gml')
                    try:
                        gdf = gpd.read_file(tmp)
                        if not gdf.crs or str(gdf.crs).upper() != srs.upper():
                            gdf = gdf.set_crs(srs, allow_override=True)
                        logging.debug(f"✅ OK ({fmt})")
                        return gdf, fmt
                    finally:
                        os.remove(tmp)

            except TypeError as e:
                last_err = e
                logging.debug(f"⛔ TypeError ({e}) → autre variante…")
            except Exception as e:
                last_err = e
                logging.debug(f"❌ Échec fmt='{fmt}' → {e}")

    logging.error(f"❌ Échec GetFeature (dernière erreur: {last_err})")
    return gpd.GeoDataFrame(), None

# -----------------------------
# Correction CRS (heuristiques)
# -----------------------------
def _guess_epsg_from_bounds(bounds):
    if not bounds:
        return None
    minx, miny, maxx, maxy = bounds
    if (100_000 <= minx <= 1_300_000) and (6_000_000 <= miny <= 7_200_000):
        return 2154
    LIM = 20_100_000
    if (-LIM <= minx <= LIM) and (-LIM <= miny <= LIM) and (-LIM <= maxx <= LIM) and (-LIM <= maxy <= LIM):
        if not (-180 <= minx <= 180 and -90 <= miny <= 90 and -180 <= maxx <= 180 and -90 <= maxy <= 90):
            return 3857
    if (-180 <= minx <= 180) and (-90 <= miny <= 90) and (-180 <= maxx <= 180) and (-90 <= maxy <= 90):
        return 4326
    return None

def _enforce_target_crs(gdf, layer_name, target_epsg=4326, assumed_query_epsg=4326):
    if gdf is None or gdf.empty:
        return gdf
    bounds = tuple(gdf.total_bounds.tolist())
    epsg_guess = _guess_epsg_from_bounds(bounds)
    reported = None
    try:
        if gdf.crs is not None:
            reported = str(gdf.crs)
    except Exception:
        reported = None
    logging.info(f"🧭 {layer_name}: bounds={bounds} | crs_reported={reported} | guess={epsg_guess}")
    if epsg_guess in (2154, 3857):
        if (gdf.crs is None) or (str(gdf.crs).upper() == f"EPSG:{assumed_query_epsg}".upper()):
            logging.info(f"🔧 {layer_name}: correction CRS → EPSG:{epsg_guess} (puis reprojection)")
            gdf = gdf.set_crs(epsg_guess, allow_override=True)
    if gdf.crs is None:
        logging.info(f"🔧 {layer_name}: CRS manquant → set EPSG:{assumed_query_epsg}")
        gdf = gdf.set_crs(assumed_query_epsg, allow_override=True)
    try:
        cur = int(str(gdf.crs).split(":")[-1])
    except Exception:
        cur = None
    if (target_epsg is not None) and (cur != target_epsg):
        logging.info(f"🔄 {layer_name}: reprojection EPSG:{cur} → EPSG:{target_epsg}")
        gdf = gdf.to_crs(target_epsg)
    return gdf

# -----------------------------
# Carroyage adaptatif + stats tuiles
# -----------------------------
def subdivide_bbox(b):
    minx, miny, maxx, maxy = b
    mx, my = (minx+maxx)/2.0, (miny+maxy)/2.0
    return [(minx, miny, mx, my), (mx, miny, maxx, my), (minx, my, mx, maxy), (mx, my, maxx, maxy)]

def harvest_adaptive_with_owslib(wfs_url, layer, bbox, srs='EPSG:4326', cap=5000, max_level=8, sleep_s=0.1):
    """
    Retourne (gdf_full, tiles_stats)
    tiles_stats: liste dicts {bbox, level, n, fmt}
    """
    stack: List[Tuple[Tuple[float,float,float,float], int]] = [(bbox, 0)]
    parts = []
    tiles_stats: List[Dict[str, Any]] = []
    tile_idx = 0

    print(f"📡 Carroyage adaptatif pour {layer}...")
    while stack:
        b, level = stack.pop()
        tile_idx += 1
        print(f"  📦 Tuile #{tile_idx} (niveau {level}): {len(parts)} entités collectées")
        gdf_tile, fmt_used = fetch_wfs_bbox(wfs_url, layer, b, srs=srs)
        n = len(gdf_tile)
        tiles_stats.append({"bbox": b, "level": level, "n": n, "format": fmt_used})
        if n >= cap and level < max_level:
            print(f"    → Saturation (≥{cap}) → subdivision niveau {level+1}")
            for child in subdivide_bbox(b):
                stack.append((child, level+1))
        elif n > 0:
            parts.append(gdf_tile)
            print(f"    → +{n} entités (total: {len(parts)})")
        time.sleep(sleep_s)

    print(f" Carroyage terminé: {len(parts)} tuiles avec données")
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs=srs), tiles_stats

    full = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=srs)
    if not full.empty:
        full = _enforce_target_crs(full, layer_name=layer, target_epsg=4326,
                                   assumed_query_epsg=int(srs.split(':')[-1]) if ':' in srs else 4326)
    return full, tiles_stats

# -----------------------------
# Dédup
# -----------------------------
def detect_id_column(gdf: gpd.GeoDataFrame):
    cands = [c for c in gdf.columns if c.lower() in ("gml_id","gmlid","id","fid","identifiant","uuid")]
    return cands[0] if cands else None

def dedup_on_id_or_geom(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    id_col = detect_id_column(gdf)
    if id_col:
        return gdf.drop_duplicates(subset=id_col, keep="first")
    g = gdf.copy()
    g["__wkb__"] = g.geometry.apply(lambda x: x.wkb_hex if x is not None else None)
    return g.drop_duplicates(subset="__wkb__", keep="first").drop(columns="__wkb__")

def hash_ids(gdf):
    col = detect_id_column(gdf)
    if not col:
        return None
    s = "|".join("" if x is None else str(x) for x in gdf[col].tolist())
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# -----------------------------
# Estimation taille
# -----------------------------
def estimate_bytes(gdf: gpd.GeoDataFrame) -> dict:
    if gdf.empty:
        return {"rows": 0, "attrs_bytes": 0, "geom_bytes": 0, "total_bytes": 0}
    attrs = gdf.drop(columns=gdf.geometry.name)
    attrs_bytes = int(attrs.memory_usage(deep=True).sum())
    geom_bytes = int(sum(len(g.wkb) if g is not None else 0 for g in gdf.geometry))
    return {"rows": len(gdf), "attrs_bytes": attrs_bytes, "geom_bytes": geom_bytes,
            "total_bytes": attrs_bytes + geom_bytes}

# -----------------------------
# Connexion Supabase
# -----------------------------
def _clean_host(h: str) -> str:
    if not h:
        return ""
    h = h.strip().replace("https://", "").replace("http://", "").split("/")[0]
    if not h.startswith("db."):
        h = "db." + h
    return h

def mk_engine_from_env():
    PG_HOST = _clean_host(os.getenv("SUPABASE_HOST", "db.<project>.supabase.co"))
    PG_DB   = os.getenv("SUPABASE_DB",   "postgres")
    PG_USER = os.getenv("SUPABASE_USER", "postgres")
    PG_PASS = os.getenv("SUPABASE_PASSWORD", "")
    _port   = os.getenv("SUPABASE_PORT", "6543").strip()
    PG_PORT = int(_port) if _port else 6543

    from sqlalchemy import create_engine
    from urllib.parse import quote_plus
    pwd = quote_plus(PG_PASS)

    # IMPORTANT: options pour désactiver le statement_timeout
    # -c statement_timeout=0  => pas de limite
    # -c idle_in_transaction_session_timeout=0  => pas de kill de sessions longues
    url = f"postgresql+psycopg2://{PG_USER}:{pwd}@{PG_HOST}:{PG_PORT}/{PG_DB}?sslmode=require"
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "connect_timeout": 20,
            "options": "-c statement_timeout=0 -c idle_in_transaction_session_timeout=0"
        }
    )
    return eng

def ensure_geom_name(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.geometry.name != "geom":
        return gdf.set_geometry(gdf.geometry.name).rename_geometry("geom")
    return gdf

# -----------------------------
# Introspection tables
# -----------------------------
def table_exists(engine, schema: str, table: str) -> bool:
    with engine.begin() as con:
        v = con.execute(text("""
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema=:s AND table_name=:t
            LIMIT 1
        """), {"s": schema, "t": table}).scalar()
    return bool(v)

def table_columns(engine, schema: str, table: str) -> set[str]:
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=:s AND table_name=:t
        """), {"s": schema, "t": table}).fetchall()
    return set(r[0] for r in rows)

# -----------------------------
# Push DB avec fallback COPY → INSERT chunké
# -----------------------------
def _to_postgis_with_fallback(gdf, engine, table, schema, if_exists="replace"):
    """
    Écriture PostGIS avec fallback : COPY rapide → INSERT chunké si timeout.
    """
    try:
        gdf.to_postgis(table, engine, schema=schema, if_exists=if_exists, index=False)
        return "copy"
    except Exception as e:
        msg = str(e).lower()
        if "statement timeout" in msg or "querycanceled" in msg or "canceling statement" in msg:
            logging.warning(f"⚠️ Timeout COPY sur {schema}.{table} → fallback INSERT chunké")
            gdf.to_postgis(
                table, engine, schema=schema, if_exists=if_exists, index=False,
                method="multi", chunksize=2000
            )
            return "insert-multi"
        raise

def push_gdf_full_refresh(gdf: gpd.GeoDataFrame, engine, table: str, schema: str = "public"):
    gdf = ensure_geom_name(gdf)
    mode = _to_postgis_with_fallback(gdf, engine, table, schema=schema, if_exists="replace")
    idx_name = safe_ident(f"{table}_geom_gix")
    with engine.begin() as con:
        con.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS id bigserial PRIMARY KEY;'))
        con.execute(text(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{schema}"."{table}" USING GIST (geom);'))
        con.execute(text(f'ANALYZE "{schema}"."{table}";'))
    logging.info(f"🧾 {schema}.{table} écrit (mode: {mode}, full refresh)")

def push_gdf_upsert(gdf: gpd.GeoDataFrame, engine, table: str, id_col: str, schema: str = "public"):
    gdf = ensure_geom_name(gdf)
    staging_raw = f"_{table}_staging"
    staging = safe_ident(staging_raw)
    idx_name = safe_ident(f"{table}_geom_gix")

    # 1) staging avec fallback
    mode = _to_postgis_with_fallback(gdf, engine, staging, schema=schema, if_exists="replace")

    with engine.begin() as con:
        # 2) préparer la table cible
        con.execute(text(f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" AS SELECT * FROM "{schema}"."{staging}" WHERE false;'))
        con.execute(text(f'''
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM information_schema.table_constraints
                WHERE table_schema='{schema}' AND table_name='{table}' AND constraint_type='PRIMARY KEY'
              ) THEN
                ALTER TABLE "{schema}"."{table}" ADD PRIMARY KEY ("{id_col}");
              END IF;
            END$$;
        '''))
        con.execute(text(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{schema}"."{table}" USING GIST (geom);'))

        # 3) colonnes et merge
        cols = con.execute(text(f'''
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='{schema}' AND table_name='{staging}';
        ''')).fetchall()
        cols = [c[0] for c in cols]
        cols_no_pk = [c for c in cols if c != id_col]
        set_clause = ", ".join([f'"{c}"=EXCLUDED."{c}"' for c in cols_no_pk])

        con.execute(text(f'''
            INSERT INTO "{schema}"."{table}" ({", ".join(f'"{c}"' for c in cols)})
            SELECT {", ".join(f'"{c}"' for c in cols)} FROM "{schema}"."{staging}"
            ON CONFLICT ("{id_col}") DO UPDATE SET {set_clause};
        '''))
        con.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{staging}";'))
        con.execute(text(f'ANALYZE "{schema}"."{table}";'))

    logging.info(f"🧾 {schema}.{table} upsert (staging via {mode}, key={id_col})")

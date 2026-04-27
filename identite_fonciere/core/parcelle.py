"""
core/parcelle.py
Récupération de la géométrie d'une parcelle depuis la base cadastrale interne.
Retourne un GeoDataFrame EPSG:4326 + métadonnées.
"""
from __future__ import annotations

import logging
import os
import time
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

import geopandas as gpd
import psycopg2
from psycopg2 import sql
from shapely.geometry import mapping, shape

logger = logging.getLogger(__name__)

DB_RETRY_COUNT = 2
DB_RETRY_BACKOFF_S = 0.35
TABLE_CANDIDATES = (("parcelles", "parcelles"), ("ecocompensation_results", "parcelles"))


def _deps_from_insee(insee: str) -> list[str]:
    code = (insee or "").strip().upper()
    if len(code) >= 3 and code[:2] in {"97", "98"}:
        return [code[:3]]
    if len(code) >= 2:
        return [code[:2]]
    return []


def _db_connect_with_retry(
    timeout: int,
    retries: int = DB_RETRY_COUNT,
    backoff_s: float = DB_RETRY_BACKOFF_S,
):
    direct_url = (os.getenv("SUPABASE_DIRECT_URL") or "").strip()
    if not direct_url:
        raise RuntimeError("SUPABASE_DIRECT_URL manquant")

    attempts = retries + 1
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return psycopg2.connect(direct_url, sslmode="require", connect_timeout=timeout)
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(backoff_s * (i + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


@lru_cache(maxsize=1)
def _resolve_cadastre_source() -> tuple[str, str, bool]:
    conn = _db_connect_with_retry(timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                for schema, table in TABLE_CANDIDATES:
                    cur.execute(
                        """
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = %s
                          AND table_name = %s
                        LIMIT 1
                        """,
                        (schema, table),
                    )
                    if not cur.fetchone():
                        continue
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = %s
                              AND table_name = %s
                              AND column_name = 'code_dep'
                        )
                        """,
                        (schema, table),
                    )
                    has_code_dep = bool(cur.fetchone()[0])
                    return schema, table, has_code_dep
    finally:
        conn.close()
    raise RuntimeError("Aucune table cadastrale trouvée")


@dataclass
class ParcelleRef:
    section: str
    numero: str
    insee: str
    commune: str

    def __post_init__(self):
        self.section = self.section.upper().strip()
        self.numero = self.numero.strip().zfill(4)
        self.commune = self.commune.strip()
        self.insee = self.insee.strip()

    @property
    def label(self) -> str:
        return f"{self.section} {self.numero}"


@dataclass
class ParcelleResult:
    ref: ParcelleRef
    gdf: gpd.GeoDataFrame          # EPSG:4326, 1 ligne
    geojson: Dict                   # geometry GeoJSON
    contenance: Optional[float] = None
    idu: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_parcelle(ref: ParcelleRef, timeout: int = 30) -> ParcelleResult:
    """
    Récupère la géométrie et les attributs d'une parcelle depuis la base locale.
    Retourne toujours un ParcelleResult (error != None si échec).
    """
    logger.info("🔍 DB cadastre — parcelle %s %s (INSEE: %s)", ref.section, ref.numero, ref.insee)
    try:
        schema, table, has_code_dep = _resolve_cadastre_source()
        deps = _deps_from_insee(ref.insee)
        conn = _db_connect_with_retry(timeout=timeout)
        try:
            with conn:
                with conn.cursor() as cur:
                    dep_filter_sql = sql.SQL("AND code_dep = ANY(%s)") if has_code_dep else sql.SQL("")
                    query = sql.SQL(
                        """
                        SELECT
                            idu,
                            contenance,
                            ST_AsGeoJSON(ST_Transform(geom_2154, 4326)) AS geom_geojson
                        FROM {}.{}
                        WHERE code_insee = %s
                          AND section = %s
                          AND (numero = %s OR ltrim(numero, '0') = ltrim(%s, '0'))
                          {}
                        LIMIT 1
                        """
                    ).format(sql.Identifier(schema), sql.Identifier(table), dep_filter_sql)
                    params: list[object] = [ref.insee, ref.section, ref.numero, ref.numero]
                    if has_code_dep:
                        params.append(deps)
                    cur.execute(query, params)
                    row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        return _error(ref, f"Erreur lecture base cadastrale : {e}")

    if not row:
        return _error(ref, f"Parcelle {ref.label} non trouvée en base (INSEE {ref.insee})")

    idu_raw, contenance_raw, geom_geojson = row
    if not geom_geojson:
        return _error(ref, f"Géométrie absente pour la parcelle {ref.label}")

    geom = shape(geom_geojson if isinstance(geom_geojson, dict) else json.loads(geom_geojson))
    geojson = mapping(geom)
    gdf = gpd.GeoDataFrame([{"geometry": geom}], geometry="geometry", crs="EPSG:4326")
    contenance = None if contenance_raw is None else float(contenance_raw)
    idu = str(idu_raw).strip() if idu_raw is not None else None

    logger.info("✅ Parcelle %s récupérée en base (contenance: %s m²)", ref.label, contenance)
    return ParcelleResult(
        ref=ref,
        gdf=gdf,
        geojson=geojson,
        contenance=contenance,
        idu=idu,
    )


def fetch_parcelles(refs: List[ParcelleRef]) -> List[ParcelleResult]:
    """Récupère plusieurs parcelles (séquentiel, suffisant pour une UF < 20 parcelles)."""
    results = []
    for ref in refs:
        results.append(fetch_parcelle(ref))
    ok = sum(1 for r in results if r.ok)
    logger.info("📦 %d/%d parcelles récupérées", ok, len(refs))
    return results


def _error(ref: ParcelleRef, msg: str) -> ParcelleResult:
    logger.error("❌ %s", msg)
    import geopandas as gpd
    return ParcelleResult(ref=ref, gdf=gpd.GeoDataFrame(), geojson={}, error=msg)

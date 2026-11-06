"""Autocomplétion des taxons flore. GET /api/flore/taxons?q=&limit=20"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query
from sqlalchemy import text

from db import get_engine

logger = logging.getLogger("kerfoncier.flore")
router = APIRouter()

_SQL_PATH = Path(__file__).resolve().parents[3] / "db" / "sql" / "flore_observation.sql"


def ensure_flore_schema(engine=None) -> None:
    eng = engine or get_engine()
    sql = _SQL_PATH.read_text(encoding="utf-8")
    parts = [p.strip() for p in sql.split(";") if p.strip() and not p.strip().startswith("--")]
    with eng.begin() as conn:
        for part in parts:
            # retire les lignes de commentaire en tête de bloc
            lines = [ln for ln in part.splitlines() if not ln.strip().startswith("--")]
            stmt = "\n".join(lines).strip()
            if stmt:
                conn.execute(text(stmt))


@router.get("/taxons")
def list_taxons(
    q: str = Query("", description="Recherche sur nom scientifique ou vernaculaire"),
    limit: int = Query(20, ge=1, le=100),
):
    ensure_flore_schema()
    needle = f"%{q.strip()}%"
    with get_engine().connect() as conn:
        if q.strip():
            rows = conn.execute(
                text("""
                    SELECT taxon_key, nom_scientifique, nom_vernaculaire,
                           cd_nom_valide, nb_observations, derniere_observation
                    FROM ecocompensation.v_flore_taxons
                    WHERE nom_scientifique ILIKE :q
                       OR nom_vernaculaire ILIKE :q
                    ORDER BY nb_observations DESC, nom_scientifique
                    LIMIT :lim
                """),
                {"q": needle, "lim": limit},
            ).mappings().all()
        else:
            rows = conn.execute(
                text("""
                    SELECT taxon_key, nom_scientifique, nom_vernaculaire,
                           cd_nom_valide, nb_observations, derniere_observation
                    FROM ecocompensation.v_flore_taxons
                    ORDER BY nb_observations DESC, nom_scientifique
                    LIMIT :lim
                """),
                {"lim": limit},
            ).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("derniere_observation") is not None:
            item["derniere_observation"] = item["derniere_observation"].isoformat()
        out.append(item)
    return out

from __future__ import annotations

import logging
import os
import json
from pathlib import Path

from sqlalchemy import bindparam, text

from .base import BasePoolProfiler

logger = logging.getLogger(__name__)

# Base Ecocompensation : copie post-migration (scripts/migrate_ppm.py) — repli optionnel
DEFAULT_PM_TABLE_CORE = "ecocompensation.parcelles_personnes_morales"
# Source actuelle : ancienne base PPM (SUPABASE_PPM_*), schéma public — défaut tant que la migration n’est pas faite
DEFAULT_PM_TABLE_PPM = "public.parcelles_personnes_morales"

_NOMENCLATURE_N3: dict[str, str] | None = None


def _load_nomenclature_n3() -> dict[str, str]:
    global _NOMENCLATURE_N3
    if _NOMENCLATURE_N3 is not None:
        return _NOMENCLATURE_N3

    p = Path(__file__).resolve().parent / "categories_juridiques_niveau_3.json"
    if not p.exists():
        logger.warning("Nomenclature N3 introuvable: %s", p)
        _NOMENCLATURE_N3 = {}
        return _NOMENCLATURE_N3

    try:
        with open(p, encoding="utf-8") as f:
            entries = json.load(f)
        _NOMENCLATURE_N3 = {
            str(e.get("code_juridique", "")).strip().zfill(4): str(e.get("categorie_juridique", "")).strip()
            for e in entries
            if e.get("code_juridique") is not None
        }
    except Exception:
        logger.exception("Impossible de charger la nomenclature N3 (%s)", p)
        _NOMENCLATURE_N3 = {}
    return _NOMENCLATURE_N3


def _forme_juridique_n3_label(forme_juridique: object) -> str | None:
    if forme_juridique is None:
        return None
    code4 = str(forme_juridique).strip()
    if not code4:
        return None
    code4 = code4.zfill(4)
    libelle = _load_nomenclature_n3().get(code4)
    if libelle:
        return libelle
    return f"Forme juridique inconnue (code {code4})"


def _empty_payload() -> dict[str, object]:
    return {
        "intersects_pm_database": False,
        "siren": None,
        "denomination": None,
        "forme_juridique": None,
    }


def _hit_payload(
    siren: object,
    denomination: object,
    forme_juridique: object,
) -> dict[str, object]:
    return {
        "intersects_pm_database": True,
        "siren": None if siren is None else str(siren).strip() or None,
        "denomination": None if denomination is None else str(denomination).strip() or None,
        "forme_juridique": _forme_juridique_n3_label(forme_juridique),
    }


class PersonnesMoralesProfiler(BasePoolProfiler):
    """
    Indique si la parcelle cadastrale figure dans la base « parcelles personnes morales »
    (SIREN / dénomination / forme juridique).

    Priorité (tant que les données ne sont pas migrées sur Ecocompensation) :
      1) Requête sur la base PPM (get_engine_ppm / SUPABASE_PPM_*) — table public.parcelles_personnes_morales
      2) Repli : jointure sur ecocompensation.parcelles_personnes_morales si la PPM est indisponible.
    """

    metric_key = "parcelles_personnes_morales"

    def _all_idus(self, conn, project_id: str, run_id: str) -> list[str]:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT pp.idu
                FROM ecocompensation_results.parcelles_pool pp
                WHERE pp.project_id = CAST(:project_id AS uuid)
                  AND pp.run_id = CAST(:run_id AS uuid)
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def _hits_from_core(self, conn, project_id: str, run_id: str, pm_table: str) -> dict[str, dict[str, object]]:
        sql = text(
            f"""
            SELECT DISTINCT ON (p.idu)
                p.idu,
                ppm.siren,
                ppm.denomination,
                ppm.forme_juridique
            FROM ecocompensation_results.parcelles_pool pp
            JOIN ecocompensation_results.parcelles p
              ON p.project_id = pp.project_id
             AND p.idu = pp.idu
            JOIN {pm_table} ppm
              ON ppm.idu = p.idu
            WHERE pp.project_id = CAST(:project_id AS uuid)
              AND pp.run_id = CAST(:run_id AS uuid)
            ORDER BY p.idu, ppm.siren NULLS LAST
            """
        )
        rows = conn.execute(
            sql,
            {"project_id": project_id, "run_id": run_id},
        ).mappings().all()
        out: dict[str, dict[str, object]] = {}
        for r in rows:
            idu = str(r["idu"])
            out[idu] = _hit_payload(r.get("siren"), r.get("denomination"), r.get("forme_juridique"))
        return out

    def _hits_from_ppm_engine(self, idus: list[str], pm_table: str) -> dict[str, dict[str, object]]:
        if not idus:
            return {}
        from db import get_engine_ppm

        engine_ppm = get_engine_ppm()
        out: dict[str, dict[str, object]] = {}
        # Requêtes par paquets pour éviter les paramètres trop gros
        chunk = 800
        sql = (
            text(
                f"""
                SELECT idu, siren, denomination, forme_juridique
                FROM {pm_table}
                WHERE idu IN :idus
                """
            )
            .bindparams(bindparam("idus", expanding=True))
        )
        with engine_ppm.connect() as c2:
            for i in range(0, len(idus), chunk):
                part = idus[i : i + chunk]
                rows = c2.execute(sql, {"idus": part}).mappings().all()
                for r in rows:
                    idu = str(r["idu"])
                    out[idu] = _hit_payload(r.get("siren"), r.get("denomination"), r.get("forme_juridique"))
        return out

    def compute_for_run(self, conn, project_id: str, run_id: str) -> dict[str, dict]:
        all_idus = self._all_idus(conn, project_id, run_id)
        if not all_idus:
            return {}

        pm_table_core = os.getenv("PARCELLES_PM_CORE_TABLE", DEFAULT_PM_TABLE_CORE)
        pm_table_ppm = os.getenv("PARCELLES_PM_TABLE", DEFAULT_PM_TABLE_PPM)

        hits: dict[str, dict[str, object]] = {}
        try:
            hits = self._hits_from_ppm_engine(all_idus, pm_table_ppm)
        except Exception as e:
            logger.warning(
                "Profiler PM : base PPM (%s) indisponible (%s) — repli table migrée %s",
                pm_table_ppm,
                e,
                pm_table_core,
            )
            try:
                hits = self._hits_from_core(conn, project_id, run_id, pm_table_core)
            except Exception:
                logger.exception(
                    "Profiler PM : échec repli core (project_id=%s, run_id=%s)",
                    project_id,
                    run_id,
                )
                hits = {}

        payload: dict[str, dict] = {}
        base = _empty_payload()
        for idu in all_idus:
            payload[idu] = dict(base)
            if idu in hits:
                payload[idu].update(hits[idu])
        return payload

# -*- coding: utf-8 -*-
"""
Cache Postgres des résultats (table existante `ecocompensation.durete_fonciere_cache`).

Aucune migration nécessaire : les ajouts V4 voyagent dans la colonne JSONB
`detail_axes` (clé `v4`). La colonne `score_llm_base` garde son nom historique
mais contient désormais le score DÉTERMINISTE.

CACHE_VERSION est dérivée de SCORING_VERSION : modifier un barème dans
config.py invalide automatiquement les anciens résultats.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .config import SCORING_VERSION

logger = logging.getLogger("durete.cache")

CACHE_VERSION = f"v{SCORING_VERSION}"
CACHE_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_cached_siren(conn, siren: str) -> dict | None:
    row = conn.execute(text("""
        SELECT score_llm_base, niveau_durete, explication, detail_axes, statut,
               avertissements, denomination, forme_juridique, computed_at
        FROM ecocompensation.durete_fonciere_cache
        WHERE siren = :siren AND cache_version = :version AND expires_at > :now
    """), {"siren": siren, "version": CACHE_VERSION, "now": _now()}).mappings().one_or_none()
    if row is None:
        return None
    detail = row["detail_axes"] or {}
    if isinstance(detail, str):
        detail = json.loads(detail)
    v4 = detail.get("v4") or {}
    return {
        "siren": siren,
        "score_final": row["score_llm_base"],
        "niveau_durete": row["niveau_durete"],
        "explication": row["explication"],
        "detail_axes": detail,
        "statut": row["statut"],
        "avertissements": list(row["avertissements"] or []),
        "denomination": row["denomination"],
        "forme_juridique": row["forme_juridique"],
        "indice_confiance": v4.get("indice_confiance"),
        "confiance": v4.get("confiance"),
        "recommandations": v4.get("recommandations", []),
        "computed_at": row["computed_at"].isoformat() if row["computed_at"] else None,
        "_from_cache": True,
    }


def set_cached_siren(conn, siren: str, res: dict, denomination: str = "", forme_juridique: str = "") -> None:
    if res.get("statut") != "ok" or res.get("score_fiable") is False:
        return  # ni erreur ni résultat dégradé (sources muettes) en cache : on retentera
    conn.execute(text("""
        INSERT INTO ecocompensation.durete_fonciere_cache (
            siren, cache_version, score_llm_base, niveau_durete, explication,
            detail_axes, statut, avertissements, denomination, forme_juridique,
            computed_at, expires_at
        ) VALUES (
            :siren, :version, :score, :niveau, :explication,
            CAST(:detail AS jsonb), :statut, CAST(:avert AS jsonb), :denomination, :forme,
            :now, :expires
        )
        ON CONFLICT (siren, cache_version) DO UPDATE SET
            score_llm_base = EXCLUDED.score_llm_base, niveau_durete = EXCLUDED.niveau_durete,
            explication = EXCLUDED.explication, detail_axes = EXCLUDED.detail_axes,
            statut = EXCLUDED.statut, avertissements = EXCLUDED.avertissements,
            denomination = EXCLUDED.denomination, forme_juridique = EXCLUDED.forme_juridique,
            computed_at = EXCLUDED.computed_at, expires_at = EXCLUDED.expires_at
    """), {
        "siren": siren, "version": CACHE_VERSION,
        "score": res.get("score_final"), "niveau": res.get("niveau_durete"),
        "explication": res.get("explication"),
        "detail": json.dumps(res.get("detail_axes") or {}, ensure_ascii=False, default=str),
        "statut": res.get("statut", "ok"),
        "avert": json.dumps(res.get("avertissements") or [], ensure_ascii=False),
        "denomination": denomination or res.get("denomination") or "",
        "forme": forme_juridique or res.get("forme_juridique") or "",
        "now": _now(), "expires": _now() + timedelta(days=CACHE_TTL_DAYS),
    })
    logger.debug("cache set %s (%s)", siren, CACHE_VERSION)

# -*- coding: utf-8 -*-
"""
Fichier des parcelles des personnes morales (DGFiP) — table `parcelles_personnes_morales`.

Évolutions vs V3 :
  - deux fournisseurs interchangeables : Supabase (REST) ou connexion SQLAlchemy
    directe (Postgres auto-hébergé OVH) → pas de dépendance au client Supabase ;
  - lecture tolérante au schéma (`select *`) : on exploite `code_droit`,
    `groupe_personne`… s'ils existent ;
  - NOUVEAUX signaux :
      * co-propriété / indivision entre PM sur une même parcelle (guide axe 2 : 22/25),
      * démembrement (usufruit / nue-propriété),
      * droits réels de tiers (emphytéose, bail à construction),
      * patrimoine total de la PM (nb parcelles, surface, communes).
Limite : le fichier ne liste que les PM ; des co-indivisaires personnes physiques
restent invisibles.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Iterable, Optional, Protocol

from ..cadastre import normaliser_idu

log = logging.getLogger("durete.parcelles")

TABLE = os.getenv("DURETE_TABLE_PARCELLES_PM", "parcelles_personnes_morales")

CODES_DROIT = {
    "P": "propriétaire", "U": "usufruitier", "N": "nu-propriétaire",
    "B": "bailleur à construction", "R": "preneur à construction",
    "E": "emphytéote", "S": "syndic de copropriété", "G": "gérant / mandataire",
    "H": "associé (société en transparence fiscale)",
}
DROITS_DEMEMBREMENT = {"U", "N"}
DROITS_REELS_TIERS = {"E", "R"}


class ParcellesProvider(Protocol):
    def par_siren(self, siren: str) -> list[dict]: ...
    def par_idus(self, idus: list[str]) -> list[dict]: ...


class SupabaseProvider:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None, table: str = TABLE):
        self.url = url or os.getenv("SUPABASE_URL_PPM") or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_KEY_PPM") or os.getenv("SUPABASE_KEY")
        self.table = table
        self._client = None

    def _c(self):
        if self._client is None:
            from supabase import create_client  # import paresseux
            self._client = create_client(self.url, self.key)
        return self._client

    def par_siren(self, siren: str) -> list[dict]:
        return self._c().table(self.table).select("*").eq("siren", siren).limit(10000).execute().data or []

    def par_idus(self, idus: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(idus), 200):
            out += self._c().table(self.table).select("*").in_("idu", idus[i:i + 200]).execute().data or []
        return out


class SqlProvider:
    """Accès direct Postgres (SQLAlchemy Connection ou Engine)."""

    def __init__(self, conn: Any, table: str = TABLE, schema: Optional[str] = None):
        self.conn, self.fq = conn, f"{schema}.{table}" if schema else table

    def _q(self, sql: str, params: dict) -> list[dict]:
        from sqlalchemy import text
        res = self.conn.execute(text(sql), params)
        return [dict(r) for r in res.mappings()]

    def par_siren(self, siren: str) -> list[dict]:
        return self._q(f"SELECT * FROM {self.fq} WHERE siren = :s", {"s": siren})

    def par_idus(self, idus: list[str]) -> list[dict]:
        return self._q(f"SELECT * FROM {self.fq} WHERE idu = ANY(:i)", {"i": list(idus)})


@lru_cache(maxsize=1)
def provider_par_defaut() -> Optional[ParcellesProvider]:
    if (os.getenv("SUPABASE_URL_PPM") or os.getenv("SUPABASE_URL")) and \
            (os.getenv("SUPABASE_KEY_PPM") or os.getenv("SUPABASE_KEY")):
        return SupabaseProvider()
    return None


def _surface(r: dict) -> float:
    for k in ("contenance", "surface_m2", "surface"):
        if r.get(k) not in (None, ""):
            try:
                return float(r[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def _idu(r: dict) -> Optional[str]:
    try:
        return normaliser_idu(r["idu"]) if r.get("idu") else None
    except ValueError:
        return None


def analyser_patrimoine(rows_siren: list[dict], siren: str) -> dict:
    """Patrimoine total de la PM d'après le fichier PM (pur)."""
    idus = sorted({i for i in (_idu(r) for r in rows_siren) if i})
    surf = sum(_surface(r) for r in rows_siren)
    communes = sorted({str(r.get("code_insee") or (i or "")[:5]) for r, i in
                       ((r, _idu(r)) for r in rows_siren) if (r.get("code_insee") or i)})
    return {
        "nb_parcelles": len(idus),
        "surface_totale_ha": round(surf / 10_000, 2),
        "nb_communes": len(communes),
        "communes_insee": communes[:50],
        "denomination": next((r.get("denomination") for r in rows_siren if r.get("denomination")), None),
        "millesime": next((str(r.get(k)) for r in rows_siren for k in ("millesime", "annee")
                           if r.get(k)), None),
        "idus": idus,
        "surfaces": {i: _surface(r) for r in rows_siren if (i := _idu(r))},
    }


def analyser_droits(rows_idus: Iterable[dict], siren: str, idus: list[str]) -> dict[str, dict]:
    """Par parcelle : droit de la PM, co-titulaires PM, démembrement, droits réels tiers (pur)."""
    par_idu: dict[str, list[dict]] = {i: [] for i in idus}
    for r in rows_idus:
        i = _idu(r)
        if i in par_idu:
            par_idu[i].append(r)
    out = {}
    for i, rows in par_idu.items():
        propres = [r for r in rows if str(r.get("siren")) == siren]
        autres = [r for r in rows if str(r.get("siren")) != siren]
        droits_pm = sorted({str(r.get("code_droit") or "").upper() for r in propres if r.get("code_droit")})
        droits_autres = {str(r.get("code_droit") or "").upper() for r in autres if r.get("code_droit")}
        out[i] = {
            "connu_fichier_pm": bool(rows),
            "droits_pm": [{"code": c, "libelle": CODES_DROIT.get(c, f"code {c}")} for c in droits_pm],
            "co_titulaires_pm": [
                {"siren": str(r.get("siren")), "denomination": r.get("denomination"),
                 "code_droit": r.get("code_droit")}
                for r in autres
            ][:10],
            "demembrement": bool(DROITS_DEMEMBREMENT & (set(droits_pm) | droits_autres)),
            "droit_reel_tiers": bool(DROITS_REELS_TIERS & droits_autres),
            "pm_non_proprietaire": bool(droits_pm) and "P" not in droits_pm and not
            ({"U", "N"} & set(droits_pm)),
            "surface_m2": max((_surface(r) for r in rows), default=None),
        }
    return out

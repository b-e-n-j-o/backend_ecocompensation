"""
Recherche Sitadel × cadastre actuel, par IDU.

Tables lues (schéma ecocompensation, déjà croisées) :
  sitadel_parcelle       lien dossier → IDU actuel
  sitadel_autorisation   statut de géolocalisation
  sitadel_logements / locaux / pa / pd   dates, état, demandeur

Voisine = parcelle du même département dont le contour touche celui de la
cible (ST_Touches), y compris un contact par un seul sommet. On ne retient
que les voisines qui portent déjà un dossier Sitadel.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import bindparam, text

from .signal_score import DEPS_COUVERTS, etat_libelle

log = logging.getLogger("autorisations_urba")

SCHEMA = "ecocompensation"
_CHUNK_SPATIAL = 40
_CHUNK_DIRECT = 200
_CHUNK_SOURCES = 100

# Lecture des dates / état dossier par dossier, après avoir trouvé les clés.
# Une jointure des quatre jeux complets force un parcours trop large.
_SOURCES = {
    "logements": f"""
        SELECT DISTINCT ON (num_autorisation)
            num_autorisation,
            type_dau,
            etat_dau AS etat,
            date_reelle_autorisation AS date_autorisation,
            date_reelle_doc AS date_doc,
            date_reelle_daact AS date_daact,
            an_depot,
            dr_depot AS date_depot,
            denom_dem,
            siren_dem,
            nature_projet_declaree AS nature_projet,
            destination_principale,
            nb_lgt_tot_crees,
            surf_hab_creee,
            superficie_terrain,
            zone_op
        FROM {SCHEMA}.sitadel_logements
        WHERE num_autorisation IN :nums
        ORDER BY num_autorisation
    """,
    "locaux": f"""
        SELECT DISTINCT ON (num_autorisation)
            num_autorisation,
            type_dau,
            etat_dau AS etat,
            date_reelle_autorisation AS date_autorisation,
            date_reelle_doc AS date_doc,
            date_reelle_daact AS date_daact,
            an_depot,
            dr_depot AS date_depot,
            denom_dem,
            siren_dem,
            nature_projet_declaree AS nature_projet,
            destination_principale,
            CAST(NULL AS numeric) AS nb_lgt_tot_crees,
            surf_hab_creee,
            superficie_terrain,
            zone_op
        FROM {SCHEMA}.sitadel_locaux
        WHERE num_autorisation IN :nums
        ORDER BY num_autorisation
    """,
    "pa": f"""
        SELECT DISTINCT ON (num_autorisation)
            num_autorisation,
            CAST('PA' AS text) AS type_dau,
            etat_pa AS etat,
            date_reelle_autorisation AS date_autorisation,
            date_reelle_doc AS date_doc,
            date_reelle_daact AS date_daact,
            an_depot,
            dr_depot AS date_depot,
            denom_dem,
            siren_dem,
            CAST(NULL AS text) AS nature_projet,
            CAST(NULL AS text) AS destination_principale,
            CAST(NULL AS numeric) AS nb_lgt_tot_crees,
            CAST(NULL AS numeric) AS surf_hab_creee,
            superficie_terrain,
            zone_op
        FROM {SCHEMA}.sitadel_pa
        WHERE num_autorisation IN :nums
        ORDER BY num_autorisation
    """,
    "pd": f"""
        SELECT DISTINCT ON (num_autorisation)
            num_autorisation,
            CAST('PD' AS text) AS type_dau,
            etat_pd AS etat,
            date_reelle_autorisation AS date_autorisation,
            CAST(NULL AS date) AS date_doc,
            CAST(NULL AS date) AS date_daact,
            an_depot,
            dr_depot AS date_depot,
            denom_dem,
            siren_dem,
            CAST(NULL AS text) AS nature_projet,
            CAST(NULL AS text) AS destination_principale,
            CAST(NULL AS numeric) AS nb_lgt_tot_crees,
            CAST(NULL AS numeric) AS surf_hab_creee,
            superficie_terrain,
            zone_op
        FROM {SCHEMA}.sitadel_pd
        WHERE num_autorisation IN :nums
        ORDER BY num_autorisation
    """,
}


class SitadelIndisponible(RuntimeError):
    """Tables Sitadel absentes ou illisibles."""


def normaliser_idus(idus: list[str]) -> list[str]:
    vus: set[str] = set()
    out: list[str] = []
    for raw in idus:
        idu = "".join(str(raw or "").split()).upper()
        if not idu or idu in vus:
            continue
        if not idu.isalnum() or not (10 <= len(idu) <= 20):
            continue
        vus.add(idu)
        out.append(idu)
    return out


def _dep(idu: str) -> str | None:
    dep = idu[:2]
    if dep.isdigit():
        return dep
    return None


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _txt(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _date(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    return s[:10] if s else None


def _num(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:
            return None
        return int(value) if value.is_integer() else value
    return None


def _vide(idu: str) -> dict:
    dep = _dep(idu)
    couvert = dep in DEPS_COUVERTS
    return {
        "idu": idu,
        "code_dep": dep,
        "couverture": "nouvelle_aquitaine" if couvert else "hors_couverture",
        "dans_cadastre_actuel": False,
        "definition_voisin": "st_touches_cadastre_actuel",
        "concerne_parcelle": False,
        "concerne_voisin": False,
        "parcelles_voisines": [],
        "nb_sur_parcelle": 0,
        "nb_sur_voisins": 0,
        "autorisations": [],
        "avertissements": [],
    }


def _exec(conn, sql: str, params: dict, *, expand: str | None = None) -> list[dict]:
    try:
        stmt = text(sql)
        if expand:
            stmt = stmt.bindparams(bindparam(expand, expanding=True))
        return list(conn.execute(stmt, params).mappings().all())
    except Exception as exc:
        message = str(exc).lower()
        if "sitadel_" in message and ("does not exist" in message or "n'existe pas" in message):
            raise SitadelIndisponible(
                "Tables Sitadel introuvables dans ecocompensation "
                "(sitadel_parcelle / sitadel_autorisation)."
            ) from exc
        raise


def _sql_direct() -> str:
    return f"""
    SELECT DISTINCT
        p.idu AS idu_cible,
        CAST('parcelle' AS text) AS relation,
        p.idu AS idu_porteur,
        p.dataset,
        p.num_autorisation,
        p.code_insee,
        p.code_dep
    FROM {SCHEMA}.sitadel_parcelle p
    WHERE p.idu IN :idus
    """


def _sql_voisins() -> str:
    """Dossiers des parcelles dont le contour touche la cible, même département."""
    return f"""
    SELECT DISTINCT
        c.idu AS idu_cible,
        CAST('voisin' AS text) AS relation,
        v.idu AS idu_porteur,
        p.dataset,
        p.num_autorisation,
        p.code_insee,
        p.code_dep
    FROM {SCHEMA}.cadastre_parcelles c
    JOIN {SCHEMA}.cadastre_parcelles v
      ON v.code_dep = c.code_dep
     AND v.idu <> c.idu
     AND c.geom IS NOT NULL
     AND v.geom IS NOT NULL
     AND v.geom && c.geom
     AND ST_Touches(c.geom, v.geom)
    JOIN {SCHEMA}.sitadel_parcelle p
      ON p.code_dep = v.code_dep
     AND p.idu = v.idu
    WHERE c.code_dep = :dep
      AND c.idu IN :idus
    """


_SQL_AUTORISATION = f"""
SELECT
    num_autorisation,
    type_dau,
    geoloc_statut,
    motif
FROM {SCHEMA}.sitadel_autorisation
WHERE dataset = :dataset
  AND num_autorisation IN :nums
"""


def _sql_cadastre() -> str:
    return f"""
    SELECT idu
    FROM {SCHEMA}.cadastre_parcelles
    WHERE code_dep = :dep
      AND idu IN :idus
    """


def _table_verrouillee(exc: Exception) -> bool:
    message = str(exc).lower()
    return "lock timeout" in message or "locknotavailable" in message or "verrouillage" in message


def _completer_autorisations(conn, rows: list[dict]) -> tuple[list[dict], bool]:
    """
    Statut de géolocalisation.

    La table peut être verrouillée le temps du calcul des points (ST_PointOnSurface).
    Dans ce cas on garde le lien parcelle et les dates, sans geoloc_statut.
    """
    par_jeu: dict[str, list[str]] = {}
    for row in rows:
        dataset = _txt(row.get("dataset"))
        numero = _txt(row.get("num_autorisation"))
        if dataset and numero:
            par_jeu.setdefault(dataset, [])
            if numero not in par_jeu[dataset]:
                par_jeu[dataset].append(numero)

    details: dict[tuple[str, str], dict] = {}
    for dataset, nums in par_jeu.items():
        for chunk in _chunks(nums, _CHUNK_SOURCES):
            try:
                conn.execute(text("SAVEPOINT sitadel_auth"))
                conn.execute(text("SET LOCAL lock_timeout = '4s'"))
                lus = _exec(
                    conn,
                    _SQL_AUTORISATION,
                    {"dataset": dataset, "nums": chunk},
                    expand="nums",
                )
                conn.execute(text("SET LOCAL lock_timeout = '0'"))
                conn.execute(text("RELEASE SAVEPOINT sitadel_auth"))
            except Exception as exc:
                conn.execute(text("ROLLBACK TO SAVEPOINT sitadel_auth"))
                if _table_verrouillee(exc):
                    log.warning("sitadel_autorisation verrouillée, géoloc omise")
                    return rows, False
                raise
            for src in lus:
                numero = _txt(src.get("num_autorisation"))
                if numero:
                    details[(dataset, numero)] = src

    out: list[dict] = []
    for row in rows:
        merged = dict(row)
        src = details.get((_txt(row.get("dataset")) or "", _txt(row.get("num_autorisation")) or ""))
        if src:
            for key, value in src.items():
                if key != "num_autorisation":
                    merged[key] = value
        out.append(merged)
    return out, True


def _completer_sources(conn, rows: list[dict]) -> list[dict]:
    """Ajoute état et dates depuis le jeu Sitadel concerné, par paquets de clés."""
    par_jeu: dict[str, list[str]] = {}
    for row in rows:
        dataset = _txt(row.get("dataset"))
        numero = _txt(row.get("num_autorisation"))
        if dataset in _SOURCES and numero:
            par_jeu.setdefault(dataset, [])
            if numero not in par_jeu[dataset]:
                par_jeu[dataset].append(numero)

    details: dict[tuple[str, str], dict] = {}
    for dataset, nums in par_jeu.items():
        for chunk in _chunks(nums, _CHUNK_SOURCES):
            for src in _exec(conn, _SOURCES[dataset], {"nums": chunk}, expand="nums"):
                numero = _txt(src.get("num_autorisation"))
                if numero:
                    details[(dataset, numero)] = src

    out: list[dict] = []
    for row in rows:
        merged = dict(row)
        src = details.get((_txt(row.get("dataset")) or "", _txt(row.get("num_autorisation")) or ""))
        if src:
            for key, value in src.items():
                if key != "num_autorisation":
                    merged[key] = value
        out.append(merged)
    return out


def _fusionner(payloads: dict[str, dict], rows: list[dict]) -> None:
    groupes: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        idu = _txt(row.get("idu_cible"))
        dataset = _txt(row.get("dataset"))
        numero = _txt(row.get("num_autorisation"))
        if not idu or not dataset or not numero or idu not in payloads:
            continue
        key = (idu, dataset, numero)
        slot = groupes.get(key)
        if slot is None:
            slot = {
                "relations": set(),
                "porteurs": set(),
                "row": row,
            }
            groupes[key] = slot
        relation = _txt(row.get("relation")) or "parcelle"
        slot["relations"].add(relation)
        porteur = _txt(row.get("idu_porteur"))
        if porteur:
            slot["porteurs"].add(porteur)

    for (idu, dataset, numero), slot in groupes.items():
        row = slot["row"]
        lien = "parcelle" if "parcelle" in slot["relations"] else "voisin"
        deja = next(
            (
                a for a in payloads[idu]["autorisations"]
                if a.get("dataset") == dataset and a.get("num_autorisation") == numero
            ),
            None,
        )
        if deja is not None:
            if lien == "parcelle":
                deja["lien"] = "parcelle"
            porteurs = set(deja.get("idus_porteurs") or [])
            porteurs.update(slot["porteurs"])
            deja["idus_porteurs"] = sorted(porteurs)
            continue
        payloads[idu]["autorisations"].append(
            {
                "lien": lien,
                "idus_porteurs": sorted(slot["porteurs"]),
                "dataset": dataset,
                "num_autorisation": numero,
                "type_dau": _txt(row.get("type_dau")),
                "etat": _txt(row.get("etat")),
                "etat_libelle": etat_libelle(_txt(row.get("etat"))),
                "date_depot": _date(row.get("date_depot")),
                "annee_depot": _num(row.get("an_depot")),
                "date_autorisation": _date(row.get("date_autorisation")),
                "date_doc": _date(row.get("date_doc")),
                "date_daact": _date(row.get("date_daact")),
                "denomination_demandeur": _txt(row.get("denom_dem")),
                "siren_demandeur": _txt(row.get("siren_dem")),
                "nature_projet": _txt(row.get("nature_projet")),
                "destination_principale": _txt(row.get("destination_principale")),
                "nb_logements_crees": _num(row.get("nb_lgt_tot_crees")),
                "surf_hab_creee_m2": _num(row.get("surf_hab_creee")),
                "superficie_terrain_m2": _num(row.get("superficie_terrain")),
                "zone_op": _txt(row.get("zone_op")),
                "geoloc_statut": _txt(row.get("geoloc_statut")),
                "motif": _txt(row.get("motif")),
                "code_insee": _txt(row.get("code_insee")),
                "code_dep": _txt(row.get("code_dep")),
            }
        )


def _finaliser(payload: dict) -> None:
    auths = payload["autorisations"]
    auths.sort(
        key=lambda a: a.get("date_autorisation") or a.get("date_depot") or "0000-00-00",
        reverse=True,
    )
    auths.sort(key=lambda a: 0 if a.get("lien") == "parcelle" else 1)
    payload["nb_sur_parcelle"] = sum(1 for a in auths if a.get("lien") == "parcelle")
    payload["nb_sur_voisins"] = sum(1 for a in auths if a.get("lien") == "voisin")
    voisines: set[str] = set()
    for auth in auths:
        for porteur in auth.get("idus_porteurs") or []:
            if porteur and porteur != payload["idu"]:
                voisines.add(porteur)
    payload["parcelles_voisines"] = sorted(voisines)
    payload["concerne_parcelle"] = payload["nb_sur_parcelle"] > 0
    payload["concerne_voisin"] = payload["nb_sur_voisins"] > 0 or bool(voisines)


def rechercher(conn, idus: list[str], *, avec_voisins: bool = True) -> dict[str, dict]:
    """
    Pour chaque IDU, les dossiers Sitadel de la parcelle et, si demandé,
    ceux des parcelles cadastrales qui la touchent.
    """
    propres = normaliser_idus(idus)
    payloads = {idu: _vide(idu) for idu in propres}
    if not payloads:
        return {}

    par_dep: dict[str, list[str]] = {}
    hors: list[str] = []
    for idu in propres:
        dep = _dep(idu)
        if dep in DEPS_COUVERTS:
            par_dep.setdefault(dep, []).append(idu)
        else:
            hors.append(idu)

    geoloc_ok = True

    def _integrer(rows: list[dict]) -> None:
        nonlocal geoloc_ok
        if not rows:
            return
        if geoloc_ok:
            rows, ok = _completer_autorisations(conn, rows)
            geoloc_ok = ok
        _fusionner(payloads, _completer_sources(conn, rows))

    if avec_voisins:
        for dep, groupe in par_dep.items():
            for chunk in _chunks(groupe, _CHUNK_SPATIAL):
                log.info("Sitadel | dep=%s idus=%d avec_voisins", dep, len(chunk))
                _integrer(_exec(conn, _sql_direct(), {"idus": chunk}, expand="idus"))
                _integrer(_exec(conn, _sql_voisins(), {"dep": dep, "idus": chunk}, expand="idus"))
                for row in _exec(conn, _sql_cadastre(), {"dep": dep, "idus": chunk}, expand="idus"):
                    idu = _txt(row.get("idu"))
                    if idu in payloads:
                        payloads[idu]["dans_cadastre_actuel"] = True
        for chunk in _chunks(hors, _CHUNK_DIRECT):
            _integrer(_exec(conn, _sql_direct(), {"idus": chunk}, expand="idus"))
    else:
        for chunk in _chunks(propres, _CHUNK_DIRECT):
            _integrer(_exec(conn, _sql_direct(), {"idus": chunk}, expand="idus"))
        for dep, groupe in par_dep.items():
            for chunk in _chunks(groupe, _CHUNK_DIRECT):
                for row in _exec(conn, _sql_cadastre(), {"dep": dep, "idus": chunk}, expand="idus"):
                    idu = _txt(row.get("idu"))
                    if idu in payloads:
                        payloads[idu]["dans_cadastre_actuel"] = True

    if not geoloc_ok:
        message = (
            "Géolocalisation Sitadel non lue : la table des autorisations "
            "est en cours de mise à jour."
        )
        for payload in payloads.values():
            if payload["autorisations"] and message not in payload["avertissements"]:
                payload["avertissements"].append(message)
    for payload in payloads.values():
        _finaliser(payload)
    return payloads

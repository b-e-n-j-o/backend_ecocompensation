"""
manuel.py — UF dessinées à la main par l'écologue (remplace enrichir/pool/ajout_uf.py).

Mêmes signatures qu'avant (add_manual_uf, delete_uf, AjoutUfError) : seules les routes
changent d'import. Différence : l'UF manuelle passe par la MÊME évaluation que les
terrains PM -> l'écologue voit tout de suite si son terrain passe le filtre, et pourquoi.
last_results_uf est toujours régénéré depuis les tables (plus de chirurgie du JSON).
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from . import build, enrich, pool, tables
from .evaluation import components
from .params import load_project_config, params_from_config

P, T = tables.P, tables.T
LABEL = "Assemblée à la main"


class AjoutUfError(ValueError):
    pass


def _clean_idus(idus: list[str]) -> list[str]:
    clean = list(dict.fromkeys(str(i).strip() for i in idus if str(i).strip()))
    if len(clean) < 2:
        raise AjoutUfError("Sélectionnez au moins deux parcelles.")
    return clean


def _assert_one_block(conn, clean: list[str]) -> None:
    found = set(conn.execute(text("""
        SELECT idu FROM ecocompensation.parcelles
        WHERE idu = ANY(CAST(:idus AS text[])) AND geom_2154 IS NOT NULL"""),
        {"idus": clean}).scalars())
    missing = [i for i in clean if i not in found]
    if missing:
        raise AjoutUfError(f"Parcelles absentes du cadastre : {', '.join(missing[:8])}")

    pairs = conn.execute(text(f"""
        SELECT a.idu, b.idu FROM ecocompensation.parcelles a
        JOIN ecocompensation.parcelles b ON a.idu < b.idu AND a.geom_2154 && b.geom_2154
         AND {tables.CONTACT_SQL}
        WHERE a.idu = ANY(CAST(:idus AS text[])) AND b.idu = ANY(CAST(:idus AS text[]))"""),
        {"idus": clean}).all()
    index = {idu: i for i, idu in enumerate(clean)}
    groups = components(list(range(len(clean))), [(index[a], index[b]) for a, b in pairs])
    if len(groups) != 1:
        raise AjoutUfError(
            f"Ces parcelles forment {len(groups)} blocs séparés. "
            "Une unité foncière doit être d'un seul tenant."
        )


def _insert_parcelles(conn, project_id: str, uf_id: str, clean: list[str]) -> None:
    conn.execute(text(f"""
        INSERT INTO {P} (project_id, source, uf_id, idu, siren, denomination, surface_m2, geom_2154)
        SELECT DISTINCT ON (idu) CAST(:pid AS uuid), 'manuel', :uf_id, idu, 'manuel', :label,
               ST_Area(geom_2154), geom_2154
        FROM ecocompensation.parcelles
        WHERE idu = ANY(CAST(:idus AS text[])) AND geom_2154 IS NOT NULL
        ORDER BY idu"""),
        {"pid": project_id, "uf_id": uf_id, "idus": clean, "label": LABEL})


def _refresh(engine, project_id: str, params) -> dict:
    result = pool.build_response(engine, project_id, params,
                                 diagnostic=pool.stored_diagnostic(engine, project_id))
    pool.save_response(engine, project_id, result)
    return result


def add_manual_uf(engine, project_id: str, idus: list[str]) -> dict:
    clean = _clean_idus(idus)
    tables.ensure_tables(engine)
    uf_id = f"manuel_{uuid.uuid4().hex[:8]}"

    with engine.begin() as conn:
        _assert_one_block(conn, clean)
        _insert_parcelles(conn, project_id, uf_id, clean)

    params = params_from_config(load_project_config(engine, project_id))
    build.evaluer_manuels(engine, project_id, params, uf_ids=[uf_id])
    enrich.run(engine, project_id, params, uf_ids=[uf_id])
    result = _refresh(engine, project_id, params)

    uf = next((u for u in result["unites_foncieres"] if u["uf_id"] == uf_id), {})
    return {"uf_id": uf_id, "nb_parcelles": len(clean), "idus": clean,
            "surface_ha": uf.get("surface_ha"), "verdict": uf.get("verdict"),
            "motif_rejet": uf.get("motif_rejet")}


def replace_manual_uf(engine, project_id: str, uf_id: str, idus: list[str]) -> dict:
    """Remplace les parcelles d'une UF assemblée à la main et relance l'évaluation."""
    uf_id = str(uf_id or "").strip()
    if not uf_id:
        raise AjoutUfError("Identifiant d'unité foncière manquant.")
    clean = _clean_idus(idus)
    tables.ensure_tables(engine)
    with engine.begin() as conn:
        source = conn.execute(text(f"""
            SELECT source FROM {T}
            WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf"""),
            {"pid": project_id, "uf": uf_id}).scalar()
        if source is None:
            raise AjoutUfError("Unité foncière introuvable.")
        if source != "manuel":
            raise AjoutUfError(
                "Seule une unité assemblée à la main peut être recomposée. "
                "Créez-en une nouvelle à partir de votre sélection."
            )
        _assert_one_block(conn, clean)
        conn.execute(text(f"""
            DELETE FROM {P}
            WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf AND source = 'manuel'"""),
            {"pid": project_id, "uf": uf_id})
        _insert_parcelles(conn, project_id, uf_id, clean)

    params = params_from_config(load_project_config(engine, project_id))
    build.evaluer_manuels(engine, project_id, params, uf_ids=[uf_id])
    enrich.run(engine, project_id, params, uf_ids=[uf_id])
    result = _refresh(engine, project_id, params)
    uf = next((u for u in result["unites_foncieres"] if u["uf_id"] == uf_id), {})
    return {"uf_id": uf_id, "nb_parcelles": len(clean), "idus": clean,
            "surface_ha": uf.get("surface_ha"), "verdict": uf.get("verdict"),
            "motif_rejet": uf.get("motif_rejet")}


def delete_uf(engine, project_id: str, uf_id: str) -> dict:
    uf_id = str(uf_id or "").strip()
    if not uf_id:
        raise AjoutUfError("Identifiant d'unité foncière manquant.")
    with engine.begin() as conn:
        n = conn.execute(text(f"DELETE FROM {T} WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf"),
                         {"pid": project_id, "uf": uf_id}).rowcount
        # parcelles d'une UF manuelle : supprimées ; d'une UF PM : détachées (relance = recalcul)
        conn.execute(text(f"DELETE FROM {P} WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf AND source = 'manuel'"),
                     {"pid": project_id, "uf": uf_id})
        conn.execute(text(f"UPDATE {P} SET uf_id = NULL WHERE project_id = CAST(:pid AS uuid) AND uf_id = :uf"),
                     {"pid": project_id, "uf": uf_id})
    if not n:
        raise AjoutUfError("Unité foncière introuvable.")
    _refresh(engine, project_id, params_from_config(load_project_config(engine, project_id)))
    return {"uf_id": uf_id, "deleted": True}

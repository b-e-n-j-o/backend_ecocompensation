"""
Cas réel Domaine des Pins (SIREN 351397096, commune 33122) — nécessite la base.

    UF_TEST_PROJECT_ID=<uuid> pytest .../unites_foncieres/tests/test_cas_reel.py -s

Ne traite que ce SIREN : n'efface que ses lignes et n'écrit pas last_results_uf.
"""
import os

import pytest
from sqlalchemy import text

PROJECT_ID = os.environ.get("UF_TEST_PROJECT_ID")
SIREN = "351397096"
ATTENDUS = ["331220000D2368", "331220000D2369", "331220000D2370", "331220000D2381", "331220000D2384"]

pytestmark = pytest.mark.skipif(not PROJECT_ID, reason="UF_TEST_PROJECT_ID non défini")


def test_domaine_des_pins_cinq_parcelles_dans_un_terrain_retenu():
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv())
    from db import get_engine

    from api.recherche_de_foncier.filtrage.layers.unites_foncieres import tables
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.params import (
        load_project_config, params_from_config)
    from api.recherche_de_foncier.filtrage.layers.unites_foncieres.runner import run_sync

    engine = get_engine()
    with engine.connect() as conn:
        aoi_id = conn.execute(text("SELECT aoi_id FROM ecocompensation.projects WHERE id = CAST(:p AS uuid)"),
                              {"p": PROJECT_ID}).scalar()
    params = params_from_config(load_project_config(engine, PROJECT_ID), siren=SIREN)
    run_sync(engine, PROJECT_ID, str(aoi_id), params, print, save=False)

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT uf_id, nb_parcelles, verdict, motif_rejet FROM {tables.T}
            WHERE project_id = CAST(:p AS uuid) AND idus @> CAST(:e AS text[])"""),
            {"p": PROJECT_ID, "e": ATTENDUS}).mappings().all()
    assert rows, "les 5 parcelles ne sont dans aucun terrain (voir eliminee_par dans uf_parcelles)"
    assert any(r["verdict"] for r in rows), f"terrain trouvé mais rejeté : {[dict(r) for r in rows]}"

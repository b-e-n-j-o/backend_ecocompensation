"""
Façade de la recherche Sitadel.

Même idée que ``durete_service`` : un appel par liste d'IDU, réutilisé
par l'endpoint seul et par l'enrichissement du pool. Le score de dureté
n'est pas recalculé ici.
"""

from __future__ import annotations

import logging

from .recherche import rechercher
from .signal_score import evaluer_signal, lecture

log = logging.getLogger("autorisations_urba")


def lookup_for_idus(
    conn,
    idus: list[str],
    *,
    avec_voisins: bool = True,
) -> dict[str, dict]:
    """
    Retourne ``{ idu: payload }``.

    ``signal_score.applique_au_score`` est toujours False : les points
    suggérés sont une hypothèse, pas une entrée du score.
    """
    payloads = rechercher(conn, idus, avec_voisins=avec_voisins)
    for payload in payloads.values():
        signal = evaluer_signal(payload)
        payload["signal_score"] = signal
        payload["lecture"] = lecture(payload, signal)
    n_parcelle = sum(1 for p in payloads.values() if p.get("concerne_parcelle"))
    n_voisin = sum(1 for p in payloads.values() if p.get("concerne_voisin"))
    log.info(
        "Sitadel lookup | idus=%d avec_permis_parcelle=%d avec_permis_voisin=%d",
        len(payloads),
        n_parcelle,
        n_voisin,
    )
    return payloads


if __name__ == "__main__":
    import argparse
    import json

    from db import get_engine

    parser = argparse.ArgumentParser(description="Autorisations Sitadel pour une liste d'IDU")
    parser.add_argument("--idus", nargs="+", required=True)
    parser.add_argument("--sans-voisins", action="store_true")
    args = parser.parse_args()
    engine = get_engine()
    with engine.begin() as conn:
        result = lookup_for_idus(conn, args.idus, avec_voisins=not args.sans_voisins)
    print(json.dumps(result, ensure_ascii=False, indent=2))

"""
Piste de surcharge pour le score de dureté — non appliquée.

Le permis d'urbanisme ne dit pas la même chose que les axes actuels :
les axes 1 à 3 décrivent la personne morale, l'axe 4 l'ancienneté
d'acquisition (DVF), la surcharge bail le RPG. Un permis dit si le terrain
est déjà engagé dans un projet de construction.

Insertion prévue, le jour où les points sont validés : au même endroit que
le bonus arrachage de vigne, dans ``DureteFonciereProfiler``, après le score
SIREN. C'est une surcharge par IDU (un permis est local), plafonnée à 100,
et non un argument du prompt LLM.

Règles proposées (``delta_suggere``, jamais ajouté au ``score_final``) :

- PC ou PA sur la parcelle, état autorisé / commencé / terminé, géoloc
  complète ou partielle, date de moins de 10 ans : +15.
  Le terrain est en cours d'urbanisation, peu disponible pour une compensation.
- Même permis, plus ancien : +5. L'artificialisation est probable, le foncier
  a pu se stabiliser.
- DP seule : +0. Trop faible (clôture, abri, annexe).
- PD seule : +0, drapeau démolition. Une démolition peut libérer le terrain
  ou précéder une reconstruction : on ne baisse pas le score tout seul.
- Permis seulement sur une voisine : +0, drapeau de contexte.
- Géoloc ambiguë, permis annulé, hors couverture : +0.
"""

from __future__ import annotations

from datetime import date, datetime

DEPS_COUVERTS = ("16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87")

ETATS_ACTIFS = {"2": "autorisé", "5": "commencé", "6": "terminé"}
ETATS = {"2": "autorisé", "4": "annulé", "5": "commencé", "6": "terminé"}

DELTA_PERMIS_RECENT = 15
DELTA_PERMIS_ANCIEN = 5
ANNEES_RECENT = 10
GEOLOC_SCORABLE = {"complet", "partiel"}
TYPES_CONSTRUCTIFS = {"PC", "PA"}


def etat_libelle(etat: str | None) -> str | None:
    if not etat:
        return None
    return ETATS.get(str(etat).strip(), str(etat).strip())


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_permis(auth: dict) -> date | None:
    for key in ("date_autorisation", "date_depot"):
        parsed = _as_date(auth.get(key))
        if parsed:
            return parsed
    annee = auth.get("annee_depot")
    if isinstance(annee, int) and 1900 <= annee <= 2100:
        return date(annee, 1, 1)
    return None


def _est_recent(auth: dict, aujourd_hui: date | None = None) -> bool:
    quand = _date_permis(auth)
    if quand is None:
        return False
    ref = aujourd_hui or date.today()
    return (ref - quand).days <= ANNEES_RECENT * 366


def _actif_scorable(auth: dict) -> bool:
    etat = str(auth.get("etat") or "").strip()
    geoloc = str(auth.get("geoloc_statut") or "").strip()
    return etat in ETATS_ACTIFS and geoloc in GEOLOC_SCORABLE


def evaluer_signal(payload: dict, aujourd_hui: date | None = None) -> dict:
    """Retourne une hypothèse de points. ``applique_au_score`` reste False."""
    base = {
        "applique_au_score": False,
        "delta_suggere": 0,
        "regle": "aucune",
        "num_autorisation": None,
        "commentaire": "Aucune autorisation exploitable pour une surcharge.",
    }
    if payload.get("couverture") != "nouvelle_aquitaine":
        base["regle"] = "hors_couverture"
        base["commentaire"] = (
            "Département hors du croisement Sitadel × cadastre actuel "
            "(Nouvelle-Aquitaine). Pas de surcharge."
        )
        return base

    sur_parcelle = [a for a in payload.get("autorisations") or [] if a.get("lien") == "parcelle"]
    sur_voisins = [a for a in payload.get("autorisations") or [] if a.get("lien") == "voisin"]

    constructifs = [
        a for a in sur_parcelle
        if _actif_scorable(a) and str(a.get("type_dau") or "").upper() in TYPES_CONSTRUCTIFS
    ]
    if constructifs:
        recent = next((a for a in constructifs if _est_recent(a, aujourd_hui)), None)
        choisi = recent or constructifs[0]
        recent_ok = recent is not None
        base["delta_suggere"] = DELTA_PERMIS_RECENT if recent_ok else DELTA_PERMIS_ANCIEN
        base["regle"] = "permis_constructif_recent" if recent_ok else "permis_constructif_ancien"
        base["num_autorisation"] = choisi.get("num_autorisation")
        age = "moins de 10 ans" if recent_ok else "plus de 10 ans, ou date absente"
        base["commentaire"] = (
            f"{choisi.get('type_dau') or 'Permis'} {choisi.get('num_autorisation')} "
            f"sur la parcelle ({etat_libelle(choisi.get('etat')) or 'état inconnu'}, {age}). "
            f"Surcharge suggérée +{base['delta_suggere']}, non appliquée au score."
        )
        return base

    if any(_actif_scorable(a) and str(a.get("type_dau") or "").upper() == "DP" for a in sur_parcelle):
        base["regle"] = "declaration_prealable_seule"
        base["commentaire"] = (
            "Seule une déclaration préalable active est rattachée à la parcelle. "
            "Signal trop faible pour une surcharge."
        )
        return base

    if any(_actif_scorable(a) and str(a.get("type_dau") or "").upper() == "PD" for a in sur_parcelle):
        base["regle"] = "demolition_seule"
        base["commentaire"] = (
            "Permis de démolir actif sur la parcelle, sans permis de construire "
            "ou d'aménager scorable. Drapeau seulement : on ne baisse pas le score."
        )
        return base

    if sur_parcelle and any(not a.get("geoloc_statut") for a in sur_parcelle):
        base["regle"] = "geoloc_absente"
        base["commentaire"] = (
            "Autorisation sur la parcelle, sans statut de géolocalisation. "
            "Pas de surcharge tant que ce statut n'est pas lu."
        )
        return base

    if sur_parcelle and all(str(a.get("geoloc_statut") or "") == "ambigu" for a in sur_parcelle):
        base["regle"] = "ambigu"
        base["commentaire"] = (
            "Les autorisations trouvées ont une géolocalisation ambiguë "
            "(plusieurs IDU pour la même section). Pas de surcharge."
        )
        return base

    if sur_parcelle and all(str(a.get("etat") or "").strip() == "4" for a in sur_parcelle):
        base["regle"] = "annule"
        base["commentaire"] = "Autorisations sur la parcelle toutes annulées. Pas de surcharge."
        return base

    if sur_voisins and not sur_parcelle:
        base["regle"] = "voisin_seulement"
        base["commentaire"] = (
            "Autorisation seulement sur une parcelle voisine (ST_Touches). "
            "Contexte urbain, pas une surcharge de la parcelle cible."
        )
        return base

    if sur_parcelle:
        base["commentaire"] = (
            "Autorisation présente sur la parcelle, sans règle de surcharge "
            "(état ou type non retenu)."
        )
    return base


def lecture(payload: dict, signal: dict) -> str:
    """Phrase courte pour l'enrichissement du pool."""
    if payload.get("couverture") != "nouvelle_aquitaine":
        return "Hors couverture Sitadel (cadastre actuel, Nouvelle-Aquitaine uniquement)."
    auths = payload.get("autorisations") or []
    if not auths:
        return "Aucune autorisation d'urbanisme sur la parcelle ni sur une parcelle voisine."
    n_p = int(payload.get("nb_sur_parcelle") or 0)
    n_v = int(payload.get("nb_sur_voisins") or 0)
    morceaux = []
    if n_p:
        morceaux.append(f"{n_p} sur la parcelle")
    if n_v:
        morceaux.append(f"{n_v} sur une parcelle voisine")
    elif payload.get("parcelles_voisines") and n_p:
        morceaux.append("dossier aussi porté par une parcelle qui touche")
    tete = signal.get("num_autorisation")
    if not tete and auths:
        tete = auths[0].get("num_autorisation")
    detail = f" ({tete})" if tete else ""
    return "Autorisations d'urbanisme" + detail + " : " + ", ".join(morceaux) + "."

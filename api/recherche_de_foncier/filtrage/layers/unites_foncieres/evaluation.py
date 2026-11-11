"""
evaluation.py — logique pure (sans base) : terrains et verdict.

Un terrain = parcelles d'une même personne morale qui se touchent, après retrait des
parcelles éliminées. Son verdict se calcule à partir des booléens par parcelle :

  eliminatoire  toutes les parcelles doivent passer (exclusions, hors emprise…)
                -> côté PM la parcelle fautive est retirée AVANT le regroupement ;
                   côté UF manuelle on garde la parcelle et le terrain échoue.
  existence     au moins une parcelle passe (intersection / distance max d'une couche :
                l'union intersecte X  <=>  une de ses parcelles intersecte X)
  faune / flore une colonne par espèce / taxon, « au moins une parcelle » par espèce,
                puis la combinaison ET/OU du filtre (fauna_joins, bio_combine…)
  zh_surface    somme des m² de zone humide des parcelles >= seuil
  surface       somme des surfaces >= surface minimale
  miller        calculé sur l'union ; bloquant seulement si miller_sur_terrain
"""
from __future__ import annotations

from dataclasses import dataclass, field

ELIMINATOIRE = "eliminatoire"
EXISTENCE = "existence"
FAUNE = "faune"
FLORE = "flore"
ZH_SURFACE = "zh_surface"

# Critères dont les parcelles « porteuses » sont listées dans le détail du terrain
KINDS_PORTEURS = (EXISTENCE, FAUNE, FLORE, ZH_SURFACE)


@dataclass(frozen=True)
class Critere:
    key: str
    label: str
    kind: str
    sql: str = ""            # expression SQL (alias p) — booléenne, ou m² pour zh_surface


@dataclass
class ParcelleUF:
    id: int
    idu: str
    siren: str | None
    surface_m2: float
    criteres: dict = field(default_factory=dict)   # {clé critère: bool}
    zh_m2: float = 0.0
    wkt: str | None = None
    denomination: str | None = None
    forme_juridique: str | None = None


@dataclass
class Regles:
    min_area_m2: float
    fauna_combine: str = "or"
    fauna_joins: list[str] = field(default_factory=list)
    flora_combine: str = "or"
    flora_joins: list[str] = field(default_factory=list)
    bio_combine: str = "or"
    min_zh_m2: float = 0.0
    miller_thresh: float | None = None
    miller_sur_terrain: bool = False


@dataclass
class Verdict:
    ok: bool
    motif: str | None                 # 1er critère en échec (diagnostic)
    detail: dict                      # {clé: {label, type, ok, idus, ...}}


# --------------------------------------------------------------------------- graphe
def components(ids: list[int], pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Composantes connexes (union-find). Ordre déterministe : taille décroissante."""
    members = set(ids)
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in members and b in members:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups: dict[int, list[int]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    comps = [sorted(g) for g in groups.values()]
    comps.sort(key=lambda g: (-len(g), g[0]))
    return comps


def chain_bool(values: list[bool], joins: list[str] | None, fallback: str = "or") -> bool:
    """Équivalent Python de faune.chain_sql : (A op1 B) op2 C, de gauche à droite."""
    if not values:
        return True
    acc = bool(values[0])
    for i, v in enumerate(values[1:]):
        raw = joins[i] if joins and i < len(joins) else fallback
        acc = (acc and bool(v)) if str(raw).lower() == "and" else (acc or bool(v))
    return acc


# --------------------------------------------------------------------------- verdict
def evaluer_terrain(
    parcelles: list[ParcelleUF],
    criteres: list[Critere],
    regles: Regles,
    *,
    miller: float | None = None,
) -> Verdict:
    detail: dict[str, dict] = {}
    echecs: list[str] = []

    surface = sum(p.surface_m2 for p in parcelles)
    detail["surface"] = {
        "label": f"Surface ≥ {regles.min_area_m2 / 1e4:g} ha",
        "type": "surface",
        "ok": surface >= regles.min_area_m2,
        "valeur_ha": round(surface / 1e4, 4),
    }
    if not detail["surface"]["ok"]:
        echecs.append("surface")

    def flag(p: ParcelleUF, key: str) -> bool:
        return bool(p.criteres.get(key, False))

    for c in criteres:
        if c.kind == ELIMINATOIRE:
            fautives = [p.idu for p in parcelles if not flag(p, c.key)]
            detail[c.key] = {"label": c.label, "type": c.kind, "ok": not fautives, "idus": fautives}
        elif c.kind in (EXISTENCE, FAUNE, FLORE):
            porteuses = [p.idu for p in parcelles if flag(p, c.key)]
            detail[c.key] = {"label": c.label, "type": c.kind, "ok": bool(porteuses), "idus": porteuses}
        elif c.kind == ZH_SURFACE:
            tot = sum(p.zh_m2 for p in parcelles)
            detail[c.key] = {
                "label": c.label, "type": c.kind, "ok": tot >= regles.min_zh_m2,
                "valeur_ha": round(tot / 1e4, 4),
                "idus": [p.idu for p in parcelles if p.zh_m2 > 0],
            }
        if c.kind in (ELIMINATOIRE, EXISTENCE, ZH_SURFACE) and not detail[c.key]["ok"]:
            echecs.append(c.key)

    faune = [detail[c.key]["ok"] for c in criteres if c.kind == FAUNE]
    flore = [detail[c.key]["ok"] for c in criteres if c.kind == FLORE]
    if faune or flore:
        f_ok = chain_bool(faune, regles.fauna_joins, regles.fauna_combine) if faune else None
        fl_ok = chain_bool(flore, regles.flora_joins, regles.flora_combine) if flore else None
        if f_ok is not None and fl_ok is not None:
            bio_ok = (f_ok and fl_ok) if str(regles.bio_combine).lower() == "and" else (f_ok or fl_ok)
        else:
            bio_ok = f_ok if f_ok is not None else fl_ok
        detail["biodiversite"] = {
            "label": "Faune / flore (combinaison du filtre)", "type": "combinaison",
            "ok": bool(bio_ok), "faune_ok": f_ok, "flore_ok": fl_ok,
        }
        if not bio_ok:
            echecs.append("biodiversite")

    if miller is not None:
        seuil = regles.miller_thresh
        ok = seuil is None or miller >= seuil
        detail["miller"] = {
            "label": "Compacité (Miller) du terrain", "type": "forme", "ok": ok,
            "valeur": round(miller, 3), "seuil": seuil, "bloquant": regles.miller_sur_terrain,
        }
        if regles.miller_sur_terrain and not ok:
            echecs.append("miller")

    return Verdict(ok=not echecs, motif=echecs[0] if echecs else None, detail=detail)


def criteres_portes(idu: str, detail: dict) -> list[str]:
    """Clés des critères positifs que porte cette parcelle (affichage « pourquoi »)."""
    return [k for k, d in detail.items()
            if isinstance(d, dict) and d.get("type") in KINDS_PORTEURS and idu in (d.get("idus") or [])]

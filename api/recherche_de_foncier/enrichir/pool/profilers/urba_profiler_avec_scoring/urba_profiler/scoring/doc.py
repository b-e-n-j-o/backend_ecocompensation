"""
doc.py — Génère la documentation des règles à partir du YAML (source unique).

    python -m urba_profiler.scoring.doc [--out-dir docs]

Produit :
- REGLES_SCORING.md         : référentiel complet lisible (tables par section)
                              + scénarios de calibration (scores obtenus)
- revue_regles_scoring.xlsx : grille de relecture pour l'urbaniste
                              (une ligne par règle + colonnes Avis / Proposition / Commentaire)
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .loader import SOUS_SCORES, RuleSet, get_rules

_ABBR = {"perennite": "pér.", "compatibilite": "compat.", "synergie": "syn.", "maitrise": "maît.", "confiance": "conf."}


def _fmt_eff(e: Dict[str, float]) -> str:
    if not e:
        return "neutre"
    return ", ".join(f"{_ABBR.get(k, k)} {v:+g}" for k, v in e.items())


def _fmt_profils(p: Dict[str, Dict[str, float]]) -> str:
    return "<br>".join(f"*{k}* : {_fmt_eff(v)}" for k, v in p.items()) if p else ""


def _fmt_veto(vs) -> str:
    out = []
    for v in vs:
        pf = f" [{', '.join(v.profils)}]" if v.profils else ""
        out.append(f"**{v.niveau}** ≥ {v.seuil_pct:g} %{pf}")
    return "<br>".join(out)


def _triggers(r) -> str:
    parts = []
    if r.codes:
        parts.append("codes " + ", ".join(r.codes))
    if r.suptypes:
        parts.append("SUP " + ", ".join(s.upper() for s in r.suptypes))
    if r.suptypes_prefixe:
        parts.append("SUP préfixe " + ", ".join(s.upper() + "*" for s in r.suptypes_prefixe))
    if r.raffine:
        parts.append("affine " + ", ".join(r.raffine) + " si : " + " / ".join(_pat(p) for p in r.mots_cles_raffinement))
    if r.mots_cles:
        parts.append("mots-clés : " + " / ".join(_pat(p) for p in r.mots_cles))
    return "<br>".join(parts)


def _pat(p) -> str:
    return "`" + p.pattern.replace("|", "\\|") + "`"


def _md_escape(s: Optional[str]) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Scénarios de calibration
# ---------------------------------------------------------------------------


def scenarios():
    from ..nomenclature import lookup
    from ..schemas import AnneauZonage, Cible, Couche, DocumentUrba, Element, Mesure, Synthese

    A = 10_000.0

    def z(lib, tz, pct, libelong=None):
        code, labc, fl = lookup("typezone", tz) if tz else (None, None, ["code_absent"])
        return Element(element_id=f"z:{lib}", source_ids=["z"], nb_entites=1,
                       attributs={"libelle": lib, "typezone": tz, "libelong": libelong}, code=code, libelle_cnig=labc,
                       flags=fl, mesure=Mesure(position="interieur", pct_cible=pct, surface_m2=pct * A / 100))

    def e(code, lib, pct, nom="psc"):
        t, st = code.split("-") if code else (None, None)
        c, labc, fl = lookup(nom, t, st)
        return Element(element_id=f"e:{code}:{lib}", source_ids=["e"], nb_entites=1, attributs={"libelle": lib},
                       code=c, libelle_cnig=labc, flags=fl,
                       mesure=Mesure(position="interieur", pct_cible=pct, surface_m2=pct * A / 100))

    def s(st, nom, pct=100):
        return Element(element_id=f"s:{st}", source_ids=["s"], nb_entites=1, attributs={"suptype": st, "nomsuplitt": nom},
                       mesure=Mesure(position="interieur", pct_cible=pct, surface_m2=pct * A / 100))

    def C(zones, psc=(), inf=(), sup=(), anneau=None):
        cs = [
            Couche(layer_key="zonage_plu", display_name="", famille="zonage", geom_kind="surf", typename="",
                   status="intersected" if zones else "not_intersected", elements=list(zones)),
            Couche(layer_key="prescriptions_surf", display_name="", famille="prescription", geom_kind="surf",
                   typename="", status="intersected", elements=list(psc)),
            Couche(layer_key="infos_surf", display_name="", famille="information", geom_kind="surf", typename="",
                   status="intersected", elements=list(inf)),
            Couche(layer_key="sup_assiette_s", display_name="", famille="servitude", geom_kind="surf", typename="",
                   status="intersected", elements=list(sup)),
        ]
        syn = Synthese(anneau_zonage=AnneauZonage(rayon_m=200, surface_m2=1, pct=anneau) if anneau else None)
        return Cible(id="x", type="parcelle", surface_m2=A, synthese=syn, couches=cs,
                     documents=[DocumentUrba(idurba="33000_PLU_20200101", typedoc="PLU", date="2020-01-01")])

    return [
        ("N 100 % + trame verte et bleue sur 40 %", C([z("N", "N", 100)], psc=[e("25-00", "TVB", 40)])),
        ("N 100 %, rien d'autre", C([z("N", "N", 100)])),
        ("N 100 % entourée d'urbanisation (U 60 % dans 200 m)", C([z("N", "N", 100)], anneau={"U": 60, "N": 40})),
        ("A 100 %, rien d'autre", C([z("A", "A", 100)])),
        ("A 100 % en PAEN", C([z("A", "A", 100)], inf=[e("25-00", "PAEN", 100, "inf")])),
        ("Nzh 100 % (secteur zone humide) + PPR (PM1)",
         C([z("Nzh", "N", 100, "Secteur naturel des zones humides")], sup=[s("pm1", "PPRI")])),
        ("N 100 % + EBC sur 60 %", C([z("N", "N", 100)], psc=[e("01-01", "Espace boisé classé", 60)])),
        ("N 100 % + réserve naturelle (AC3)", C([z("N", "N", 100)], sup=[s("ac3", "Réserve naturelle")])),
        ("A 100 % + ligne HT (I4) + canalisation gaz (I3)",
         C([z("A", "A", 100)], sup=[s("i4", "Lignes électriques"), s("i3", "Gaz")])),
        ("A 100 % + code 99 « zone humide à préserver » sur 50 %", C([z("A", "A", 100)], psc=[e("99-00", "Zone humide à préserver", 50)])),
        ("A 100 % + ER voirie sur 25 %", C([z("A", "A", 100)], psc=[e("05-01", "ER élargissement voie", 25)])),
        ("A 100 % + ER voirie sur 60 %", C([z("A", "A", 100)], psc=[e("05-01", "ER élargissement voie", 60)])),
        ("A 100 % + DPU", C([z("A", "A", 100)], inf=[e("04-00", "DPU", 100, "inf")])),
        ("Commune au RNU (aucun zonage GPU)", C([])),
        ("Npv 100 % (secteur photovoltaïque)", C([z("Npv", "N", 100)])),
        ("2AU 100 % (urbanisation future)", C([z("2AU", "AUs", 100)])),
        ("A 100 % + OAP habitat sur 40 %", C([z("A", "A", 100)], psc=[e("18-06", "OAP habitat", 40)])),
        ("1AU saisie par erreur en typezone N", C([z("1AUh", "N", 100)])),
        ("UB 100 %", C([z("UB", "U", 100)])),
    ]


def scenario_table(rs: RuleSet) -> str:
    from .engine import score_cible

    profils = list(rs.profils)
    head = "| Scénario | " + " | ".join(profils) + " | Confiance |\n|---|" + "---|" * (len(profils) + 1) + "\n"
    rows = []
    for name, c in scenarios():
        cells = []
        conf = None
        for p in profils:
            sc = score_cible(c, profil=p, rules=rs)
            v = " ⛔" if any(x.niveau == "bloquant" for x in sc.vetos) else (" ⚠" if sc.vetos else "")
            cells.append(f"{sc.score_global:.0f} {sc.classe}{v}")
            conf = sc.confiance
        rows.append(f"| {name} | " + " | ".join(cells) + f" | {conf.valeur:.0f} ({conf.niveau}) |")
    return head + "\n".join(rows) + "\n\n⛔ veto bloquant, ⚠ veto majeur.\n"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def build_markdown(rs: RuleSet) -> str:
    L: List[str] = []
    L.append(f"# Référentiel de scoring urbanistique — compensation écologique\n")
    L.append(f"Version des règles **{rs.version}** — statut : *{rs.statut}*. "
             f"Document généré le {date.today():%d/%m/%Y} depuis `regles_compensation.yaml` : ne pas éditer à la main.\n")
    L.append("Méthode et principes : voir `SCORING.md`. Abréviations : pér. = pérennité, compat. = compatibilité, "
             "syn. = synergie, maît. = maîtrise foncière, conf. = confiance. Les effets sont les points appliqués pour "
             "une couverture totale de la parcelle et une donnée fiable.\n")

    L.append("## 1. Paramètres généraux\n")
    L.append("| Sous-score | Poids | Base | Définition |\n|---|---|---|---|")
    for k in SOUS_SCORES:
        base = "zonage" if k == "perennite" else rs.base[k]
        L.append(f"| {k} | {rs.ponderation[k]:.2f} | {base} | {_md_escape(rs.sous_scores.get(k))} |")
    L.append("\n**Profils** : " + " ; ".join(f"`{k}` — {v}" for k, v in rs.profils.items()) + "\n")
    L.append("**Classes** : " + " ; ".join(f"{c['code']} ≥ {c['min']} ({c['libelle']})" for c in rs.classes) + "\n")
    L.append(f"**Vetos** : bloquant → score plafonné à {rs.plafonds_veto['bloquant']} ; majeur → {rs.plafonds_veto['majeur']}. "
             f"Un veto déclenché par une donnée de fiabilité basse n'est pas appliqué (rétrogradé en point à vérifier).\n")
    L.append("**Fiabilité** (multiplicateur des points) : " +
             ", ".join(f"{k} ×{v}" for k, v in rs.fiab_mult.items()) + "\n")

    L.append("## 2. Zonage — base de pérennité\n")
    L.append("| Type | Base | Justification |\n|---|---|---|")
    for t, v in rs.zonage_base.items():
        L.append(f"| {t} | {v['valeur']} | {_md_escape(v['justification'])} |")
    L.append("\n**Vetos de zonage**\n\n| Types | Seuil | Niveau | Motif |\n|---|---|---|---|")
    for v in rs.zonage_veto:
        L.append(f"| {', '.join(v['types'])} | ≥ {v['seuil_pct']} % | {v['niveau']} | {_md_escape(v['motif'])} |")
    L.append("\n## 3. Zonage — sous-secteurs\n")
    L.append("| ID | Secteur | Types | Suffixe (regex) | Mots-clés du libellé long | Effets | Profils | Veto | Justification | À confirmer |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in rs.secteurs:
        L.append(f"| `{s.id}` | {s.label} | {', '.join(s.types)} | {_pat(s.libelle_regex) if s.libelle_regex else ''} | "
                 f"{' / '.join(_pat(p) for p in s.mots_cles)} | {_fmt_eff(s.effets)} | {_fmt_profils(s.profils)} | "
                 f"{_fmt_veto(s.veto)} | {_md_escape(s.justification)} | "
                 f"{'❓ ' + _md_escape(s.question_expert or '') if s.a_confirmer else ''} |")

    titles = {"prescription": "4. Prescriptions", "information": "5. Informations", "servitude": "6. Servitudes d'utilité publique"}
    for fam, title in titles.items():
        L.append(f"\n## {title}\n")
        L.append("| ID | Règle | Déclencheurs | Effets | Profils | Veto | Justification | Réf. | À confirmer |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in rs.regles:
            if r.familles[0] != fam or r.id.startswith("kw_"):
                continue
            L.append(f"| `{r.id}` | {r.label} | {_triggers(r)} | {_fmt_eff(r.effets)} | {_fmt_profils(r.profils)} | "
                     f"{_fmt_veto(r.veto)} | {_md_escape(r.justification)} | {', '.join(r.references)} | "
                     f"{'❓ ' + _md_escape(r.question_expert or '') if r.a_confirmer else ''} |")

    L.append("\n## 7. Règles par mots-clés (libellés, codes 99 / absents / erronés)\n")
    L.append("| ID | Règle | Mots-clés | Effets | Profils | Veto | Justification |\n|---|---|---|---|---|---|---|")
    for r in rs.regles:
        if r.id.startswith("kw_"):
            L.append(f"| `{r.id}` | {r.label} | {' / '.join(_pat(p) for p in r.mots_cles)} | {_fmt_eff(r.effets)} | "
                     f"{_fmt_profils(r.profils)} | {_fmt_veto(r.veto)} | {_md_escape(r.justification)} |")

    L.append("\n## 8. Voisinage\n")
    for k, v in rs.voisinage.items():
        params = ", ".join(f"{a}={b}" for a, b in v.items() if a != "justification")
        L.append(f"- **{k}** ({params}) : {v.get('justification', '')}")

    L.append("\n## 9. Groupes sémantiques compatibles\n")
    L.append("Un code et un libellé de groupes compatibles ne sont pas en conflit : "
             + " ; ".join(" ↔ ".join(sorted(p)) for p in sorted(rs.groupes_compatibles, key=lambda x: sorted(x))) + "\n")

    L.append("\n## 10. Scénarios de calibration\n")
    L.append("Scores obtenus avec les règles actuelles sur des parcelles types (10 000 m², PLU de 2020). "
             "À relire en premier : si le classement relatif semble faux, ce sont les poids qu'il faut revoir.\n")
    L.append(scenario_table(rs))

    q = [r for r in rs.regles if r.a_confirmer] + [s for s in rs.secteurs if s.a_confirmer]
    L.append(f"\n## 11. Questions ouvertes pour l'expert ({len(q)})\n")
    for r in q:
        L.append(f"- `{r.id}` — {r.label}" + (f" : {r.question_expert}" if r.question_expert else " : interprétation à confirmer."))
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Grille de relecture Excel
# ---------------------------------------------------------------------------


def build_xlsx(rs: RuleSet, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Revue des règles"
    head = ["Section", "ID", "Règle", "Déclencheurs", "Effets (générique)", "Effets par profil", "Veto",
            "Point à vérifier", "Justification", "Références", "À confirmer", "Question à l'expert",
            "AVIS (Valider / Modifier / Supprimer)", "Proposition (effets, seuils...)", "Commentaire de l'expert"]
    ws.append(head)
    clean = lambda s: (s or "").replace("<br>", "\n").replace("`", "").replace("*", "").replace("\\|", "|")

    for t, v in rs.zonage_base.items():
        ws.append(["Zonage — base", f"zonage:{t}", t, f"typezone / libellé {t}", f"pér. base {v['valeur']}", "", "",
                   v.get("point_a_verifier", ""), v["justification"], "", "", "", "", "", ""])
    for v in rs.zonage_veto:
        ws.append(["Zonage — veto", "zonage_veto:" + "/".join(v["types"]), v["motif"], ", ".join(v["types"]), "", "",
                   f"{v['niveau']} ≥ {v['seuil_pct']} %", "", "", "", "", "", "", "", ""])
    for s in rs.secteurs:
        trig = f"suffixe {s.libelle_regex.pattern if s.libelle_regex else '-'} ; libellé long : " + " / ".join(p.pattern for p in s.mots_cles)
        ws.append(["Zonage — sous-secteur", s.id, s.label, trig, _fmt_eff(s.effets), clean(_fmt_profils(s.profils)),
                   clean(_fmt_veto(s.veto)), s.point_a_verifier or "", s.justification, "", "oui" if s.a_confirmer else "",
                   s.question_expert or "", "", "", ""])
    for r in rs.regles:
        sec = {"prescription": "Prescription", "information": "Information", "servitude": "Servitude"}[r.familles[0]]
        if r.id.startswith("kw_"):
            sec = "Mots-clés"
        ws.append([sec, r.id, r.label, clean(_triggers(r)), _fmt_eff(r.effets), clean(_fmt_profils(r.profils)),
                   clean(_fmt_veto(r.veto)), r.point_a_verifier or "", r.justification, ", ".join(r.references),
                   "oui" if r.a_confirmer else "", r.question_expert or "", "", "", ""])
    for k, v in rs.voisinage.items():
        ws.append(["Voisinage", k, k, ", ".join(f"{a}={b}" for a, b in v.items() if a != "justification"), "", "", "", "",
                   v.get("justification", ""), "", "", "", "", "", ""])

    widths = [16, 26, 34, 48, 22, 30, 24, 40, 60, 16, 10, 40, 18, 30, 40]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    hdr_fill = PatternFill("solid", fgColor="2D6A4F")
    rev_fill = PatternFill("solid", fgColor="FFF4CC")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hdr_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for c in row[12:15]:
            c.fill = rev_fill
    dv = DataValidation(type="list", formula1='"Valider,Modifier,Supprimer"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"M2:M{ws.max_row}")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet("Scénarios")
    from .engine import score_cible

    profils = list(rs.profils)
    ws2.append(["Scénario"] + profils + ["Confiance", "Classement jugé cohérent ? (oui/non)", "Commentaire"])
    for name, c in scenarios():
        vals, conf = [], None
        for p in profils:
            sc = score_cible(c, profil=p, rules=rs)
            vals.append(f"{sc.score_global:.0f} ({sc.classe})" + (" veto " + sc.vetos[0].niveau if sc.vetos else ""))
            conf = sc.confiance.valeur
        ws2.append([name] + vals + [conf, "", ""])
    ws2.column_dimensions["A"].width = 55
    for col in "BCDEFGH":
        ws2.column_dimensions[col].width = 20
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hdr_fill
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--rules")
    a = ap.parse_args()
    from .loader import load_rules

    rs = load_rules(a.rules) if a.rules else get_rules()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "REGLES_SCORING.md").write_text(build_markdown(rs), encoding="utf-8")
    try:
        build_xlsx(rs, out / "revue_regles_scoring.xlsx")
    except ImportError:
        print("openpyxl absent : grille Excel non générée")
    print(f"Documentation générée dans {out.resolve()}")


if __name__ == "__main__":
    main()

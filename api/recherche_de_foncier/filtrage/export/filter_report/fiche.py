"""
fiche.py — Fiche « Paramètres de filtrage » sur UNE page A4 : tous les critères du panneau
(dans l'ordre du panneau), leur état ON/OFF, leurs valeurs, et ce que chacun a retiré.
Réutilise builder.build_report (même format d'entrée que render.py).

CLI :
    python fiche.py --input examples/example_complet.json --out fiche.pdf [--html fiche.html] [-v]
Python :
    from fiche import render_fiche_pdf_bytes ; pdf = render_fiche_pdf_bytes(data)
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from builder import build_report, fmt_int, fmt_pct
from catalog import THEME_LABELS, THEME_ORDER

log = logging.getLogger("filter_report.fiche")
TEMPLATES = Path(__file__).parent / "templates"

# Paramètres qui alourdiraient la fiche sans aider à la relire : on les masque.
HIDDEN_PARAMS = {"Formule"}
MAX_CHIP_LEN = 60


def _chips(c: dict[str, Any]) -> list[str]:
    """Valeurs du critère : liste de (étiquette, valeur). Le nom du critère est affiché à part."""
    out: list[tuple[str, str]] = []
    for k, v in c["params"]:
        if k in HIDDEN_PARAMS or k == "Unités foncières":
            continue
        if k == "Milieux acceptés":  # une valeur par milieu
            out += [("", m.strip()) for m in v.split(",") if m.strip()]
        elif c["key"] in ("fauna_criteria", "flora_criteria") and k != "Combinaison":
            out.append((k, v.replace("observation à ", "")))   # espèce → « ≤ 600 m »
        elif k in ("Seuil", "Distance max.", "Rayon", "Types", "Mode"):
            out.append(("", v))
        else:
            out.append((k, v))
    return out


def build_fiche_context(data: dict[str, Any]) -> dict[str, Any]:
    ctx = build_report(data)
    blocks = []
    for theme in THEME_ORDER:
        items = [dict(c, chips=_chips(c)) for c in ctx["criteria"] if c["theme"] == theme]
        if not items:
            continue
        blocks.append({"title": THEME_LABELS[theme], "crits": items, "any_on": any(c["applied"] for c in items)})
    start, total = ctx["start_count"], ctx["total"]
    ctx.update(
        blocks=blocks,
        n_total=sum(1 for c in ctx["criteria"] if c["in_funnel"]),
        retention=fmt_pct(total / start * 100) if start else None,
        etude={k: (ctx.get("etude") or {}).get(k)
               for k in ("nom", "reference", "projet", "commune", "departement", "auteur", "organisme")},
    )
    log.info("Fiche : %d blocs, %d/%d critères actifs", len(blocks), ctx["n_applied"], ctx["n_total"])
    return ctx


def render_fiche_html(data: dict[str, Any]) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters["n"] = fmt_int
    env.filters["pct"] = fmt_pct
    return env.get_template("fiche_parametres.html.j2").render(**build_fiche_context(data))


def render_fiche_pdf_bytes(data: dict[str, Any]) -> bytes:
    from weasyprint import HTML

    t0 = time.perf_counter()
    doc = HTML(string=render_fiche_html(data), base_url=str(TEMPLATES)).render()
    if len(doc.pages) > 1:
        log.warning("La fiche déborde sur %d pages : réduire le contenu (beaucoup de taxons/milieux ?)", len(doc.pages))
    pdf = doc.write_pdf()
    log.info("PDF fiche généré en %.1f s (%d Ko, %d page(s))", time.perf_counter() - t0, len(pdf) // 1024, len(doc.pages))
    return pdf


def main() -> None:
    ap = argparse.ArgumentParser(description="Fiche une page des paramètres de filtrage.")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="fiche_parametres.pdf")
    ap.add_argument("--html")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", datefmt="%H:%M:%S")
    for noisy in ("fontTools", "weasyprint"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    data = json.loads(Path(a.input).read_text(encoding="utf-8"))
    if a.html:
        Path(a.html).write_text(render_fiche_html(data), encoding="utf-8")
        log.info("HTML écrit : %s", a.html)
    Path(a.out).write_bytes(render_fiche_pdf_bytes(data))
    log.info("PDF écrit : %s", a.out)


if __name__ == "__main__":
    main()

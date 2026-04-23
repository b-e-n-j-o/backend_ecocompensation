#!/usr/bin/env python3
"""
run_batch_test.py
=================
Lance le pipeline identité foncière sur un batch de parcelles/UF défini
dans un fichier JSON, et produit un dossier de rapports PDF.

Format JSON : liste de listes de parcelles.
Chaque sous-liste = une UF (une ou plusieurs parcelles contiguës).

Usage (depuis le dossier v0/) :
    python run_batch_test.py
    python run_batch_test.py --json DATA/premier_batch_de_test.json --out_dir ./BATCH_RESULTS
"""

import argparse
import json
import logging
import random
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

# ── Chemins par défaut (modifie si nécessaire) ────────────────────────────
DEFAULT_JSON    = "DATA/premier_batch_de_test.json"
DEFAULT_OUT_DIR = "BATCH_RESULTS"

# ── Le script est dans v0/ — les modules core/, pdf/, etc. sont au même niveau
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.parcelle import ParcelleRef, fetch_parcelles
from core.unites_foncieres import build_uf, parcelles_detail, uf_geojson, uf_surface_m2
from core.intersections import compute_intersections
from core.gpu_wfs import GPU_LAYERS_BY_TABLE, _fetch_layer
from utils.geo import gdf_bbox_4326, intersects_gdf
from visuels.carte_plu import render_plu_map
from visuels.carte_servitudes import render_servitudes_map
from visuels.carte_dpu import compute_dpu_result, render_dpu_map
from pdf.rapport import generate_rapport_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("batch")


# ==========================================================================
# Helpers
# ==========================================================================

def _slug(uf_parcelles: list) -> str:
    """Slug court pour nommer le dossier de sortie."""
    commune = uf_parcelles[0].get("commune", "inconnu").replace(" ", "-")
    refs = "-".join(
        f"{p['section']}{p['numero'].lstrip('0') or '0'}"
        for p in uf_parcelles[:3]
    )
    if len(uf_parcelles) > 3:
        refs += f"-et{len(uf_parcelles) - 3}autres"
    safe = "".join(
        c if c.isalnum() or c in "-_" else ""
        for c in f"{commune}_{refs}"
    )
    return safe


def _refs(uf_parcelles: list) -> list:
    """Convertit une liste de dicts JSON en ParcelleRef (gère code_insee et insee)."""
    return [
        ParcelleRef(
            section=str(p.get("section", "")).strip(),
            numero=str(p.get("numero", "")).strip(),
            insee=str(p.get("code_insee") or p.get("insee") or "").strip(),
            commune=str(p.get("commune", "")).strip(),
        )
        for p in uf_parcelles
    ]


# ==========================================================================
# Pipeline pour une UF
# ==========================================================================

def run_one(uf_parcelles: list, out_dir: Path, dpi: int = 150) -> dict:
    """
    Pipeline complet pour une UF.
    Retourne un dict de métadonnées (succès ou échec avec traceback dans error.txt).
    """
    refs    = _refs(uf_parcelles)
    commune = refs[0].commune
    slug    = _slug(uf_parcelles)

    meta = {
        "slug":               slug,
        "commune":            commune,
        "parcelles":          [f"{r.section} {r.numero}" for r in refs],
        "status":             "pending",
        "pdf_path":           None,
        "intersections_count": 0,
        "layers_found":       [],
        "warnings":           [],
        "error":              None,
        "duration_s":         0.0,
    }

    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Géométries IGN ────────────────────────────────────────────────
        parc_results = fetch_parcelles(refs)
        ok     = [r for r in parc_results if r.ok]
        failed = [r for r in parc_results if not r.ok]

        if not ok:
            raise RuntimeError(
                "Aucune parcelle récupérée : "
                + " | ".join(r.error or "?" for r in failed)
            )
        for r in failed:
            msg = f"Parcelle {r.ref.label} non récupérée : {r.error}"
            log.warning("   ⚠  %s", msg)
            meta["warnings"].append(msg)

        # 2. UF ────────────────────────────────────────────────────────────
        uf_gdf  = build_uf(parc_results)
        surface = uf_surface_m2(uf_gdf)
        geom    = uf_geojson(uf_gdf)
        pd_list = parcelles_detail(parc_results)
        log.info("   UF : %d parcelle(s) | %.0f m²", len(ok), surface)

        # 3. Intersections WFS GPU ─────────────────────────────────────────
        intersections, plu_pct_stats = compute_intersections(uf_gdf, buffer_m=300.0)
        meta["intersections_count"] = len(intersections)
        meta["layers_found"]        = [i.get("display_name", "") for i in intersections]
        log.info("   Intersections : %d couche(s)", len(intersections))

        result = {
            "parcelle":              ", ".join(r.ref.label for r in ok),
            "commune":               commune,
            "insee":                 refs[0].insee,
            "nb_intersections":      len(intersections),
            "intersections":         intersections,
            "surface_uf_m2":         round(surface, 2),
            "geometry":              geom,
            "parcelles_cadastrales": [
                {"section": r.ref.section, "numero": r.ref.numero} for r in ok
            ],
            "parcelles_uf_detail":   pd_list,
        }

        # 4. Cartes + PDF ──────────────────────────────────────────────────
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp     = Path(tmpdir)
            plu_png = None
            sup_png = None

            # Carte PLU
            if plu_pct_stats:
                try:
                    plu_lr = _fetch_layer(
                        GPU_LAYERS_BY_TABLE["zone_urba"],
                        gdf_bbox_4326(uf_gdf, buffer_m=50.0),
                        timeout=30,
                    )
                    if plu_lr.ok:
                        plu_png = str(tmp / "plu_map.png")
                        render_plu_map(uf_gdf, plu_lr.gdf, plu_pct_stats, plu_png, dpi=dpi)
                        shutil.copy2(plu_png, out_dir / "plu_map.png")
                        log.info("   ✓ Carte PLU")
                except Exception as e:
                    msg = f"Carte PLU ignorée : {e}"
                    log.warning("   ⚠  %s", msg)
                    meta["warnings"].append(msg)
                    plu_png = None

            # Carte servitudes
            has_sup = any(i.get("article") == "4" for i in intersections)
            if has_sup:
                try:
                    bbox_sup = gdf_bbox_4326(uf_gdf, buffer_m=300.0)
                    sup_gdfs = {}
                    for table in ("assiette_sup_s", "assiette_sup_l"):
                        cfg = GPU_LAYERS_BY_TABLE.get(table)
                        if not cfg:
                            continue
                        lr = _fetch_layer(cfg, bbox_sup, timeout=30)
                        if lr.ok:
                            filtered = intersects_gdf(uf_gdf, lr.gdf)
                            if not filtered.empty:
                                sup_gdfs[table] = filtered
                    if sup_gdfs:
                        sup_png = str(tmp / "servitudes_map.png")
                        render_servitudes_map(
                            uf_gdf=uf_gdf,
                            sup_gdfs=sup_gdfs,
                            out_path=sup_png,
                            buffer_m=300.0,
                            dpi=dpi,
                        )
                        shutil.copy2(sup_png, out_dir / "servitudes_map.png")
                        log.info("   ✓ Carte servitudes (%d couche(s))", len(sup_gdfs))
                except Exception as e:
                    msg = f"Carte servitudes ignorée : {e}"
                    log.warning("   ⚠  %s", msg)
                    meta["warnings"].append(msg)
                    sup_png = None

            # Carte DPU (toujours générée — soumise ou non)
            dpu_png = None
            dpu_res = None
            try:
                dpu_res = compute_dpu_result(uf_gdf, buffer_m=300.0, intersections=intersections)
                dpu_png = str(tmp / "dpu_map.png")
                render_dpu_map(
                    uf_gdf=uf_gdf,
                    dpu_gdf=dpu_res["dpu_gdf"],
                    out_path=dpu_png,
                    intersecte=dpu_res["intersecte"],
                    dpi=dpi,
                )
                shutil.copy2(dpu_png, out_dir / "dpu_map.png")
                status_dpu = "soumise" if dpu_res["intersecte"] else "non soumise"
                log.info("   ✓ Carte DPU (%s)", status_dpu)
            except Exception as e:
                msg = f"Carte DPU ignorée : {e}"
                log.warning("   ⚠  %s", msg)
                meta["warnings"].append(msg)
                dpu_png = None
                dpu_res = None

            # PDF
            pdf_tmp = generate_rapport_pdf(
                result,
                output_dir=str(tmp),
                plu_map_png=plu_png,
                servitudes_map_png=sup_png,
                dpu_map_png=dpu_png,
                dpu_result=dpu_res,
            )
            pdf_final = out_dir / Path(pdf_tmp).name
            shutil.copy2(pdf_tmp, pdf_final)

        meta["status"]   = "ok"
        meta["pdf_path"] = str(pdf_final)
        log.info("   ✅ %s", pdf_final.name)

    except Exception as exc:
        meta["status"] = "error"
        meta["error"]  = str(exc)
        (out_dir / "error.txt").write_text(
            f"{datetime.now().isoformat()}\n\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        log.error("   ❌ %s", exc)

    meta["duration_s"] = round(time.time() - t0, 1)
    return meta


# ==========================================================================
# Runner batch
# ==========================================================================

def run_batch(
    json_path: str,
    out_dir: str,
    dpi: int = 150,
    random_n: int | None = None,
    only_index: int | None = None,
) -> None:
    json_file = Path(json_path)
    if not json_file.is_file():
        # Essai relatif au script
        json_file = HERE / json_path
    if not json_file.is_file():
        log.error("Fichier JSON introuvable : %s", json_path)
        sys.exit(1)

    with open(json_file, encoding="utf-8") as f:
        batch: list = json.load(f)

    if not isinstance(batch, list) or not batch:
        log.error("Le JSON doit être une liste non vide de listes de parcelles.")
        sys.exit(1)

    n_total_available = len(batch)

    if only_index is not None and random_n is not None:
        log.error("Utilise soit --only_index, soit --random_n, mais pas les deux.")
        sys.exit(1)

    if only_index is not None:
        if only_index < 1 or only_index > n_total_available:
            log.error(
                "Le numéro de liste demandé (--only_index=%s) doit être entre 1 et %s.",
                only_index,
                n_total_available,
            )
            sys.exit(1)
        selected_items = [(only_index, batch[only_index - 1])]
    else:
        selected_items = list(enumerate(batch, start=1))
        if random_n is not None:
            if random_n < 1:
                log.error("Le paramètre --random_n doit être >= 1.")
                sys.exit(1)
            n_pick = min(random_n, n_total_available)
            picked = random.sample(selected_items, k=n_pick)
            # On garde l'ordre du JSON pour une lecture plus simple des logs.
            selected_items = sorted(picked, key=lambda x: x[0])

    out       = Path(out_dir) if Path(out_dir).is_absolute() else HERE / out_dir
    out.mkdir(parents=True, exist_ok=True)
    n_total   = len(selected_items)
    date_run  = datetime.now().strftime("%Y%m%d_%H%M%S")
    sep       = "=" * 66

    print(f"\n{sep}")
    print(f"  BATCH IDENTITÉ FONCIÈRE — {n_total} UF à traiter")
    print(f"  Total disponible dans JSON : {n_total_available}")
    print(f"  JSON    : {json_file}")
    print(f"  Sortie  : {out}")
    print(f"  DPI     : {dpi}")
    if only_index is not None:
        print(f"  Mode    : liste unique #{only_index}")
    elif random_n is not None:
        print(f"  Mode    : tirage aléatoire de {n_total} liste(s)")
    else:
        print("  Mode    : batch complet")
    print(f"{sep}\n")

    summary  = []
    n_ok = n_err = 0

    for run_idx, (idx, uf_parcelles) in enumerate(selected_items, start=1):
        slug    = _slug(uf_parcelles)
        folder  = f"{idx:02d}_{slug}"
        uf_out  = out / folder

        print(f"[{run_idx:02d}/{n_total}] {folder}")

        meta           = run_one(uf_parcelles, out_dir=uf_out, dpi=dpi)
        meta["index"]  = idx
        meta["folder"] = folder
        summary.append(meta)

        if meta["status"] == "ok":
            n_ok += 1
            layers_str = ", ".join(meta["layers_found"]) or "—"
            print(f"         ✅  {meta['intersections_count']} couche(s) | "
                  f"{meta['duration_s']:.1f}s")
            print(f"             {layers_str}")
        else:
            n_err += 1
            print(f"         ❌  {meta['error']}")

        for w in meta["warnings"]:
            print(f"         ⚠   {w}")

        print()

    # ── Résumé ────────────────────────────────────────────────────────────
    t_total = sum(m["duration_s"] for m in summary)
    print(f"{sep}")
    print(f"  BATCH TERMINÉ en {t_total:.0f}s")
    print(f"  ✅ {n_ok} succès  |  ❌ {n_err} erreur(s)  |  {n_total} total")
    print(f"{sep}\n")

    # Table récap
    print(f"  {'#':>3}  {'Commune':<28}  {'Parcelles':<20}  {'C':>3}  {'Durée':>6}  Statut")
    print(f"  {'─'*3}  {'─'*28}  {'─'*20}  {'─'*3}  {'─'*6}  {'─'*6}")
    for m in summary:
        parc = ", ".join(m["parcelles"])[:19]
        com  = m["commune"][:27]
        c    = str(m["intersections_count"]) if m["status"] == "ok" else "—"
        st   = "✅" if m["status"] == "ok" else "❌"
        print(f"  {m['index']:>3}  {com:<28}  {parc:<20}  {c:>3}  {m['duration_s']:>5.1f}s  {st}")
    print()

    # JSON résumé
    summary_path = out / f"batch_summary_{date_run}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  Résumé JSON → {summary_path}\n")


# ==========================================================================
# CLI
# ==========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch test — identité foncière V0")
    parser.add_argument("--json",    default=DEFAULT_JSON,    help="Fichier JSON de batch")
    parser.add_argument("--out_dir", default=DEFAULT_OUT_DIR, help="Dossier de sortie")
    parser.add_argument("--dpi",     type=int, default=150,   help="DPI cartes PNG")
    parser.add_argument(
        "--random_n",
        type=int,
        default=None,
        help="Nombre de listes à tirer aléatoirement dans le batch",
    )
    parser.add_argument(
        "--only_index",
        type=int,
        default=None,
        help="Exécuter uniquement la liste N (index 1-based du JSON)",
    )
    args = parser.parse_args()

    run_batch(
        json_path=args.json,
        out_dir=args.out_dir,
        dpi=args.dpi,
        random_n=args.random_n,
        only_index=args.only_index,
    )
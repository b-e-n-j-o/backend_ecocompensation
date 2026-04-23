#!/usr/bin/env python3
"""
test_pipeline.py
Script de test autonome (sans FastAPI) — lance le pipeline complet
et génère un rapport PDF dans le répertoire courant.

Usage :
    python test_pipeline.py
    python test_pipeline.py --section AC --numero 0042 --insee 33522 --commune "Bordeaux"
    python test_pipeline.py --section AL --numero 0074 --insee 33234 --commune "Latresne"
"""
import argparse
import logging
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Le script est dans identite_fonciere_v0/ — on ajoute le parent au path
sys.path.insert(0, str(Path(__file__).parent.parent))

from identite_fonciere_v0.core.parcelle import ParcelleRef, fetch_parcelles
from identite_fonciere_v0.core.unites_foncieres import build_uf, parcelles_detail, uf_geojson, uf_surface_m2
from identite_fonciere_v0.core.intersections import compute_intersections
from identite_fonciere_v0.core.gpu_wfs import GPU_LAYERS_BY_TABLE, _fetch_layer
from identite_fonciere_v0.utils.geo import gdf_bbox_4326
from identite_fonciere_v0.visuels.carte_plu import render_plu_map
from identite_fonciere_v0.visuels.carte_servitudes import render_servitudes_map
from identite_fonciere_v0.pdf.rapport import generate_rapport_pdf


def run(refs, out_dir=".", dpi=150):
    print(f"\n{'='*60}")
    print(f"  Pipeline Identité Foncière V0")
    print(f"{'='*60}")
    print(f"  Parcelles : {[r.label for r in refs]}")
    print(f"  Commune   : {refs[0].commune} (INSEE {refs[0].insee})")
    print()

    # 1. IGN WFS — géométries
    print("[1/4] Récupération géométries IGN …")
    results = fetch_parcelles(refs)
    ok = [r for r in results if r.ok]
    if not ok:
        errors = [r.error for r in results]
        print(f"❌ Aucune parcelle récupérée : {errors}")
        sys.exit(1)
    print(f"  ✓ {len(ok)}/{len(refs)} parcelle(s) OK")

    # 2. UF
    print("\n[2/4] Construction de l'unité foncière …")
    uf_gdf = build_uf(results)
    surface = uf_surface_m2(uf_gdf)
    geom = uf_geojson(uf_gdf)
    pd_list = parcelles_detail(results)
    print(f"  ✓ Surface UF : {surface:,.0f} m²")

    # 3. GPU WFS — intersections
    print("\n[3/4] Requêtes GPU WFS + intersections Shapely …")
    intersections, plu_pct_stats = compute_intersections(uf_gdf, buffer_m=300.0)
    print(f"  ✓ {len(intersections)} couche(s) intersectée(s)")
    if plu_pct_stats:
        print(f"  ✓ Zonages PLU : {plu_pct_stats}")

    result = {
        "parcelle": ", ".join(r.ref.label for r in ok),
        "commune": refs[0].commune,
        "insee": refs[0].insee,
        "nb_intersections": len(intersections),
        "intersections": intersections,
        "surface_uf_m2": round(surface, 2),
        "geometry": geom,
        "parcelles_cadastrales": [
            {"section": r.ref.section, "numero": r.ref.numero} for r in ok
        ],
        "parcelles_uf_detail": pd_list,
    }

    # 4. Cartes + PDF
    print("\n[4/4] Génération des cartes et du rapport PDF …")
    with tempfile.TemporaryDirectory() as tmpdir:
        plu_png = None
        sup_png = None

        # ── Carte PLU ────────────────────────────────────────────────────
        if plu_pct_stats:
            plu_cfg = GPU_LAYERS_BY_TABLE["zone_urba"]
            bbox_plu = gdf_bbox_4326(uf_gdf, buffer_m=50.0)
            plu_lr = _fetch_layer(plu_cfg, bbox_plu, timeout=30)
            if plu_lr.ok:
                plu_png = str(Path(tmpdir) / "plu_map.png")
                try:
                    render_plu_map(uf_gdf, plu_lr.gdf, plu_pct_stats, plu_png, dpi=dpi)
                    print("  ✓ Carte PLU générée")
                except Exception as e:
                    print(f"  ⚠️  Carte PLU ignorée : {e}")
                    plu_png = None

        # ── Carte servitudes ─────────────────────────────────────────────
        has_sup = any(inter.get("article") == "4" for inter in intersections)
        if has_sup:
            bbox_sup = gdf_bbox_4326(uf_gdf, buffer_m=300.0)
            sup_gdfs = {}
            for table in ("assiette_sup_s", "assiette_sup_l"):
                cfg = GPU_LAYERS_BY_TABLE.get(table)
                if not cfg:
                    continue
                lr = _fetch_layer(cfg, bbox_sup, timeout=30)
                if lr.ok:
                    from identite_fonciere_v0.utils.geo import intersects_gdf
                    filtered = intersects_gdf(uf_gdf, lr.gdf)
                    if not filtered.empty:
                        sup_gdfs[table] = filtered

            if sup_gdfs:
                sup_png = str(Path(tmpdir) / "servitudes_map.png")
                try:
                    render_servitudes_map(
                        uf_gdf=uf_gdf,
                        sup_gdfs=sup_gdfs,
                        out_path=sup_png,
                        buffer_m=300.0,
                        dpi=dpi,
                    )
                    print(f"  ✓ Carte servitudes générée ({len(sup_gdfs)} couche(s))")
                except Exception as e:
                    print(f"  ⚠️  Carte servitudes ignorée : {e}")
                    sup_png = None

        pdf_path = generate_rapport_pdf(
            result,
            output_dir=out_dir,
            plu_map_png=plu_png,
            servitudes_map_png=sup_png,
        )

    print(f"\n{'='*60}")
    print(f"  ✅ Rapport généré :")
    print(f"     {pdf_path}")
    print(f"{'='*60}\n")
    return pdf_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test pipeline identité foncière V0")
    parser.add_argument("--section", default="AL", help="Section cadastrale")
    parser.add_argument("--numero", default="0074", help="Numéro de parcelle")
    parser.add_argument("--insee", default="33234", help="Code INSEE commune")
    parser.add_argument("--commune", default="Latresne", help="Nom de la commune")
    parser.add_argument("--out_dir", default=".", help="Répertoire de sortie du PDF")
    parser.add_argument("--dpi", type=int, default=150, help="DPI des cartes")
    args = parser.parse_args()

    refs = [ParcelleRef(
        section=args.section,
        numero=args.numero,
        insee=args.insee,
        commune=args.commune,
    )]
    run(refs, out_dir=args.out_dir, dpi=args.dpi)
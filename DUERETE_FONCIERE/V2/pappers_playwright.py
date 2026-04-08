"""
Scraping Pappers via Playwright — données complètes
====================================================
Scroll progressif + attente lazy loading pour capturer
l'intégralité de la page dont les biens immobiliers.

Usage : python pappers_playwright.py --siren 892632365
"""

import argparse, json, asyncio, re
from playwright.async_api import async_playwright


async def scrape_pappers(siren: str, debug: bool = False) -> dict:
    url = f"https://www.pappers.fr/entreprise/{siren}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print(f"Chargement : {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # ── Scroll progressif pour déclencher le lazy loading ────────────
        # Pappers charge les biens immobiliers en lazy → il faut scroller
        # jusqu'en bas par étapes et attendre entre chaque
        print("Scroll progressif ...")
        for step in range(0, 20):
            await page.evaluate(f"window.scrollTo(0, {step * 800})")
            await page.wait_for_timeout(300)

        # Scroll final tout en bas + attente réseau
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        # Re-scroll vers le haut puis bas pour forcer tous les observers
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)

        final_url = page.url
        print(f"URL finale  : {final_url}")

        # ── Texte brut COMPLET ───────────────────────────────────────────
        full_text = await page.evaluate("() => document.body.innerText")
        print(f"Texte total : {len(full_text)} chars")

        result = {
            "siren": siren,
            "url": final_url,
            "full_text": full_text,  # complet, pas de troncature
        }

        # ── Parsing du texte brut ────────────────────────────────────────

        # 1. Conformité
        result["conformite"] = {
            "procedures_collectives": _extract_int(full_text, r"(\d+)\s*procédure[s]?\s*collective[s]?"),
            "contentieux":            _extract_int(full_text, r"(\d+)\s*contentieux"),
            "sanctions":              _extract_int(full_text, r"(\d+)\s*sanction[s]?"),
            "comptes_disponibles":    "Aucun compte n'est disponible" not in full_text,
        }

        # 2. Dirigeants avec âges (plus précis que l'Annuaire)
        dirigeants = []
        pattern_dir = re.compile(
            r"([\w\s\(\)]+)\n([\w\s]+)\n(\d+)\s*ans\s*-\s*([\d/]+)\nDepuis le ([\d/]+)"
        )
        for m in pattern_dir.finditer(full_text):
            dirigeants.append({
                "nom":          m.group(1).strip(),
                "qualite":      m.group(2).strip(),
                "age":          int(m.group(3)),
                "naissance":    m.group(4).strip(),
                "depuis":       m.group(5).strip(),
            })
        result["dirigeants"] = dirigeants

        # 3. Biens immobiliers — pattern : "Surface : XXXXm²" + commune
        # Structure Pappers :
        #   " 0 NOM_LIEU - COMMUNE (dept)\nSurface : XXXXm²"
        biens = []
        pattern_bien = re.compile(
            r"(?:[\u00a0\s]*\d+\s+)?([\w\s\'\-]+?)\s*-\s*([\w\s\-]+?)\s*\((\d{2})\)\s*\nSurface\s*:\s*([\d\s]+)m²",
            re.MULTILINE
        )
        for m in pattern_bien.finditer(full_text):
            biens.append({
                "lieu":       m.group(1).strip(),
                "commune":    m.group(2).strip(),
                "departement": m.group(3).strip(),
                "surface_m2": int(m.group(4).replace(" ", "").replace("\u00a0", "")),
            })

        # Fallback : cherche toutes les lignes "Surface : Xm²"
        if not biens:
            pattern_surface = re.compile(r"Surface\s*:\s*([\d\s\u00a0]+)m²")
            for m in pattern_surface.finditer(full_text):
                surface = int(m.group(1).replace(" ", "").replace("\u00a0", ""))
                # Contexte : 2 lignes avant pour avoir lieu/commune
                start = max(0, m.start() - 150)
                ctx = full_text[start:m.start()]
                biens.append({"surface_m2": surface, "contexte": ctx.strip()[-80:]})

        result["biens_immobiliers"] = biens
        result["nb_biens"] = len(biens)
        result["surface_totale_m2"] = sum(b.get("surface_m2", 0) for b in biens)

        # 4. Annonces BODACC dans la page
        bodacc_section = ""
        if "Annonces BODACC" in full_text:
            idx = full_text.index("Annonces BODACC")
            bodacc_section = full_text[idx:idx+1000]
        result["bodacc_page"] = bodacc_section

        # 5. Infos juridiques clés
        result["infos_juridiques"] = {
            "capital_social":   _extract_str(full_text, r"Capital social\s*:\s*([\d\s,\.€]+)"),
            "forme_juridique":  _extract_str(full_text, r"Forme juridique\s*:\s*(.+)"),
            "greffe":           _extract_str(full_text, r"greffe de\s+([A-Z\-\s]+),"),
            "date_immat_rcs":   _extract_str(full_text, r"le (\d{2}/\d{2}/\d{4})\s*\)"),
            "naf":              _extract_str(full_text, r"Code NAF ou APE\s*:\s*(\w+\.\w+)"),
            "tva":              _extract_str(full_text, r"Numéro de TVA\s*:\s*\n?(FR\w+)"),
        }

        if debug:
            result["full_text"] = full_text  # déjà là

        await browser.close()
        return result


def _extract_int(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _extract_str(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _print_summary(r: dict):
    print(f"\n{'='*55}")
    print(f"  {r.get('siren')} — {r.get('url', '').split('/')[-1]}")
    print(f"{'='*55}")

    ij = r.get("infos_juridiques", {})
    print(f"Forme juridique  : {ij.get('forme_juridique')}")
    print(f"Capital social   : {ij.get('capital_social')}")
    print(f"Greffe           : {ij.get('greffe')}")
    print(f"NAF              : {ij.get('naf')}")

    print(f"\n── Dirigeants ({len(r.get('dirigeants', []))}) ──")
    for d in r.get("dirigeants", []):
        print(f"  {d['nom']} | {d['qualite']} | {d['age']} ans | depuis {d['depuis']}")

    print(f"\n── Conformité ──")
    c = r.get("conformite", {})
    print(f"  Procédures : {c.get('procedures_collectives')} | Contentieux : {c.get('contentieux')} | Sanctions : {c.get('sanctions')}")
    print(f"  Comptes disponibles : {c.get('comptes_disponibles')}")

    print(f"\n── Biens immobiliers : {r.get('nb_biens')} parcelle(s) ──")
    print(f"  Surface totale : {r.get('surface_totale_m2', 0):,} m² ({r.get('surface_totale_m2', 0)/10000:.1f} ha)")
    for b in r.get("biens_immobiliers", [])[:10]:
        print(f"  {b.get('lieu', '?')} — {b.get('commune', '?')} ({b.get('departement', '?')}) : {b.get('surface_m2', '?'):,} m²")
    if r.get("nb_biens", 0) > 10:
        print(f"  ... et {r['nb_biens'] - 10} autres parcelles")

    if r.get("bodacc_page"):
        print(f"\n── BODACC (extrait page) ──")
        print(r["bodacc_page"][:300])

    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--siren",  required=True)
    parser.add_argument("--json",   action="store_true", help="Sortie JSON brute complète")
    parser.add_argument("--debug",  action="store_true", help="Inclut le texte brut complet")
    args = parser.parse_args()

    result = asyncio.run(scrape_pappers(args.siren, debug=args.debug))

    _print_summary(result)

    if args.json:
        # Exclut full_text du JSON sauf si --debug
        if not args.debug:
            result.pop("full_text", None)
        print(json.dumps(result, indent=2, ensure_ascii=False))
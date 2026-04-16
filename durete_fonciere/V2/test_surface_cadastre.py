"""
Test surface cadastrale via WFS IGN
Vérifie la surface réelle d'une parcelle depuis le cadastre officiel.
"""
import requests, json, argparse

WFS_URL = "https://data.geopf.fr/wfs/ows"

def fetch_parcelle_info(insee: str, section: str, numero: str) -> dict:
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
        "srsName": "EPSG:2154",
        "outputFormat": "application/json",
        "CQL_FILTER": f"code_insee='{insee}' AND section='{section}' AND numero='{numero}'",
    }
    r = requests.get(WFS_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    features = data.get("features", [])
    if not features:
        return {}
    f = features[0]
    props = f.get("properties", {})

    # Calcul surface depuis géométrie si dispo
    from shapely.geometry import shape
    geom = shape(f["geometry"])
    surface_m2_geom = geom.area  # en m² si EPSG:2154

    return {
        "code_insee":  props.get("code_insee"),
        "section":     props.get("section"),
        "numero":      props.get("numero"),
        "contenance":  props.get("contenance"),   # surface officielle cadastre (en m²)
        "surface_geom_m2": round(surface_m2_geom, 1),
        "adresse":     props.get("adresse"),
        "properties_raw": props,
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--insee",   default="86275")
    parser.add_argument("--section", default="0D")
    parser.add_argument("--numero",  default="0319")
    args = parser.parse_args()

    print(f"\nRequête WFS cadastral : {args.insee} {args.section} {args.numero}\n")
    result = fetch_parcelle_info(args.insee, args.section, args.numero)

    if result:
        print(f"Contenance cadastrale officielle : {result['contenance']} m²")
        print(f"Surface calculée depuis géométrie : {result['surface_geom_m2']} m²")
        print(f"\nProperties brutes :")
        print(json.dumps(result["properties_raw"], indent=2, ensure_ascii=False))
    else:
        print("Parcelle introuvable")
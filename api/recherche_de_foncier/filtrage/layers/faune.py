"""Points faune vus par le filtre : base régionale + observations d'écocompensation.

Le nom du référentiel (`fauna.nom_vernaculaire`) et le nom importé (`observation_faune.nom_espece`)
ne sont pas supposés identiques. Le filtre cherche le nom du référentiel dans fauna, et seulement
les noms explicitement associés dans les observations d'écocompensation.
"""


def chain_sql(parts: list[str], joins: list[str] | None, fallback: str = "or") -> str:
    """Associe les conditions de gauche à droite : (A ET B) OU C."""
    if not parts:
        return "TRUE"
    expr = f"({parts[0]})"
    for i, part in enumerate(parts[1:]):
        raw = joins[i] if joins and i < len(joins) else fallback
        op = "AND" if str(raw).lower() == "and" else "OR"
        expr = f"({expr} {op} ({part}))"
    return expr


def criterion_names(species: str, aliases: list[str] | None = None) -> list[str]:
    """Noms cochés : référentiel + alias confirmés. Les noms restés neutres n'y figurent pas."""
    names: list[str] = []
    for raw in [species, *(aliases or [])]:
        name = str(raw or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def aliases_only(aliases: list[str] | None = None) -> list[str]:
    names: list[str] = []
    for raw in aliases or []:
        name = str(raw or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def fauna_exists_sql(geom_col: str, i: int) -> str:
    """Fauna régionale = nom du référentiel. Observations perso = seulement les noms cochés."""
    return f"""
                EXISTS (
                    SELECT 1
                    FROM ecocompensation.fauna f
                    WHERE f.nom_vernaculaire = :fauna_species_{i}
                      AND ST_DWithin({geom_col}, f.geometry, :fauna_dist_m_{i})
                )
                OR EXISTS (
                    SELECT 1
                    FROM ecocompensation.observation_faune o
                    WHERE o.nom_espece = ANY(CAST(:fauna_aliases_{i} AS text[]))
                      AND ST_DWithin({geom_col}, o.geom, :fauna_dist_m_{i})
                )
    """

FAUNA_POINTS = """(
    SELECT f.nom_vernaculaire AS nom_vernaculaire, f.geometry AS geometry
    FROM ecocompensation.fauna f
    WHERE f.nom_vernaculaire IS NOT NULL
      AND f.geometry IS NOT NULL
    UNION ALL
    SELECT o.nom_espece, o.geom
    FROM ecocompensation.observation_faune o
    WHERE o.nom_espece IS NOT NULL
      AND o.geom IS NOT NULL
)"""

"""Points faune vus par le filtre : base régionale + observations d'écocompensation.

Le nom du référentiel (`fauna.nom_vernaculaire`) et le nom importé (`observation_faune.nom_espece`)
ne sont pas supposés identiques. Le filtre cherche le nom du référentiel dans fauna, et seulement
les noms explicitement associés dans les observations d'écocompensation.
"""
import re

_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def observation_geom_2154(column: str = "o.geom") -> str:
    """geom sans contrainte de SRID : Lambert 93, WGS84, ou 0 selon l'import."""
    return f"""
        CASE
            WHEN ST_SRID({column}) = 2154 THEN {column}
            WHEN ST_SRID({column}) = 4326 THEN ST_Transform({column}, 2154)
            WHEN ST_SRID({column}) = 0
             AND abs(ST_X(ST_PointOnSurface({column}))) <= 180
             AND abs(ST_Y(ST_PointOnSurface({column}))) <= 90
                THEN ST_Transform(ST_SetSRID({column}, 4326), 2154)
            ELSE ST_SetSRID(ST_Force2D({column}), 2154)
        END
    """


def names_match_sql(column: str, array_param: str = ":names") -> str:
    """Égalité insensible à la casse, avec ou sans suffixe « (Le) »."""
    return f"""(
        lower(btrim({column})) IN (
            SELECT lower(btrim(n)) FROM unnest(CAST({array_param} AS text[])) n
        )
        OR lower(btrim({column})) IN (
            SELECT lower(btrim(regexp_replace(n, '\\s*\\([^)]*\\)\\s*$', '')))
            FROM unnest(CAST({array_param} AS text[])) n
        )
    )"""


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
        stripped = _PAREN_SUFFIX.sub("", name).strip()
        if stripped and stripped not in names:
            names.append(stripped)
    return names


def aliases_only(aliases: list[str] | None = None) -> list[str]:
    names: list[str] = []
    for raw in aliases or []:
        name = str(raw or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def fauna_exists_sql(geom_col: str, i: int) -> str:
    """Fauna régionale = nom du référentiel. Observations perso = nom du filtre + alias."""
    g2154 = observation_geom_2154("o.geom")
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
                    WHERE (
                        lower(btrim(o.nom_espece)) = lower(btrim(:fauna_species_{i}))
                        OR lower(btrim(o.nom_espece)) = lower(btrim(regexp_replace(:fauna_species_{i}, '\\s*\\([^)]*\\)\\s*$', '')))
                        OR {names_match_sql("o.nom_espece", f":fauna_aliases_{i}")}
                    )
                      AND ST_DWithin({geom_col}, {g2154}, :fauna_dist_m_{i})
                )
    """

def nearest_fauna_distance_sql(geom_expr: str) -> str:
    """Distance (m) au point le plus proche, ou NULL s'il n'y en a pas.

    Deux recherches séparées : un UNION ALL empêche l'index GiST
    (ORDER BY geom <-> … LIMIT 1) et force un parcours de tous les points.
    """
    g2154 = observation_geom_2154("o.geom")
    return f"""
        LEAST(
            (
                SELECT ROUND(ST_Distance({geom_expr}, f.geometry))::int
                FROM ecocompensation.fauna f
                WHERE f.geometry IS NOT NULL
                  AND {names_match_sql("f.nom_vernaculaire")}
                ORDER BY {geom_expr} <-> f.geometry
                LIMIT 1
            ),
            (
                SELECT ROUND(ST_Distance({geom_expr}, {g2154}))::int
                FROM ecocompensation.observation_faune o
                WHERE o.geom IS NOT NULL
                  AND {names_match_sql("o.nom_espece")}
                ORDER BY {geom_expr} <-> {g2154}
                LIMIT 1
            )
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

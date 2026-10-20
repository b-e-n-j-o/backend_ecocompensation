"""
Exclusion EBC : parcelle couverte à 100 % par un EBC opposable.

Source : ecocompensation.v_ebc (est_ebc = true).
ebc_a_creer est volontairement hors de cette exclusion : c'est une opportunité
de boisement, signalée à l'enrichissement, pas une parcelle à écarter.
"""

from __future__ import annotations

# Couverture quasi totale : tolère les micro-écarts de topologie GPU / cadastre.
EBC_FULL_COVER_RATIO = 0.995

# Contrainte de milieu ouvert. ebc_a_creer n'en fait pas partie.
EBC_CONTRAINTE_CLASSES: tuple[str, ...] = (
    "ebc",
    "ebc_littoral",
    "ebc_presume",
)

EBC_A_CREER_CLASSE = "ebc_a_creer"

EBC_VIEW = "ecocompensation.v_ebc"


def clause_exclude_ebc_couverture_totale(geom_alias: str) -> str:
    """Vrai (NOT …) tant que la parcelle n'est pas entièrement sous EBC contrainte."""
    classes_sql = ", ".join(f"'{c}'" for c in EBC_CONTRAINTE_CLASSES)
    return f"""
        NOT (
            ST_Area({geom_alias}.geom_2154) > 0
            AND COALESCE((
                SELECT ST_Area(
                    ST_Intersection(
                        {geom_alias}.geom_2154,
                        ST_UnaryUnion(ST_Collect(e.geom_2154))
                    )
                )
                FROM {EBC_VIEW} e
                WHERE e.geom_2154 IS NOT NULL
                  AND e.classe IN ({classes_sql})
                  AND {geom_alias}.geom_2154 && e.geom_2154
                  AND ST_Intersects({geom_alias}.geom_2154, e.geom_2154)
            ), 0) / ST_Area({geom_alias}.geom_2154) >= {EBC_FULL_COVER_RATIO}
        )
    """

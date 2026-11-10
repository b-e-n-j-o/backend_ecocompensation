#!/usr/bin/env python3
"""Produit un schéma PostGIS chargeable à partir du dump Supabase.

Ne modifie jamais schema_snapshot.sql. Chaque retrait ou adaptation est loggé.

    python build_test_schema.py --in ../../db/sql/schema_snapshot.sql --out test_schema.sql -v
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

log = logging.getLogger("build_test_schema")

PREAMBLE = """\
-- Généré par tests/db/build_test_schema.py. Ne pas éditer à la main.
-- Source : backend/db/sql/schema_snapshot.sql (non modifié).
-- Cible : postgis/postgis:17-3.5, driver psycopg 3.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Stub Supabase : les policies et DEFAULT auth.uid() référencent ce schéma.
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;
CREATE OR REPLACE FUNCTION auth.role() RETURNS text
    LANGUAGE sql STABLE AS $$ SELECT NULL::text $$;

"""


def transform(src: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    out_lines: list[str] = []
    for lineno, line in enumerate(src.splitlines(keepends=True), 1):
        stripped = line.strip()
        if stripped.startswith("\\restrict") or stripped.startswith("\\unrestrict"):
            notes.append(f"L{lineno} retiré méta pg_dump 18 : {stripped[:40]}…")
            continue
        if stripped == "CREATE SCHEMA public;":
            notes.append(
                f"L{lineno} CREATE SCHEMA public retiré : le schéma existe déjà dans une base vierge"
            )
            out_lines.append("-- CREATE SCHEMA public; -- déjà présent\n")
            continue
        if "auth.uid()" in line:
            new = line.replace("auth.uid()", "auth.uid()")
            # La fonction stub est créée dans le préambule. On garde l'appel.
            notes.append(f"L{lineno} auth.uid() conservé via stub auth.uid() (préambule)")
            out_lines.append(new)
            continue
        if "auth.role()" in line:
            notes.append(f"L{lineno} auth.role() conservé via stub auth.role() (préambule)")
            out_lines.append(line)
            continue
        out_lines.append(line)

    # SET transaction_timeout est du PG 17 : conservé, l'image est 17.
    body = "".join(out_lines)
    if "CREATE EXTENSION" in src:
        notes.append("le dump source ne contient aucun CREATE EXTENSION ; ajoutés dans le préambule")
    else:
        notes.append(
            "aucun CREATE EXTENSION dans le dump (Supabase les installe hors schéma). "
            "Préambule : postgis, pg_trgm, uuid-ossp. gen_random_uuid est dans le catalogue PG 17."
        )
    notes.append(
        "RLS et policies conservés. Le rôle de test est superuser (postgres) : le RLS ne bloque pas le propriétaire. "
        "Policies auth.role() dépendent du stub, pas du schéma auth Supabase."
    )
    notes.append(
        "Aucun rôle anon/authenticated/service_role, aucune publication, storage, graphql, vault, realtime dans ce dump."
    )
    notes.append("COMMENT ON SCHEMA public conservé.")
    return PREAMBLE + body, notes


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="src", type=Path, default=here.parents[1] / "db" / "sql" / "schema_snapshot.sql")
    parser.add_argument("--out", dest="dst", type=Path, default=here / "test_schema.sql")
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(message)s")
    raw = args.src.read_text(encoding="utf-8")
    transformed, notes = transform(raw)
    args.dst.write_text(transformed, encoding="utf-8")
    log.info("écrit %s (%d octets) depuis %s", args.dst, args.dst.stat().st_size, args.src)
    for note in notes:
        log.info("adapté : %s", note)
    if re.search(r"\\restrict|\\unrestrict", transformed):
        raise SystemExit("méta-commande pg_dump encore présente")


if __name__ == "__main__":
    main()

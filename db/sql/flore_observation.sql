-- Observations flore (points, lignes, polygones) + traçabilité d'import.
-- Hypothèses isolées : nom_taxon_v = libellé source tel quel (souvent vernaculaire) ;
-- cd_nom_valide = code TAXREF s'il est fourni (cd_nom ou cd_ref non tranché) ;
-- surface_population reste du texte tant que l'unité n'est pas confirmée.

create table if not exists ecocompensation.flore_import (
  id            bigint generated always as identity primary key,
  fichier       text not null,
  source        text,
  nb_lignes     int  not null default 0,
  nb_inserees   int  not null default 0,
  nb_doublons   int  not null default 0,
  nb_rejetees   int  not null default 0,
  rapport       jsonb not null default '{}'::jsonb,
  created_by    text,
  created_at    timestamptz not null default now()
);

create table if not exists ecocompensation.flore_observation (
  id                    bigint generated always as identity primary key,
  import_id             bigint references ecocompensation.flore_import(id) on delete cascade,
  id_donnee             text,
  observateur           text,
  nom_taxon_cite        text not null,
  cd_nom_valide         integer,
  nom_taxon_v           text,
  taxon_key             text generated always as (
                          coalesce('taxref:' || cd_nom_valide::text,
                                   'nom:' || lower(regexp_replace(btrim(nom_taxon_cite), '\s+', ' ', 'g')))
                        ) stored,
  denombrement_min      integer,
  denombrement_max      integer,
  stade_developpement   text,
  surface_population    text,
  spontaneite           text,
  remarque_observation  text,
  date_observation      date,
  milieu                text,
  remarque_milieu       text,
  attributs_source      jsonb not null default '{}'::jsonb,
  empreinte             text not null,
  geom                  geometry(Geometry, 2154) not null,
  created_at            timestamptz not null default now(),
  constraint flore_obs_geom_type check (
    GeometryType(geom) in ('POINT','MULTIPOINT','LINESTRING','MULTILINESTRING','POLYGON','MULTIPOLYGON')
  ),
  constraint flore_obs_empreinte_uq unique (empreinte)
);

create index if not exists flore_observation_geom_gix  on ecocompensation.flore_observation using gist (geom);
create index if not exists flore_observation_taxon_idx on ecocompensation.flore_observation (taxon_key);
create index if not exists flore_observation_import_idx on ecocompensation.flore_observation (import_id);

create or replace view ecocompensation.v_flore_taxons as
select taxon_key,
       mode() within group (order by nom_taxon_cite) as nom_scientifique,
       mode() within group (order by nom_taxon_v)    as nom_vernaculaire,
       max(cd_nom_valide)                            as cd_nom_valide,
       count(*)                                      as nb_observations,
       max(date_observation)                         as derniere_observation
from ecocompensation.flore_observation
group by taxon_key;

alter table ecocompensation_results.parcelles
  add column if not exists flora_distances jsonb;

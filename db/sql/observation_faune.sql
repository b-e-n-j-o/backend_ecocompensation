-- Réserve d'observations faune pour l'écocompensation.
-- Le nom d'espèce est la seule colonne métier verrouillée à l'import.
-- date_observation est optionnelle (souvent Date_Deb du gabarit SINP).
-- Tout le reste du gabarit source va dans details (jsonb).

create table if not exists ecocompensation.faune_import (
  id            bigint generated always as identity primary key,
  fichier       text not null,
  gabarit       text,
  source        text,
  nb_lignes     int  not null default 0,
  nb_inserees   int  not null default 0,
  nb_doublons   int  not null default 0,
  nb_rejetees   int  not null default 0,
  rapport       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);

create table if not exists ecocompensation.observation_faune (
  id                 bigint generated always as identity primary key,
  import_id          bigint references ecocompensation.faune_import(id) on delete cascade,
  nom_espece         text not null,
  date_observation   date,
  details            jsonb not null default '{}'::jsonb,
  empreinte          text not null,
  geom               geometry(Geometry, 2154) not null,
  created_at         timestamptz not null default now(),
  constraint observation_faune_geom_type check (
    GeometryType(geom) in ('POINT','MULTIPOINT','LINESTRING','MULTILINESTRING','POLYGON','MULTIPOLYGON')
  ),
  constraint observation_faune_empreinte_uq unique (empreinte)
);

create index if not exists observation_faune_geom_gix
  on ecocompensation.observation_faune using gist (geom);
create index if not exists observation_faune_nom_idx
  on ecocompensation.observation_faune (lower(nom_espece));
create index if not exists observation_faune_import_idx
  on ecocompensation.observation_faune (import_id);
create index if not exists observation_faune_details_gin
  on ecocompensation.observation_faune using gin (details);

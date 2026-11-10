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

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ecocompensation; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ecocompensation;


--
-- Name: ecocompensation_results; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ecocompensation_results;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- CREATE SCHEMA public; -- déjà présent


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: classe_ebc(text); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.classe_ebc(libelle text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $_$
  with l as (select ecocompensation.norm_lib(libelle) as s)
  select case
    when s = '' or s ~ '^(n|\?+)$'                                         then 'libelle_vide'
    when s ~ 'emplacement reserve|^er( |$)'                                 then 'emplacement_reserve'
    when s ~ '^evp$|patrimoine bati|espace libre|continuites ecologiques'   then 'hors_ebc'
    when s ~ 'significati|121-27'
      or (s ~ 'loi littoral' and s !~ 'hors loi littoral')                 then 'ebc_littoral'
    when s ~ '\mebc\M|e\.b\.c|bois.*clas|l\.?\s?(113|130)[-._ ]?1'
      then case when s ~ 'creer|planter|realiser'
                 and s !~ 'conserv|proteg|preserv|existant'                then 'ebc_a_creer'
                else 'ebc' end
    when s ~ '151-?(19|23)|r\.?\s?123-11|patrimoine naturel'                then 'protection_l151'
    when s ~ 'haie|alignement|arbre|plantation'                             then 'haies_arbres'
    when s ~ 'bois|foret|saule|parc|jardin'                                 then 'ebc_presume'
    else 'a_verifier'
  end from l
$_$;


--
-- Name: est_maitre_ouvrage_probable(text, text); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.est_maitre_ouvrage_probable(cj text, denom text) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT COALESCE(cj IN ('7343', '7344'), false)          -- communautés urbaines, métropoles
        OR COALESCE(upper(denom) ~ (
               '(SNCF|RESEAU FERRE|\mRFF\M|AUTOROUTE|\mASF\M|\mAPRR\M|\mSANEF\M|COFIROUTE|'
            || 'ELECTRICITE DE FRANCE|\mEDF\M|\mRTE\M|RESEAU DE TRANSPORT D.?ELECTRICITE|ENEDIS|'
            || 'GRT ?GAZ|TEREGA|GRAND PORT|PORT AUTONOME|VOIES NAVIGABLES|ETAT ROUTES|'
            || 'DIRECTION INTERDEPARTEMENTALE DES ROUTES|AEROPORT)'), false)
$$;


--
-- Name: est_public_typo(text); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.est_public_typo(typo text) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT typo IN ('collectivite', 'section_de_commune', 'intercommunalite_syndicat', 'etat',
                    'etablissement_public', 'bien_collectif_syndical')
$$;


--
-- Name: norm_lib(text); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.norm_lib(t text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
  select lower(translate(
    trim(regexp_replace(coalesce(t, ''), '\s+', ' ', 'g')),
    'àâäáãéèêëíìîïóòôöõúùûüçñœÀÂÄÁÃÉÈÊËÍÌÎÏÓÒÔÖÕÚÙÛÜÇÑ',
    'aaaaaeeeeiiiiooooouuuucnoAAAAAEEEEIIIIOOOOOUUUUCN'
  ))
$$;


--
-- Name: pm_voisinage(text, double precision); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.pm_voisinage(p_idu text, p_buffer_m double precision DEFAULT 200) RETURNS TABLE(owner_key text, siren text, denomination text, nb_parcelles bigint, surface_m2 double precision, est_proprietaire_cible boolean, contigu boolean)
    LANGUAGE sql STABLE
    AS $$
    WITH cible AS (
        SELECT ST_Union(geom) AS geom, array_agg(DISTINCT owner_key) AS owners
        FROM ecocompensation.pm_parcelle
        WHERE idu = p_idu
    )
    SELECT
        v.owner_key,
        min(v.siren),
        min(v.denomination),
        count(*),
        sum(v.contenance_m2),
        v.owner_key = ANY (c.owners),
        bool_or(ST_DWithin(v.geom, c.geom, 2))
    FROM ecocompensation.pm_parcelle v, cible c
    WHERE c.geom IS NOT NULL
      AND v.idu <> p_idu
      AND ST_DWithin(v.geom, c.geom, p_buffer_m)
    GROUP BY v.owner_key, c.owners
    ORDER BY sum(v.contenance_m2) DESC NULLS LAST;
$$;


--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
  new.updated_at = now();
  return new;
end $$;


--
-- Name: typologie_cj(text, text); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.typologie_cj(cj text, denom text DEFAULT NULL::text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $_$
    SELECT CASE
        -- code absent ou non standard ('*') : SNCF et réseau ferré, sinon inconnu
        WHEN cj IS NULL OR cj !~ '^[0-9]{4}$' THEN
            CASE WHEN upper(denom) ~ '(SNCF|RESEAU FERRE|RFF)' THEN 'etablissement_public'
                 ELSE 'inconnu' END
        -- biens sectionaux : communaux en apparence, verrouillés juridiquement (lift observé ~0,06)
        WHEN cj = '7313'                                         THEN 'section_de_commune'
        WHEN cj IN ('7210','7220','7229','7230','7312')          THEN 'collectivite'
        WHEN cj LIKE '734%' OR cj LIKE '735%'                    THEN 'intercommunalite_syndicat'
        WHEN cj LIKE '71%'                                       THEN 'etat'
        WHEN cj LIKE '73%' OR cj LIKE '74%' OR cj LIKE '4%'      THEN 'etablissement_public'
        -- groupements / sociétés forestières reconnus au nom, quel que soit le code (6599, 9220, 9900, 8490…)
        WHEN upper(denom) ~ '(GROUPEMENT FORES|GROUPT FORES|GROUP FORES|^GF |SOCIETE (CIVILE )?FORESTIERE|STE (CIVILE )?FORESTIERE)'
                                                                 THEN 'groupement_forestier'
        WHEN cj IN ('6534','6535','6538')                        THEN 'groupement_foncier_agricole'
        WHEN cj = '6536'                                         THEN 'groupement_forestier'
        WHEN cj IN ('6533','6597','6598') OR cj LIKE '631%'      THEN 'exploitation_agricole'
        WHEN cj IN ('6539','6540','6541','6542','6521')          THEN 'sci_immobilier'
        WHEN cj LIKE '65%'                                       THEN 'societe_civile_autre'
        WHEN cj IN ('5546','5547','5646')                        THEN 'bailleur_social'
        WHEN cj LIKE '5%' OR cj LIKE '62%'                       THEN 'societe_commerciale'
        WHEN cj = '9110'                                         THEN 'copropriete'
        -- 9900 « autre personne morale de droit privé » : fourre-tout, découpé par la dénomination
        WHEN cj = '9900' THEN
            CASE
                WHEN upper(denom) ~ '(INDIVIS|PROPRIETAIRES? (DU|DES) BND|\mBND\M|SUCCESSION|AYANTS? ?DROIT|HERITIERS|CONSORTS)'
                    THEN 'indivision_succession'
                WHEN upper(denom) ~ '(COMMISSION SYNDICALE|SYNDICAT DES HABITANTS|SYNDICAT DU |SYNDICAT DE LA |SYNDICAT INTERCOMMUNAL|\mSIVOM\M|\mSIVU\M|\mUSID\M)'
                    THEN 'bien_collectif_syndical'
                ELSE 'autre_droit_prive'
            END
        WHEN cj LIKE '92%' OR cj LIKE '93%'                      THEN 'association_fondation'
        WHEN cj LIKE '9%'                                        THEN 'autre_droit_prive'
        WHEN cj LIKE '8%'                                        THEN 'organisme_social_mutualiste'
        WHEN cj LIKE '3%'                                        THEN 'personne_morale_etrangere'
        ELSE 'autre'
    END
$_$;


--
-- Name: update_faune_updated_at(); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.update_faune_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_flore_updated_at(); Type: FUNCTION; Schema: ecocompensation; Owner: -
--

CREATE FUNCTION ecocompensation.update_flore_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: aoi; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.aoi (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code_insee text NOT NULL,
    buffer_m integer NOT NULL,
    geom_2154 public.geometry NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: arrachage_vignes; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.arrachage_vignes (
    c_sup_arra double precision,
    c_div_rena text,
    c_div_re_1 text,
    geom_2154 public.geometry
);


--
-- Name: bd_topo_et_cesbio_dups_backup; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.bd_topo_et_cesbio_dups_backup (
    id bigint,
    source text,
    nature text,
    libelle text,
    geom public.geometry(MultiPolygon,2154),
    libelle_prio text
);


--
-- Name: bv_spe_masse_d_eau; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.bv_spe_masse_d_eau (
    id bigint NOT NULL,
    cd_bv_spe_mdo text,
    nom_bv_spe_md text,
    niveau_prec text,
    surface_bvs double precision,
    cd_categori text,
    cd_masse_dea text,
    comment_bvs text,
    cd_eu_masse_d text,
    surface text,
    geom_2154 public.geometry(MultiPolygon,2154)
);


--
-- Name: cadastre_parcelles; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL
)
PARTITION BY LIST (code_dep);


--
-- Name: TABLE cadastre_parcelles; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.cadastre_parcelles IS 'Cadastre Etalab (latest), partitionné par département';


--
-- Name: cadastre_parcelles_16; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_16 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_16_new_code_dep_check CHECK ((code_dep = '16'::text))
);


--
-- Name: cadastre_parcelles_17; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_17 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_17_new_code_dep_check CHECK ((code_dep = '17'::text))
);


--
-- Name: cadastre_parcelles_19; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_19 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_19_new_code_dep_check CHECK ((code_dep = '19'::text))
);


--
-- Name: cadastre_parcelles_23; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_23 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_23_new_code_dep_check CHECK ((code_dep = '23'::text))
);


--
-- Name: cadastre_parcelles_24; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_24 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_24_new_code_dep_check CHECK ((code_dep = '24'::text))
);


--
-- Name: cadastre_parcelles_33; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_33 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_33_new_code_dep_check CHECK ((code_dep = '33'::text))
);


--
-- Name: cadastre_parcelles_40; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_40 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_40_new_code_dep_check CHECK ((code_dep = '40'::text))
);


--
-- Name: cadastre_parcelles_47; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_47 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_47_new_code_dep_check CHECK ((code_dep = '47'::text))
);


--
-- Name: cadastre_parcelles_64; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_64 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_64_new_code_dep_check CHECK ((code_dep = '64'::text))
);


--
-- Name: cadastre_parcelles_79; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_79 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_79_new_code_dep_check CHECK ((code_dep = '79'::text))
);


--
-- Name: cadastre_parcelles_86; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_86 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_86_new_code_dep_check CHECK ((code_dep = '86'::text))
);


--
-- Name: cadastre_parcelles_87; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_87 (
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    com_abs text,
    section text,
    numero text,
    contenance integer,
    arpente boolean,
    created date,
    updated date,
    geom public.geometry(MultiPolygon,2154),
    ingere_le timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cadastre_parcelles_87_new_code_dep_check CHECK ((code_dep = '87'::text))
);


--
-- Name: cadastre_parcelles_histo; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cadastre_parcelles_histo (
    dep text,
    millesime date NOT NULL,
    idu text NOT NULL,
    code_insee text,
    com_abs text,
    section text,
    numero text,
    geom public.geometry(MultiPolygon,2154)
);


--
-- Name: carhab_clean; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.carhab_clean (
    id_polygone_carhab double precision,
    code_eunis text,
    nom_eunis text,
    commentaire_conformite_biotope_departementale text,
    commentaire_conformite_physio_departementale text,
    geom public.geometry
);


--
-- Name: cartofriches; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cartofriches (
    site_id text,
    site_nom text,
    site_type text,
    site_adresse text,
    site_identif_date text,
    site_actu_date text,
    site_surface double precision,
    site_occupation text,
    site_url text,
    site_ademe_url text,
    site_projet_url text,
    site_securite text,
    site_numero_basias text,
    site_numero_basol text,
    activite_libelle text,
    activite_code text,
    activite_fin_annee text,
    comm_nom text,
    comm_insee text,
    dep text,
    bati_type text,
    bati_nombre double precision,
    bati_surface double precision,
    bati_pollution text,
    bati_vacance text,
    bati_patrimoine text,
    bati_etat text,
    local_ancienne_annee double precision,
    local_recent_annee double precision,
    proprio_type text,
    proprio_personne text,
    proprio_nom text,
    unite_fonciere_surface double precision,
    unite_fonciere_refcad text,
    l_catpro3txt text,
    friche_avec_vacance text,
    taux_artif_ff double precision,
    date_mutation text,
    sol_pollution_annee text,
    sol_pollution_existe text,
    sol_pollution_origine text,
    sol_pollution_commentaire text,
    sol_depollution_fiche text,
    urba_zone_type text,
    urba_zone_lib text,
    urba_zone_formdomi text,
    urba_zone_formdomi_txt text,
    urba_doc_type text,
    urba_datappro text,
    desserte_distance_route double precision,
    desserte_distance_ferroviaire double precision,
    desserte_distance_fluvial double precision,
    desserte_distance_maritime double precision,
    desserte_commentaire text,
    producteur_fk integer,
    source_nom text,
    source_producteur text,
    nature text,
    source_contact text,
    source_url text,
    site_reconv_annee text,
    site_reconv_type text,
    zone_activites text,
    site_vocadomi text,
    monuhisto text,
    monuhisto500 text,
    emprise_sol_bati text,
    zonage_enviro text,
    site_zaer text,
    distance_ite_bon double precision,
    distance_ite_mauvais double precision,
    site_statut text,
    long double precision,
    lat double precision,
    nom_prodcartofriches text,
    "P_residentiel" double precision,
    "P_equipement" double precision,
    "P_tertiaire" double precision,
    "P_industriel" double precision,
    "P_renaturation" double precision,
    "P_PV" double precision,
    geometry public.geometry(MultiPolygon,4326)
);


--
-- Name: cesbio; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.cesbio (
    id bigint NOT NULL,
    departement character(2) NOT NULL,
    classe smallint,
    validmean smallint,
    validstd real,
    confidence smallint,
    urbaindens real,
    urbain_diff real,
    zoneindcom real,
    routes real,
    colza real,
    cerealpail real,
    proteagine real,
    soja real,
    tournesol real,
    mais real,
    riz real,
    tuberracin real,
    prairie real,
    vergers real,
    vignes real,
    feuillus real,
    coniferes real,
    pelouse real,
    landes real,
    surfmin real,
    plagedune real,
    glace_neige real,
    eau real,
    serres real,
    aire real,
    geom public.geometry(Geometry,2154),
    libelle_classe text
);


--
-- Name: cesbio_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.cesbio_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cesbio_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.cesbio_id_seq OWNED BY ecocompensation.cesbio.id;


--
-- Name: composition_des_sols; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.composition_des_sols (
    wfs_id text,
    etude double precision,
    id_ucs double precision,
    ger double precision,
    ger_nom text,
    no_ucs double precision,
    nom_ucs text,
    pourcnt double precision,
    code text,
    url text,
    etat text,
    gestnnr text,
    file_ucs text,
    file_ger text,
    lien_ger text,
    structr text,
    geom_2154 public.geometry(Geometry,2154)
);


--
-- Name: data_interne_layer_notes; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.data_interne_layer_notes (
    layer_key text NOT NULL,
    note text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text
);


--
-- Name: TABLE data_interne_layer_notes; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.data_interne_layer_notes IS 'Note libre par couche SIG (clé catalogue / etude-* / user-*), indépendante du libellé.';


--
-- Name: COLUMN data_interne_layer_notes.layer_key; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.data_interne_layer_notes.layer_key IS 'Identifiant stable de la couche (InternalLayer.key), pas le nom affiché.';


--
-- Name: durete_fonciere_cache; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.durete_fonciere_cache (
    siren character(9) NOT NULL,
    cache_version text DEFAULT 'v1'::text NOT NULL,
    score_llm_base double precision,
    niveau_durete text,
    explication text,
    detail_axes jsonb,
    statut text DEFAULT 'ok'::text NOT NULL,
    avertissements jsonb DEFAULT '[]'::jsonb NOT NULL,
    denomination text,
    forme_juridique text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: emprise_zpens; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.emprise_zpens (
    id bigint NOT NULL,
    objectid integer,
    code_zpens text,
    libelle text,
    statut text,
    ref_deliberation text,
    date_creation text,
    date_derniere_modif text,
    operateur_foncier text,
    nb_parcelles_valide integer,
    surface_valide_ha double precision,
    date_sig_debut text,
    date_sig_fin text,
    st_area double precision,
    st_length double precision,
    attributs_source jsonb DEFAULT '{}'::jsonb NOT NULL,
    content_hash text,
    geom_2154 public.geometry(MultiPolygon,2154),
    geom_3857 public.geometry(MultiPolygon,3857),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: emprise_zpens_etl_log; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.emprise_zpens_etl_log (
    id bigint NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    n_source integer NOT NULL,
    n_inserted integer NOT NULL,
    n_updated integer NOT NULL,
    n_deleted integer NOT NULL,
    n_unchanged integer NOT NULL,
    note text
);


--
-- Name: emprise_zpens_etl_log_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.emprise_zpens_etl_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: emprise_zpens_etl_log_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.emprise_zpens_etl_log_id_seq OWNED BY ecocompensation.emprise_zpens_etl_log.id;


--
-- Name: emprise_zpens_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.emprise_zpens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: emprise_zpens_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.emprise_zpens_id_seq OWNED BY ecocompensation.emprise_zpens.id;


--
-- Name: espaces_boises_classes; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.espaces_boises_classes (
    cle text NOT NULL,
    gid bigint,
    gpu_doc_id text,
    gpu_status text,
    gpu_timestamp text,
    partition text,
    libelle text,
    txt text,
    typepsc text,
    stypepsc text,
    nomfic text,
    urlfic text,
    insee text,
    datappro text,
    datvalid text,
    idurba text,
    nature text,
    attr_hash text NOT NULL,
    geom_2154 public.geometry(MultiPolygon,2154) NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    classe text,
    est_ebc boolean
);


--
-- Name: espaces_naturels_sensibles_ens; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.espaces_naturels_sensibles_ens (
    id_com text,
    code_com text,
    parcelle text,
    section text,
    pre text,
    idu text,
    id_par text,
    supf double precision,
    texte text,
    nom_site text,
    commune text,
    date_acq text,
    refcad text,
    sup_calcul double precision,
    shape_leng double precision,
    shape_area double precision,
    geom_2154 public.geometry(Geometry,2154),
    geom_3857 public.geometry(Geometry,3857)
);


--
-- Name: etat_cours_d_eau; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.etat_cours_d_eau (
    gid double precision,
    "CdMasseDEa" text,
    "CdEuMasseD" text,
    "NomMasseDE" text,
    "SystemeRef" text,
    "CdEuBassin" text,
    "TypeMasseD" text,
    "CdEuSsBass" text,
    "CdCategori" text,
    "CdNatureMa" text,
    "CategorieG" text,
    "LongueurTo" double precision,
    "StrahlMax" double precision,
    "StrahlMin" double precision,
    "TailleFctS" text,
    "EtendueSur" text,
    "EtatEcoValue" text,
    "EtatEcoYear" text,
    "EtatEcoConfidence" bigint,
    geom_2154 public.geometry(MultiLineString,2154)
);


--
-- Name: etat_masses_d_eau; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.etat_masses_d_eau (
    gid bigint,
    "CdMasseDEa" text,
    "CdEuMasseD" text,
    "NomMasseDE" text,
    "SystemeRef" text,
    "CdEuBassin" text,
    "TypeMasseD" text,
    "CdEuSsBass" text,
    "CdCategori" text,
    "CdNatureMa" text,
    "CategorieG" text,
    "CdEntiteHy" text,
    "SurfaceTot" double precision,
    "EtatEcoValue" text,
    "EtatEcoYear" text,
    "EtatEcoConfidence" bigint,
    geom_2154 public.geometry(MultiPolygon,2154)
);


--
-- Name: fauna; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.fauna (
    id_releve text,
    id_obs text,
    date_debut date,
    date_fin date,
    classe text,
    ordre text,
    famille text,
    cd_ref bigint,
    nom_cite text,
    nom_taxref text,
    nom_vernaculaire text,
    geom_id bigint,
    geom_type text,
    geometry public.geometry(Geometry,2154),
    annee_obs integer,
    lon double precision,
    lat double precision
);


--
-- Name: fauna_taxa_ref; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.fauna_taxa_ref (
    tax text,
    protection_nationale character varying(50),
    niveau_patrimonialite character varying(20)
);


--
-- Name: faune; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.faune (
    id bigint NOT NULL,
    categorie character varying(50) NOT NULL,
    groupe_especes character varying(30) NOT NULL,
    nom_vernaculaire character varying(100) NOT NULL,
    nom_scientifique character varying(100) NOT NULL,
    directive_oiseaux character varying(10),
    directive_habitats character varying(20),
    protection_nationale character varying(50),
    liste_rouge_uicn_france character varying(20),
    liste_rouge_uicn_europe character varying(20),
    liste_rouge_nouvelle_aquitaine character varying(100),
    enjeu_conservation_na character varying(20),
    niveau_patrimonialite character varying(20),
    pna character varying(20),
    determinante_znieff character varying(150),
    enjeu_local_espece character varying(10),
    espece_exogene character varying(5),
    commentaire text,
    plante_hote character varying(200),
    milieu_frequente text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: TABLE faune; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.faune IS 'Base de données des espèces faunistiques - Nouvelle-Aquitaine 2025';


--
-- Name: COLUMN faune.categorie; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.faune.categorie IS 'Catégorie de la feuille source: OISEAUX NICHEURS, AMPHIBIENS, CHIROPTERES, etc.';


--
-- Name: COLUMN faune.liste_rouge_uicn_france; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.faune.liste_rouge_uicn_france IS 'Statut UICN: LC=Préoccupation mineure, NT=Quasi menacée, VU=Vulnérable, EN=En danger, CR=En danger critique';


--
-- Name: COLUMN faune.enjeu_conservation_na; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.faune.enjeu_conservation_na IS 'Niveau d''enjeu selon FAUNA 2020: Modéré, Notable, Fort, Très Fort, Majeur';


--
-- Name: COLUMN faune.determinante_znieff; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.faune.determinante_znieff IS 'Espèce déterminante pour les ZNIEFF en Nouvelle-Aquitaine';


--
-- Name: faune_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.faune_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: faune_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.faune_id_seq OWNED BY ecocompensation.faune.id;


--
-- Name: faune_import; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.faune_import (
    id bigint NOT NULL,
    fichier text NOT NULL,
    gabarit text,
    source text,
    nb_lignes integer DEFAULT 0 NOT NULL,
    nb_inserees integer DEFAULT 0 NOT NULL,
    nb_doublons integer DEFAULT 0 NOT NULL,
    nb_rejetees integer DEFAULT 0 NOT NULL,
    rapport jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: faune_import_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.faune_import ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ecocompensation.faune_import_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: faune_lin; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.faune_lin (
    index bigint,
    "IdCadreAc" text,
    "IdJdd" text,
    "IdReleve" text,
    "IdRegional" text,
    "IdProduct" text,
    "DateModif" text,
    "TypeSource" text,
    "RefSource" text,
    "NomCite" text,
    "TaxVersion" text,
    "CdNomCite" text,
    "CdRef" text,
    "CdEspece" text,
    "Classe" text,
    "Ordre" text,
    "Famille" text,
    "TaxNomVal" text,
    "TaxNomVern" text,
    "Observer" text,
    "StatPresen" text,
    "Determiner" text,
    "ComDetermi" text,
    "DateDebut" text,
    "DateFin" text,
    "GeomWkt" text,
    "TypeGeom" text,
    "NomLocalisation" text,
    "CodeDpt" text,
    "InseeCom" text,
    "NomCom" text,
    "Maille10" text,
    "Maille5" text,
    "Maille2" text,
    "Maille1" text,
    "Maille500" text,
    "Maille100" text,
    "ComLocal" text,
    "ComHabitat" text,
    "ComObserv" text,
    "SensiNiv" text,
    "SensiDate" text,
    "SensiRef" text,
    "NivValReg" text,
    "TypeValReg" text,
    "DateValReg" text,
    "DenbrMin" text,
    "DenbrMax" text,
    "ObjDenbr" text,
    "DenbrType" text,
    "IndicePres" text,
    "Sexe" text,
    "StadeDevlp" text,
    "ProcObserv" text,
    "Comportmt" text,
    "StatBiolog" text,
    "ComDescTax" text,
    "PreuvNum" text,
    geometry public.geometry(LineString,2154)
);


--
-- Name: faune_pct; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.faune_pct (
    index bigint,
    "IdCadreAc" text,
    "IdJdd" text,
    "IdReleve" text,
    "IdRegional" text,
    "IdProduct" text,
    "DateModif" text,
    "TypeSource" text,
    "RefSource" text,
    "NomCite" text,
    "TaxVersion" text,
    "CdNomCite" text,
    "CdRef" text,
    "CdEspece" text,
    "Classe" text,
    "Ordre" text,
    "Famille" text,
    "TaxNomVal" text,
    "TaxNomVern" text,
    "Observer" text,
    "StatPresen" text,
    "Determiner" text,
    "ComDetermi" text,
    "DateDebut" text,
    "DateFin" text,
    "GeomWkt" text,
    "TypeGeom" text,
    "NomLocalisation" text,
    "CodeDpt" text,
    "InseeCom" text,
    "NomCom" text,
    "Maille10" text,
    "Maille5" text,
    "Maille2" text,
    "Maille1" text,
    "Maille500" text,
    "Maille100" text,
    "ComLocal" text,
    "ComHabitat" text,
    "ComObserv" text,
    "SensiNiv" text,
    "SensiDate" text,
    "SensiRef" text,
    "NivValReg" text,
    "TypeValReg" text,
    "DateValReg" text,
    "DenbrMin" text,
    "DenbrMax" text,
    "ObjDenbr" text,
    "DenbrType" text,
    "IndicePres" text,
    "Sexe" text,
    "StadeDevlp" text,
    "ProcObserv" text,
    "Comportmt" text,
    "StatBiolog" text,
    "ComDescTax" text,
    "PreuvNum" text,
    geometry public.geometry(Point,2154)
);


--
-- Name: faune_surf; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.faune_surf (
    index bigint,
    "IdCadreAc" text,
    "IdJdd" text,
    "IdReleve" text,
    "IdRegional" text,
    "IdProduct" text,
    "DateModif" text,
    "TypeSource" text,
    "RefSource" text,
    "NomCite" text,
    "TaxVersion" text,
    "CdNomCite" text,
    "CdRef" text,
    "CdEspece" text,
    "Classe" text,
    "Ordre" text,
    "Famille" text,
    "TaxNomVal" text,
    "TaxNomVern" text,
    "Observer" text,
    "StatPresen" text,
    "Determiner" text,
    "ComDetermi" text,
    "DateDebut" text,
    "DateFin" text,
    "GeomWkt" text,
    "TypeGeom" text,
    "NomLocalisation" text,
    "CodeDpt" text,
    "InseeCom" text,
    "NomCom" text,
    "Maille10" text,
    "Maille5" text,
    "Maille2" text,
    "Maille1" text,
    "Maille500" text,
    "Maille100" text,
    "ComLocal" text,
    "ComHabitat" text,
    "ComObserv" text,
    "SensiNiv" text,
    "SensiDate" text,
    "SensiRef" text,
    "NivValReg" text,
    "TypeValReg" text,
    "DateValReg" text,
    "DenbrMin" text,
    "DenbrMax" text,
    "ObjDenbr" text,
    "DenbrType" text,
    "IndicePres" text,
    "Sexe" text,
    "StadeDevlp" text,
    "ProcObserv" text,
    "Comportmt" text,
    "StatBiolog" text,
    "ComDescTax" text,
    "PreuvNum" text,
    geometry public.geometry(Geometry,2154)
);


--
-- Name: filiation_vers_actuel; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.filiation_vers_actuel (
    code_dep text NOT NULL,
    id_hist text NOT NULL,
    code_insee text NOT NULL,
    prefixe text NOT NULL,
    section text NOT NULL,
    numero text NOT NULL,
    derniere_apparition date,
    id_actuel text NOT NULL,
    part_surface real,
    n_actuels integer NOT NULL,
    faible_confiance boolean NOT NULL,
    lignee_eteinte boolean NOT NULL
);
ALTER TABLE ONLY ecocompensation.filiation_vers_actuel ALTER COLUMN code_dep SET STATISTICS 1000;


--
-- Name: flore_import; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.flore_import (
    id bigint NOT NULL,
    fichier text NOT NULL,
    source text,
    nb_lignes integer DEFAULT 0 NOT NULL,
    nb_inserees integer DEFAULT 0 NOT NULL,
    nb_doublons integer DEFAULT 0 NOT NULL,
    nb_rejetees integer DEFAULT 0 NOT NULL,
    rapport jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: flore_import_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.flore_import ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ecocompensation.flore_import_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: flore_observation; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.flore_observation (
    id bigint NOT NULL,
    import_id bigint,
    id_donnee text,
    observateur text,
    nom_taxon_cite text NOT NULL,
    cd_nom_valide integer,
    nom_taxon_v text,
    taxon_key text GENERATED ALWAYS AS (COALESCE(('taxref:'::text || (cd_nom_valide)::text), ('nom:'::text || lower(regexp_replace(btrim(nom_taxon_cite), '\s+'::text, ' '::text, 'g'::text))))) STORED,
    denombrement_min integer,
    denombrement_max integer,
    stade_developpement text,
    surface_population text,
    spontaneite text,
    remarque_observation text,
    date_observation date,
    milieu text,
    remarque_milieu text,
    attributs_source jsonb DEFAULT '{}'::jsonb NOT NULL,
    empreinte text NOT NULL,
    geom public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT flore_obs_geom_type CHECK ((public.geometrytype(geom) = ANY (ARRAY['POINT'::text, 'MULTIPOINT'::text, 'LINESTRING'::text, 'MULTILINESTRING'::text, 'POLYGON'::text, 'MULTIPOLYGON'::text])))
);


--
-- Name: flore_observation_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.flore_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ecocompensation.flore_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: foncier; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.foncier (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    geom_2154 public.geometry(MultiPolygon,2154) NOT NULL,
    area_ha numeric,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: geomce_lin; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.geomce_lin (
    id uuid NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(MultiLineString,2154),
    created_at timestamp with time zone,
    millesime text,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: geomce_pct; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.geomce_pct (
    id uuid NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(MultiPoint,2154),
    created_at timestamp with time zone,
    millesime text,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: geomce_surf; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.geomce_surf (
    id uuid NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(MultiPolygon,2154),
    created_at timestamp with time zone,
    millesime text,
    updated_at timestamp with time zone,
    deleted_at timestamp with time zone
);


--
-- Name: natura_2000; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.natura_2000 (
    uid text NOT NULL,
    id text,
    id_local text,
    nom_site text,
    date_crea text,
    modif_adm text,
    modif_geo text,
    url_fiche text,
    surf_off text,
    acte_deb text,
    acte_fin text,
    gest_site text,
    operateur text,
    "precision" text,
    src_geom text,
    src_annee text,
    marin text,
    p1_nature text,
    p4_geologi text,
    id_mnhn text,
    area_sig double precision,
    cd_sig text,
    territoire text,
    type text,
    geom_2154 public.geometry(MultiPolygon,2154),
    geom_3857 public.geometry(MultiPolygon,3857),
    _updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: observation_faune; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.observation_faune (
    id bigint NOT NULL,
    import_id bigint,
    nom_espece text NOT NULL,
    date_observation date,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    empreinte text NOT NULL,
    geom public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT observation_faune_geom_type CHECK ((public.geometrytype(geom) = ANY (ARRAY['POINT'::text, 'MULTIPOINT'::text, 'LINESTRING'::text, 'MULTILINESTRING'::text, 'POLYGON'::text, 'MULTIPOLYGON'::text])))
);


--
-- Name: observation_faune_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.observation_faune ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME ecocompensation.observation_faune_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ocs_ge; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.ocs_ge (
    id text,
    code_cs text,
    code_us text,
    millesime text,
    source text,
    ossature integer,
    id_origine text,
    code_or text,
    geometry public.geometry(MultiPolygon,2154),
    libelle_cs text,
    libelle_us text
);


--
-- Name: parcelles; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.parcelles (
    id text,
    gid integer,
    numero text,
    feuille integer,
    section text,
    code_dep text,
    nom_com text,
    code_com text,
    com_abs text,
    code_arr text,
    idu text,
    contenance double precision,
    code_insee text,
    geom_2154 public.geometry(Geometry,2154),
    aoi_id text
);


--
-- Name: parcelles_prospects_filtered; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.parcelles_prospects_filtered (
    siren text NOT NULL,
    denomination text,
    nb_mc_distinctes integer,
    nb_parcelles_deja_en_mc integer,
    surface_deja_en_mc_m2 double precision,
    est_acteur_public boolean,
    est_grand_industriel boolean,
    idu text NOT NULL,
    code_insee text,
    code_commune text,
    nom_commune text,
    section text,
    numero text,
    prefixe text,
    contenance double precision,
    suf text,
    nature_culture text,
    contenance_nature_culture text,
    geom_2154 public.geometry(Geometry,2154),
    parcelle_deja_en_mc boolean,
    computed_at timestamp with time zone,
    exclusion_reason text
);


--
-- Name: parcelles_syndicates; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.parcelles_syndicates (
    id bigint NOT NULL,
    ogc_fid bigint,
    geo_parcel text,
    idu text,
    tex text,
    geo_sectio text,
    nomcommune text,
    codecommun text,
    surface_ge bigint,
    contenance bigint,
    adresse text,
    urbain text,
    code text,
    comptecomm text,
    voie text,
    proprietai text,
    propriet_1 text,
    lot text,
    layer text,
    path text,
    surface double precision,
    geom_2154 public.geometry(MultiPolygon,2154) NOT NULL,
    geom_3857 public.geometry(MultiPolygon,3857) NOT NULL
);


--
-- Name: parcelles_syndicates_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.parcelles_syndicates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: parcelles_syndicates_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.parcelles_syndicates_id_seq OWNED BY ecocompensation.parcelles_syndicates.id;


--
-- Name: pm_commune_rang; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_commune_rang (
    owner_key text NOT NULL,
    code_insee text NOT NULL,
    nb_parcelles bigint,
    surface_m2 double precision,
    rang_pm_commune bigint,
    part_foncier_pm_commune double precision,
    part_du_patrimoine double precision
);


--
-- Name: pm_idu_coproprio; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_idu_coproprio (
    idu text NOT NULL,
    n_proprietaires_pm bigint
);


--
-- Name: pm_ilot; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_ilot (
    owner_key text NOT NULL,
    ilot_num integer NOT NULL,
    ilot_fonc integer,
    nb_parcelles bigint,
    surface_m2 double precision,
    geom public.geometry(MultiPolygon,2154),
    compacite double precision
);


--
-- Name: pm_ilot_membre; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_ilot_membre (
    owner_key text NOT NULL,
    idu text NOT NULL,
    ilot_strict integer,
    ilot_fonc integer
);


--
-- Name: pm_parcelle; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_parcelle (
    owner_key text NOT NULL,
    siren text,
    idu text NOT NULL,
    code_insee text,
    denomination text,
    categorie_juridique text,
    typologie text,
    est_public boolean,
    contenance_m2 double precision,
    geom public.geometry,
    geom_suspecte boolean,
    contenance_estimee boolean DEFAULT false
);


--
-- Name: pm_patch_owners; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_patch_owners (
    owner_key text NOT NULL
);


--
-- Name: pm_propension_typologie; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_propension_typologie (
    typologie text NOT NULL,
    nb_proprietaires bigint,
    nb_hotes_mc bigint,
    taux_hote_pct numeric,
    taux_global_pct numeric,
    lift numeric,
    niveau text,
    computed_at timestamp with time zone,
    justification text
);


--
-- Name: TABLE pm_propension_typologie; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.pm_propension_typologie IS 'Propension par typologie de propriétaire à accueillir de la compensation : part des propriétaires déjà hôtes de mesures compensatoires (GEOMCE) comparée à la moyenne. Maîtres d''ouvrage probables exclus. Recalculée par pm_propension.sql.';


--
-- Name: COLUMN pm_propension_typologie.lift; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_propension_typologie.lift IS 'Taux d''hôtes de la typologie / taux d''hôtes de l''ensemble. 1 = moyenne ; 4 = quatre fois plus fréquent ; 0,25 = quatre fois moins.';


--
-- Name: pm_proprietaire; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_proprietaire (
    owner_key text NOT NULL,
    siren text,
    denomination text,
    categorie_juridique text,
    typologie text,
    est_public boolean,
    nb_parcelles bigint,
    surface_m2 double precision,
    surface_mediane_parcelle_m2 double precision,
    nb_communes bigint,
    nb_departements bigint,
    nb_parcelles_geom_suspecte bigint,
    centre_patrimoine public.geometry,
    rayon_patrimoine_km double precision,
    surface_ha double precision,
    nb_ilots bigint,
    plus_grand_ilot_m2 double precision,
    nb_parcelles_isolees bigint,
    nb_ilots_5ha_plus bigint,
    surface_ilots_5ha_plus_m2 double precision,
    part_plus_grand_ilot double precision,
    nb_ilots_fonc bigint,
    plus_grand_ilot_fonc_m2 double precision,
    nb_parcelles_copro_pm bigint,
    commune_principale text,
    part_commune_principale double precision,
    rang_dans_commune_principale bigint,
    nb_communes_ou_1er_pm bigint,
    nb_mc_distinctes integer,
    nb_parcelles_deja_en_mc integer,
    surface_deja_en_mc_m2 double precision,
    est_acteur_public boolean,
    est_grand_industriel boolean,
    computed_at timestamp with time zone,
    maitre_ouvrage_probable boolean,
    deja_hote_mc boolean,
    plus_grand_ilot_superieur_a_demi_hectare boolean,
    lift_propension numeric,
    niveau_propension text
);


--
-- Name: COLUMN pm_proprietaire.maitre_ouvrage_probable; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_proprietaire.maitre_ouvrage_probable IS 'Opérateur d''infrastructure (SNCF, autoroutes, EDF, RTE, ports…) ou métropole / communauté urbaine, repéré au nom ou au code juridique. Ses mesures compensatoires concernent vraisemblablement ses propres projets.';


--
-- Name: COLUMN pm_proprietaire.deja_hote_mc; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_proprietaire.deja_hote_mc IS 'Au moins une parcelle du propriétaire porte déjà une mesure compensatoire (GEOMCE).';


--
-- Name: COLUMN pm_proprietaire.plus_grand_ilot_superieur_a_demi_hectare; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_proprietaire.plus_grand_ilot_superieur_a_demi_hectare IS 'Le plus grand îlot de parcelles contiguës du propriétaire dépasse 0,5 ha. Aucun hôte de mesure compensatoire observé en dessous de 0,3 ha.';


--
-- Name: COLUMN pm_proprietaire.lift_propension; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_proprietaire.lift_propension IS 'Lift de la typologie du propriétaire (voir pm_propension_typologie). Mesure une tendance de la catégorie, pas du propriétaire individuellement.';


--
-- Name: COLUMN pm_proprietaire.niveau_propension; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.pm_proprietaire.niveau_propension IS 'tres_favorable (lift >= 4), favorable (>= 1,5), neutre (>= 0,67), defavorable (< 0,67), non_determine (< 50 propriétaires dans la typologie).';


--
-- Name: pm_proprietaire_propension; Type: VIEW; Schema: ecocompensation; Owner: -
--

CREATE VIEW ecocompensation.pm_proprietaire_propension AS
 SELECT p.owner_key,
    p.siren,
    p.denomination,
    p.typologie,
    p.lift_propension,
    p.niveau_propension,
    p.deja_hote_mc,
    p.nb_mc_distinctes,
    p.maitre_ouvrage_probable,
    p.plus_grand_ilot_superieur_a_demi_hectare,
    concat_ws(' '::text, COALESCE(r.justification, format('Typologie « %s » : aucun propriétaire hors maîtres d''ouvrage, propension non calculable.'::text, p.typologie)),
        CASE
            WHEN p.deja_hote_mc THEN format('Ce propriétaire accueille déjà %s mesure(s) compensatoire(s).'::text, p.nb_mc_distinctes)
            ELSE NULL::text
        END,
        CASE
            WHEN p.maitre_ouvrage_probable THEN 'Attention : maître d''ouvrage probable (opérateur d''infrastructure ou grande intercommunalité) : ses mesures compensatoires portent vraisemblablement sur ses propres projets ; foncier a priori peu mobilisable pour un tiers.'::text
            ELSE NULL::text
        END,
        CASE
            WHEN (NOT p.plus_grand_ilot_superieur_a_demi_hectare) THEN 'Plus grand îlot inférieur à 0,5 ha : patrimoine a priori trop morcelé ou trop petit pour une mesure compensatoire (aucun hôte observé sous 0,3 ha).'::text
            ELSE NULL::text
        END) AS justification_propension
   FROM (ecocompensation.pm_proprietaire p
     LEFT JOIN ecocompensation.pm_propension_typologie r ON ((r.typologie = p.typologie)));


--
-- Name: VIEW pm_proprietaire_propension; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON VIEW ecocompensation.pm_proprietaire_propension IS 'Propension par propriétaire avec justification composée à la lecture (non stockée). Usage : WHERE siren = ''...''.';


--
-- Name: pm_prospectables_raffinee_cesbio; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.pm_prospectables_raffinee_cesbio (
    siren text,
    denomination text,
    nb_mc_distinctes integer,
    nb_parcelles_deja_en_mc integer,
    surface_deja_en_mc_m2 double precision,
    est_acteur_public boolean,
    est_grand_industriel boolean,
    idu text,
    code_insee text,
    code_commune text,
    nom_commune text,
    section text,
    numero text,
    prefixe text,
    contenance double precision,
    suf text,
    nature_culture text,
    contenance_nature_culture text,
    geom_2154 public.geometry(Geometry,2154),
    parcelle_deja_en_mc boolean,
    computed_at timestamp with time zone,
    exclusion_reason text,
    cesbio_pct_exclu numeric,
    cesbio_labels text,
    exclu_cesbio boolean
);


--
-- Name: preemption_espaces_naturels_sensibles; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.preemption_espaces_naturels_sensibles (
    id_com text,
    code_com text,
    parcelle text,
    section text,
    idu text,
    id_par text,
    supf double precision,
    texte text,
    nom_zpens text,
    commune text,
    date_creat text,
    sup_calcul double precision,
    shape_leng double precision,
    shape_area double precision,
    geom_2154 public.geometry(Geometry,2154)
);


--
-- Name: project_parcelles; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.project_parcelles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    code_insee text NOT NULL,
    section text NOT NULL,
    numero text NOT NULL,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: projects; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    aoi_id uuid,
    status text DEFAULT 'created'::text NOT NULL,
    layers_status jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_filter jsonb,
    last_results jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    foncier_id uuid,
    last_filter_uf jsonb,
    last_results_uf jsonb,
    filter_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    study_type text DEFAULT 'faune_buffer'::text NOT NULL,
    last_results_ilots_rpg jsonb,
    CONSTRAINT projects_study_type_check CHECK ((study_type = ANY (ARRAY['recherche'::text, 'analyse'::text, 'faune_buffer'::text, 'zones_humides_intra'::text, 'recherche_de_foncier'::text, 'analyse_de_foncier'::text])))
);


--
-- Name: COLUMN projects.study_type; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON COLUMN ecocompensation.projects.study_type IS 'Méthodologie : faune_buffer = recherche dans un buffer autour du foncier ; zones_humides_intra = recherche à l''intérieur du foncier uploadé.';


--
-- Name: projets_environnementaux; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.projets_environnementaux (
    id bigint NOT NULL,
    recordsid text,
    titre text,
    descriptif text,
    statut text,
    themes text,
    categories text,
    types text,
    dc_location text,
    source text,
    date_api text,
    date_issued text,
    date_ouverture text,
    date_fermeture text,
    url_etude_impact text,
    url_avis_ae text,
    url_biodiv text,
    attributs_source jsonb DEFAULT '{}'::jsonb NOT NULL,
    content_hash text,
    geom_2154 public.geometry(Geometry,2154),
    geom_3857 public.geometry(Geometry,3857),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: projets_environnementaux_etl_log; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.projets_environnementaux_etl_log (
    id bigint NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    n_source integer NOT NULL,
    n_inserted integer NOT NULL,
    n_updated integer NOT NULL,
    n_deleted integer NOT NULL,
    n_unchanged integer NOT NULL,
    note text
);


--
-- Name: projets_environnementaux_etl_log_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.projets_environnementaux_etl_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: projets_environnementaux_etl_log_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.projets_environnementaux_etl_log_id_seq OWNED BY ecocompensation.projets_environnementaux_etl_log.id;


--
-- Name: projets_environnementaux_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.projets_environnementaux_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: projets_environnementaux_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.projets_environnementaux_id_seq OWNED BY ecocompensation.projets_environnementaux.id;


--
-- Name: remontee_de_nappes; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.remontee_de_nappes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_index bigint,
    classe text,
    fiab_mnt text,
    fiab_eso text,
    fiab_tot text,
    classefiab text,
    gridcode bigint,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: reserve_fonciere; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.reserve_fonciere (
    id bigint NOT NULL,
    idu text,
    nomcommune text,
    codeinsee text,
    surface_cadastrale double precision,
    source_type text NOT NULL,
    statut_prospection text DEFAULT 'A_ETUDIER'::text NOT NULL,
    contact_nom text,
    contact_email text,
    description_site text,
    attributs_source jsonb DEFAULT '{}'::jsonb NOT NULL,
    geom_2154 public.geometry(MultiPolygon,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    ilot_id bigint,
    section text,
    numero text,
    prefixe text DEFAULT '000'::text,
    millesime_cadastre text,
    created_by uuid DEFAULT auth.uid(),
    archived_at timestamp with time zone,
    geom_3857 public.geometry(MultiPolygon,3857) GENERATED ALWAYS AS (public.st_transform(geom_2154, 3857)) STORED,
    surface_sig double precision GENERATED ALWAYS AS (public.st_area(geom_2154)) STORED,
    CONSTRAINT reserve_fonciere_geom_valid_chk CHECK (public.st_isvalid(geom_2154)),
    CONSTRAINT reserve_fonciere_source_type_chk CHECK ((length(btrim(source_type)) > 0)),
    CONSTRAINT reserve_fonciere_statut_chk CHECK ((statut_prospection = ANY (ARRAY['A_ETUDIER'::text, 'CONTACTE'::text, 'EN_NEGOCIATION'::text, 'VALIDE'::text, 'REJETE'::text])))
);


--
-- Name: reserve_fonciere_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.reserve_fonciere_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reserve_fonciere_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.reserve_fonciere_id_seq OWNED BY ecocompensation.reserve_fonciere.id;


--
-- Name: rf_contact; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rf_contact (
    id bigint NOT NULL,
    nom text,
    organisme text,
    role text,
    telephone text,
    email text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid DEFAULT auth.uid(),
    CONSTRAINT rf_contact_identifiable_chk CHECK (((COALESCE(btrim(nom), ''::text) <> ''::text) OR (COALESCE(btrim(organisme), ''::text) <> ''::text)))
);


--
-- Name: rf_contact_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.rf_contact_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rf_contact_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.rf_contact_id_seq OWNED BY ecocompensation.rf_contact.id;


--
-- Name: rf_ilot; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rf_ilot (
    id bigint NOT NULL,
    nom text NOT NULL,
    contact_id bigint,
    source_type text NOT NULL,
    source_ref text,
    date_reception date,
    statut_prospection text DEFAULT 'A_ETUDIER'::text NOT NULL,
    surface_annoncee_ha numeric,
    duree_blocage_ans integer,
    contraintes text,
    description text,
    attributs jsonb DEFAULT '{}'::jsonb NOT NULL,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid DEFAULT auth.uid(),
    CONSTRAINT rf_ilot_duree_blocage_ans_check CHECK (((duree_blocage_ans IS NULL) OR (duree_blocage_ans >= 0))),
    CONSTRAINT rf_ilot_nom_check CHECK ((length(btrim(nom)) > 0)),
    CONSTRAINT rf_ilot_source_type_check CHECK ((source_type = ANY (ARRAY['RECHERCHE_IGN'::text, 'IMPORT_FICHIER'::text, 'ENVOI_ECOCOMP'::text]))),
    CONSTRAINT rf_ilot_statut_prospection_check CHECK ((statut_prospection = ANY (ARRAY['A_ETUDIER'::text, 'CONTACTE'::text, 'EN_NEGOCIATION'::text, 'VALIDE'::text, 'REJETE'::text]))),
    CONSTRAINT rf_ilot_surface_annoncee_ha_check CHECK (((surface_annoncee_ha IS NULL) OR (surface_annoncee_ha > (0)::numeric)))
);


--
-- Name: rf_ilot_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.rf_ilot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rf_ilot_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.rf_ilot_id_seq OWNED BY ecocompensation.rf_ilot.id;


--
-- Name: rpg2025_ilot_membre; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_ilot_membre (
    num_ilot text NOT NULL,
    idu text NOT NULL,
    code_dep text NOT NULL,
    code_insee text,
    surf_inter_m2 double precision,
    part_parcelle double precision,
    part_ilot double precision
);


--
-- Name: rpg2025_ilots; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_ilots (
    code_dep text,
    num_ilot text,
    geom public.geometry(MultiPolygon,2154),
    nb_parcelles bigint,
    surf_parcelles_ha double precision,
    part_pp double precision,
    part_jachere double precision,
    part_vigne double precision,
    part_non_cultivee double precision,
    cat_dominante text,
    code_group_dominant bigint,
    code_cultu_dominant text,
    surf_ha double precision,
    nb_parcelles_cad integer,
    surf_cad_m2 double precision,
    part_ilot_couverte double precision,
    nb_pm integer,
    part_surf_pm double precision,
    pm_dominant text,
    part_pm_dominant double precision,
    pm_unique boolean
);


--
-- Name: rpg2025_parcelles; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_parcelles (
    code_dep text,
    id_parcel text,
    surf_parc double precision,
    cat_cult_p text,
    code_cultu text,
    code_group bigint,
    "precision" text,
    gcp boolean,
    pp boolean,
    bio boolean,
    iae boolean,
    geom public.geometry(MultiPolygon,2154),
    non_cultivee boolean,
    jachere boolean,
    vigne boolean,
    prairie_permanente boolean,
    surf_geom_ha double precision,
    num_ilot text
);


--
-- Name: rpg2025_sna; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_sna (
    code_dep text,
    couche text,
    numero_sna text,
    categorie text,
    type text,
    camp_deb bigint,
    camp_fin bigint,
    surface double precision,
    longueur double precision,
    largeur double precision,
    nb_arbres bigint,
    mur_tradi text,
    geom public.geometry(MultiPolygon,2154),
    surface_m2 double precision,
    disparu boolean
);


--
-- Name: rpg2025_sna_arbres; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_sna_arbres (
    code_dep text,
    couche text,
    numero_sna text,
    categorie text,
    type text,
    camp_deb bigint,
    camp_fin bigint,
    surface double precision,
    longueur double precision,
    largeur double precision,
    nb_arbres double precision,
    mur_tradi text,
    geom public.geometry(Point,2154),
    surface_m2 double precision,
    disparu boolean
);


--
-- Name: rpg2025_zdh; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.rpg2025_zdh (
    code_dep text,
    id_zdh text,
    prorata text,
    geom public.geometry(MultiPolygon,2154),
    prorata_ordre bigint,
    etat_milieu text,
    part_sous_parcelles double precision,
    hors_parcelle_millesime boolean,
    surf_ha double precision
);


--
-- Name: sitadel_autorisation; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_autorisation (
    dataset text NOT NULL,
    num_autorisation text NOT NULL,
    code_dep text NOT NULL,
    code_insee text NOT NULL,
    type_dau text,
    n_refs integer NOT NULL,
    n_refs_trouvees integer NOT NULL,
    geoloc_statut text NOT NULL,
    motif text NOT NULL,
    pt public.geometry(Point,2154)
);
ALTER TABLE ONLY ecocompensation.sitadel_autorisation ALTER COLUMN code_dep SET STATISTICS 1000;


--
-- Name: sitadel_emprise; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_emprise (
    dataset text NOT NULL,
    num_autorisation text NOT NULL,
    dep text,
    geom public.geometry(MultiPolygon,2154),
    geoloc_statut text,
    n_refs_declarees integer,
    n_refs_trouvees integer,
    sources text
);


--
-- Name: sitadel_lien_parcelle; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_lien_parcelle (
    dataset text,
    num_autorisation text,
    dep text,
    rang smallint,
    idu text,
    methode text,
    millesime date,
    idu_origine text,
    recouvrement real,
    n_candidats integer
);


--
-- Name: sitadel_locaux; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_locaux (
    num_autorisation text,
    reg_code text,
    reg_libelle text,
    dep_code text,
    dep_libelle text,
    comm text,
    type_dau text,
    num_dau text,
    etat_dau text,
    date_reelle_autorisation date,
    date_reelle_doc date,
    date_reelle_daact date,
    an_depot integer,
    dr_depot date,
    dpc_aut text,
    dpc_doc text,
    dpc_dern text,
    ape_dem text,
    cj_dem text,
    denom_dem text,
    siren_dem text,
    siret_dem text,
    codpost_dem text,
    localite_dem text,
    rec_archi text,
    adr_num_ter text,
    adr_libvoie_ter text,
    adr_lieudit_ter text,
    adr_localite_ter text,
    adr_codpost_ter text,
    sec_cadastre1 text,
    num_cadastre1 text,
    sec_cadastre2 text,
    num_cadastre2 text,
    sec_cadastre3 text,
    num_cadastre3 text,
    superficie_terrain numeric,
    zone_op text,
    nature_projet_declaree text,
    nature_projet_completee text,
    destination_principale text,
    type_transfo_principal text,
    type_princip_locaux_transformes text,
    i_extension text,
    i_surelevation text,
    i_nivsupp text,
    nb_niv_max numeric,
    nb_chambres numeric,
    surf_agr_avant numeric,
    surf_agr_creee numeric,
    surf_agr_issue_transfo numeric,
    surf_agr_demolie numeric,
    surf_agr_transformee numeric,
    surf_hab_avant numeric,
    surf_hab_creee numeric,
    surf_hab_issue_transfo numeric,
    surf_hab_demolie numeric,
    surf_hab_transformee numeric,
    surf_com_avant numeric,
    surf_com_creee numeric,
    surf_com_issue_transfo numeric,
    surf_com_demolie numeric,
    surf_com_transformee numeric,
    surf_pub_avant numeric,
    surf_pub_creee numeric,
    surf_pub_issue_transfo numeric,
    surf_pub_demolie numeric,
    surf_pub_transformee numeric,
    surf_loc_avant numeric,
    surf_loc_creee numeric,
    surf_loc_issue_transfo numeric,
    surf_loc_demolie numeric,
    surf_loc_transformee numeric,
    type_service_public text,
    surf_heb_avant numeric,
    surf_heb_creee numeric,
    surf_heb_issue_transfo numeric,
    surf_heb_demolie numeric,
    surf_heb_transformee numeric,
    surf_bur_avant numeric,
    surf_bur_creee numeric,
    surf_bur_issue_transfo numeric,
    surf_bur_demolie numeric,
    surf_bur_transformee numeric,
    surf_ind_avant numeric,
    surf_ind_creee numeric,
    surf_ind_issue_transfo numeric,
    surf_ind_demolie numeric,
    surf_ind_transformee numeric,
    surf_ent_avant numeric,
    surf_ent_creee numeric,
    surf_ent_issue_transfo numeric,
    surf_ent_demolie numeric,
    surf_ent_transformee numeric,
    geom public.geometry(MultiPolygon,2154),
    geoloc_statut text
);


--
-- Name: TABLE sitadel_locaux; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.sitadel_locaux IS 'Sitadel SDES — locaux — Nouvelle-Aquitaine — chargé le 2026-09-30';


--
-- Name: sitadel_logements; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_logements (
    num_autorisation text,
    reg_code text,
    reg_libelle text,
    dep_code text,
    dep_libelle text,
    comm text,
    type_dau text,
    num_dau text,
    etat_dau text,
    date_reelle_autorisation date,
    date_reelle_doc date,
    date_reelle_daact date,
    an_depot integer,
    dr_depot date,
    dpc_aut text,
    dpc_doc text,
    dpc_dern text,
    ape_dem text,
    cj_dem text,
    denom_dem text,
    siren_dem text,
    siret_dem text,
    codpost_dem text,
    localite_dem text,
    rec_archi text,
    adr_num_ter text,
    adr_libvoie_ter text,
    adr_lieudit_ter text,
    adr_localite_ter text,
    adr_codpost_ter text,
    sec_cadastre1 text,
    num_cadastre1 text,
    sec_cadastre2 text,
    num_cadastre2 text,
    sec_cadastre3 text,
    num_cadastre3 text,
    superficie_terrain numeric,
    zone_op text,
    nature_projet_declaree text,
    nature_projet_completee text,
    destination_principale text,
    type_princip_logts_crees text,
    type_transfo_principal text,
    type_princip_locaux_transformes text,
    i_extension text,
    i_surelevation text,
    i_nivsupp text,
    nb_niv_max numeric,
    utilisation text,
    res_princip_ou_second text,
    typ_annexe text,
    i_piscine text,
    i_garage text,
    i_veranda text,
    i_abri_jardin text,
    i_autre_annexe text,
    residence text,
    res_pers_agees text,
    res_etudiants text,
    res_tourisme text,
    res_hotel_sociale text,
    res_sociale text,
    res_handicapes text,
    res_autre text,
    nb_lgt_tot_crees numeric,
    nb_lgt_ind_crees numeric,
    nb_lgt_col_crees numeric,
    nb_lgt_indiv_purs numeric,
    nb_lgt_indiv_groupes numeric,
    nb_lgt_res numeric,
    nb_lgt_col_hors_res numeric,
    nb_lgt_demolis numeric,
    nb_lgt_1p numeric,
    nb_lgt_2p numeric,
    nb_lgt_3p numeric,
    nb_lgt_4p numeric,
    nb_lgt_5p numeric,
    nb_lgt_6p_plus numeric,
    nb_lgt_pret_loc_social numeric,
    nb_lgt_acc_soc_hors_ptz numeric,
    nb_lgt_ptz numeric,
    surf_hab_avant numeric,
    surf_hab_creee numeric,
    surf_hab_issue_transfo numeric,
    surf_hab_demolie numeric,
    surf_hab_transformee numeric,
    surf_loc_avant numeric,
    surf_loc_creee numeric,
    surf_loc_issue_transfo numeric,
    surf_loc_demolie numeric,
    surf_loc_transformee numeric,
    surf_autr_transformee numeric,
    surf_com_transformee numeric,
    surf_agr_transformee numeric,
    surf_pub_transformee numeric,
    geom public.geometry(MultiPolygon,2154),
    geoloc_statut text
);


--
-- Name: TABLE sitadel_logements; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.sitadel_logements IS 'Sitadel SDES — logements — Nouvelle-Aquitaine — chargé le 2026-09-30';


--
-- Name: sitadel_pa; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_pa (
    num_autorisation text,
    reg_code text,
    reg_libelle text,
    dep_code text,
    dep_libelle text,
    comm text,
    num_pa text,
    etat_pa text,
    date_reelle_autorisation date,
    date_reelle_doc date,
    date_reelle_daact date,
    an_depot integer,
    dr_depot date,
    dpc_aut text,
    dpc_doc text,
    dpc_dern text,
    ape_dem text,
    cj_dem text,
    denom_dem text,
    siren_dem text,
    siret_dem text,
    codpost_dem text,
    localite_dem text,
    rec_archi text,
    adr_num_ter text,
    adr_libvoie_ter text,
    adr_lieudit_ter text,
    adr_localite_ter text,
    adr_codpost_ter text,
    sec_cadastre1 text,
    num_cadastre1 text,
    sec_cadastre2 text,
    num_cadastre2 text,
    sec_cadastre3 text,
    num_cadastre3 text,
    superficie_terrain numeric,
    zone_op text,
    geom public.geometry(MultiPolygon,2154),
    geoloc_statut text
);


--
-- Name: TABLE sitadel_pa; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.sitadel_pa IS 'Sitadel SDES — pa — Nouvelle-Aquitaine — chargé le 2026-09-30';


--
-- Name: sitadel_parcelle; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_parcelle (
    dataset text NOT NULL,
    num_autorisation text NOT NULL,
    code_dep text NOT NULL,
    rang smallint NOT NULL,
    idu text NOT NULL,
    code_insee text NOT NULL,
    section text NOT NULL,
    numero text NOT NULL,
    n_idu integer NOT NULL,
    methode text DEFAULT 'direct'::text NOT NULL
);
ALTER TABLE ONLY ecocompensation.sitadel_parcelle ALTER COLUMN code_dep SET STATISTICS 1000;


--
-- Name: sitadel_parcelle_origine; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_parcelle_origine (
    dataset text,
    num_autorisation text,
    dep text,
    rang smallint,
    millesime date,
    idu_origine text,
    geom public.geometry(MultiPolygon,2154)
);


--
-- Name: sitadel_pd; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_pd (
    num_autorisation text,
    reg_code text,
    reg_libelle text,
    dep_code text,
    dep_libelle text,
    comm text,
    num_pd text,
    etat_pd text,
    date_reelle_autorisation date,
    an_depot integer,
    dr_depot date,
    dpc_aut text,
    dpc_dern text,
    ape_dem text,
    cj_dem text,
    denom_dem text,
    siren_dem text,
    siret_dem text,
    codpost_dem text,
    localite_dem text,
    rec_archi text,
    adr_num_ter text,
    adr_libvoie_ter text,
    adr_lieudit_ter text,
    adr_localite_ter text,
    adr_codpost_ter text,
    sec_cadastre1 text,
    num_cadastre1 text,
    sec_cadastre2 text,
    num_cadastre2 text,
    sec_cadastre3 text,
    num_cadastre3 text,
    superficie_terrain numeric,
    zone_op text,
    geom public.geometry(MultiPolygon,2154),
    geoloc_statut text
);


--
-- Name: TABLE sitadel_pd; Type: COMMENT; Schema: ecocompensation; Owner: -
--

COMMENT ON TABLE ecocompensation.sitadel_pd IS 'Sitadel SDES — pd — Nouvelle-Aquitaine — chargé le 2026-09-30';


--
-- Name: sitadel_ref_parcelle; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_ref_parcelle (
    dataset text,
    num_autorisation text,
    dep text,
    comm text,
    rang smallint,
    sec_n text,
    num_n text,
    date_ref date
);


--
-- Name: sitadel_test_pc33; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.sitadel_test_pc33 (
    num_autorisation text,
    comm text,
    rang integer,
    idu text,
    geom public.geometry(MultiPolygon,2154)
);


--
-- Name: surfaces_hydros; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.surfaces_hydros (
    cleabs text NOT NULL,
    code_hydrographique text,
    nature text,
    position_par_rapport_au_sol text,
    persistance text,
    salinite boolean,
    origine text,
    statut text,
    commentaire_sur_l_objet_hydro text,
    nom text,
    geom_2154 public.geometry(MultiPolygon,2154),
    geom_3857 public.geometry(MultiPolygon,3857),
    _updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: tmp_bdtopo_sub; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.tmp_bdtopo_sub (
    id text,
    nature text,
    geom_2154 public.geometry
);


--
-- Name: tmp_cesbio_sub; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.tmp_cesbio_sub (
    departement character(2),
    libelle_classe text,
    geom public.geometry
);


--
-- Name: troncons_hydros; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.troncons_hydros (
    cleabs text NOT NULL,
    code_hydrographique text,
    nature text,
    persistance text,
    fosse boolean,
    navigabilite boolean,
    salinite boolean,
    numero_d_ordre integer,
    origine text,
    sens_de_l_ecoulement text,
    classe_de_largeur text,
    type_de_bras text,
    nom text,
    geom_2154 public.geometry(MultiLineString,2154),
    geom_3857 public.geometry(MultiLineString,3857),
    _updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: v_ebc; Type: VIEW; Schema: ecocompensation; Owner: -
--

CREATE VIEW ecocompensation.v_ebc AS
 SELECT cle,
    gid,
    gpu_doc_id,
    gpu_status,
    gpu_timestamp,
    partition,
    libelle,
    txt,
    typepsc,
    stypepsc,
    nomfic,
    urlfic,
    insee,
    datappro,
    datvalid,
    idurba,
    nature,
    attr_hash,
    geom_2154,
    ingested_at,
    updated_at,
    classe,
    est_ebc
   FROM ecocompensation.espaces_boises_classes
  WHERE est_ebc;


--
-- Name: v_flore_taxons; Type: VIEW; Schema: ecocompensation; Owner: -
--

CREATE VIEW ecocompensation.v_flore_taxons AS
 SELECT taxon_key,
    mode() WITHIN GROUP (ORDER BY nom_taxon_cite) AS nom_scientifique,
    mode() WITHIN GROUP (ORDER BY nom_taxon_v) AS nom_vernaculaire,
    max(cd_nom_valide) AS cd_nom_valide,
    count(*) AS nb_observations,
    max(date_observation) AS derniere_observation
   FROM ecocompensation.flore_observation
  GROUP BY taxon_key;


--
-- Name: v_reserve_fonciere; Type: VIEW; Schema: ecocompensation; Owner: -
--

CREATE VIEW ecocompensation.v_reserve_fonciere AS
 SELECT p.id,
    p.ilot_id,
    p.idu,
    p.section,
    p.numero,
    p.nomcommune,
    p.codeinsee,
    p.surface_cadastrale,
    p.surface_sig,
    COALESCE(i.source_type, p.source_type) AS source_type,
    COALESCE(i.statut_prospection, p.statut_prospection) AS statut_prospection,
    i.nom AS ilot_nom,
    i.surface_annoncee_ha,
    i.duree_blocage_ans,
    i.contraintes,
    COALESCE(i.description, p.description_site) AS description_site,
    c.id AS contact_id,
    COALESCE(c.nom, p.contact_nom) AS contact_nom,
    c.organisme AS contact_organisme,
    c.telephone AS contact_telephone,
    COALESCE(c.email, p.contact_email) AS contact_email,
    p.attributs_source,
    p.geom_2154,
    p.geom_3857,
    p.created_at,
    p.updated_at
   FROM ((ecocompensation.reserve_fonciere p
     LEFT JOIN ecocompensation.rf_ilot i ON (((i.id = p.ilot_id) AND (i.archived_at IS NULL))))
     LEFT JOIN ecocompensation.rf_contact c ON ((c.id = i.contact_id)))
  WHERE (p.archived_at IS NULL);


--
-- Name: vegetation_sur_cesbio; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.vegetation_sur_cesbio (
    id bigint NOT NULL,
    source text NOT NULL,
    nature text,
    libelle text,
    libelle_prio text GENERATED ALWAYS AS (COALESCE(nature, libelle)) STORED,
    tile_idx integer NOT NULL,
    geom public.geometry(Polygon,2154) NOT NULL
);


--
-- Name: vegetation_sur_cesbio_old; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.vegetation_sur_cesbio_old (
    id bigint NOT NULL,
    source text NOT NULL,
    nature text,
    libelle text,
    geom public.geometry(MultiPolygon,2154),
    libelle_prio text GENERATED ALWAYS AS (COALESCE(nature, libelle)) STORED
);


--
-- Name: vegetation_sur_cesbio_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.vegetation_sur_cesbio_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: vegetation_sur_cesbio_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.vegetation_sur_cesbio_id_seq OWNED BY ecocompensation.vegetation_sur_cesbio_old.id;


--
-- Name: vegetation_sur_cesbio_sub; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.vegetation_sur_cesbio_sub (
    libelle_prio text,
    geom public.geometry
);


--
-- Name: vegetation_sur_cesbio_v2_id_seq; Type: SEQUENCE; Schema: ecocompensation; Owner: -
--

CREATE SEQUENCE ecocompensation.vegetation_sur_cesbio_v2_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: vegetation_sur_cesbio_v2_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation; Owner: -
--

ALTER SEQUENCE ecocompensation.vegetation_sur_cesbio_v2_id_seq OWNED BY ecocompensation.vegetation_sur_cesbio.id;


--
-- Name: znieff; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.znieff (
    cle text NOT NULL,
    type_znieff text NOT NULL,
    id_mnhn text,
    id_local text,
    nom_site text,
    date_crea text,
    modif_adm text,
    modif_geo text,
    url_fiche text,
    surf_off double precision,
    acte_deb text,
    acte_fin text,
    gest_site text,
    operateur text,
    "precision" text,
    src_geom text,
    src_annee text,
    marin text,
    p1_nature text,
    p4_geologi text,
    area_sig double precision,
    cd_sig text,
    territoire text,
    attr_hash text NOT NULL,
    geom_2154 public.geometry(MultiPolygon,2154) NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: zone_humide; Type: TABLE; Schema: ecocompensation; Owner: -
--

CREATE TABLE ecocompensation.zone_humide (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text NOT NULL,
    inventaire_id text,
    libelle text,
    inv_nom text,
    geom_2154 public.geometry NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: arrachage_vignes; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.arrachage_vignes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    c_sup_arra double precision,
    c_div_rena text,
    c_div_re_1 text,
    geom_2154 public.geometry(Polygon,2154),
    project_id uuid
);


--
-- Name: bd_topo_et_cesbio; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.bd_topo_et_cesbio (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    source text,
    nature text,
    libelle text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    libelle_prio text
);


--
-- Name: carhab; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.carhab (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    id_polygone_carhab double precision,
    code_hab_carhab text,
    code_biotope text,
    code_physio text,
    code_eunis text,
    nom_eunis text,
    rang double precision,
    commentaire text,
    surface double precision,
    code_niv2 text,
    cd_hab_eunis double precision,
    id_sinp_evenement text,
    id_sinp_habitat text,
    conformite_biotope_departementale text,
    commentaire_conformite_biotope_departementale text,
    conformite_physio_departementale text,
    commentaire_conformite_physio_departementale text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: cesbio; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.cesbio (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    classe smallint,
    libelle_classe text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: cosia; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.cosia (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    classe text,
    departement text NOT NULL,
    dpt text NOT NULL,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    tile_name text
);


--
-- Name: ebc; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.ebc (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    libelle text,
    insee text,
    nature text,
    typepsc text,
    stypepsc text,
    gpu_doc_id text,
    gpu_status text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: espaces_naturels_sensibles_ens; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.espaces_naturels_sensibles_ens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    id_com text,
    code_com text,
    parcelle text,
    section text,
    pre text,
    idu text,
    id_par text,
    supf double precision,
    texte text,
    nom_site text,
    commune text,
    date_acq text,
    refcad text,
    sup_calcul double precision,
    shape_leng double precision,
    shape_area double precision,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: fauna; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.fauna (
    id_releve text,
    id_obs text,
    date_debut date,
    date_fin date,
    classe text,
    ordre text,
    famille text,
    cd_ref bigint,
    nom_cite text,
    nom_taxref text,
    nom_vernaculaire text,
    geom_id bigint,
    geom_type text,
    geom_2154 public.geometry(Geometry,2154),
    annee_obs integer,
    lon double precision,
    lat double precision,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    niveau_patrimonialite text,
    protection_nationale character varying(50)
);


--
-- Name: fragmentation_polygons; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.fragmentation_polygons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    rid integer NOT NULL,
    value integer,
    geom public.geometry(Polygon,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: frayeres; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.frayeres (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    topo_carth text,
    larg_plage text,
    espece text,
    liste text,
    densite_p text,
    protection text,
    activite text,
    pression text,
    geom_2154 public.geometry(Point,2154),
    project_id uuid
);


--
-- Name: ilots_rpg; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.ilots_rpg (
    project_id uuid NOT NULL,
    num_ilot text NOT NULL,
    millesime text,
    idus text[] NOT NULL,
    nb_parcelles integer,
    surface_ha double precision,
    miller double precision,
    dist_centre_m double precision,
    cat_dominante text,
    code_group_dominant integer,
    code_cultu_dominant text,
    part_pp double precision,
    part_jachere double precision,
    part_vigne double precision,
    nb_pm integer,
    pm_dominant text,
    pm_unique boolean,
    veg_libelles text[] DEFAULT '{}'::text[] NOT NULL,
    fauna_distances jsonb DEFAULT '{}'::jsonb NOT NULL,
    geom_2154 public.geometry(MultiPolygon,2154),
    created_at timestamp with time zone DEFAULT now(),
    veg_profile jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: mesures_compensatoire_lin; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.mesures_compensatoire_lin (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: mesures_compensatoire_pct; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.mesures_compensatoire_pct (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: mesures_compensatoire_surf; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.mesures_compensatoire_surf (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    categorie text,
    classe text,
    type text,
    sous_categorie text,
    type_procedure text,
    theme text,
    projet text,
    maitre_ouvrage text,
    origine_si text,
    dossier_no text,
    duree text,
    date_decision date,
    identifiant integer,
    l_dep text,
    liste_communes text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: mnt_dalles; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.mnt_dalles (
    aoi_id uuid NOT NULL,
    project_id uuid,
    filename text NOT NULL,
    storage_path text NOT NULL,
    size_bytes bigint,
    source_url text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: natura2000; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.natura2000 (
    rid uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    natura_categorie text,
    id_local text,
    nom_site text,
    date_crea date,
    modif_adm date,
    modif_geo date,
    url_fiche text,
    surf_off double precision,
    acte_deb text,
    acte_fin text,
    gest_site text,
    operateur text,
    precision_qual text,
    src_geom text,
    src_annee text,
    marin text,
    p1_nature text,
    p4_geologi text,
    id_mnhn text,
    area_sig double precision,
    cd_sig text,
    territoire text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: parcelles; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles (
    id text,
    gid integer,
    numero text,
    feuille integer,
    section text,
    code_dep text,
    nom_com text,
    code_com text,
    com_abs text,
    code_arr text,
    idu text,
    contenance double precision,
    code_insee text,
    geom_2154 public.geometry(MultiPolygon,2154),
    aoi_id text,
    project_id uuid,
    veg_libelles text[] DEFAULT '{}'::text[] NOT NULL,
    fauna_distances jsonb DEFAULT '{}'::jsonb NOT NULL,
    veg_details jsonb DEFAULT '{}'::jsonb NOT NULL,
    zone_humide_ha double precision DEFAULT 0 NOT NULL,
    dist_hydro_m double precision,
    troncons_hydro_info jsonb DEFAULT '[]'::jsonb NOT NULL,
    dist_surface_hydro_m double precision,
    surface_hydro_ha double precision DEFAULT 0 NOT NULL,
    surfaces_hydro_info jsonb DEFAULT '[]'::jsonb NOT NULL,
    flora_distances jsonb
);


--
-- Name: parcelles_pool; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles_pool (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    rank integer,
    surface_ha double precision,
    miller double precision,
    distance_km double precision,
    dist_hydro_m double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: parcelles_pool_indesirables; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles_pool_indesirables (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: parcelles_pool_metrics; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles_pool_metrics (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    idu text NOT NULL,
    metric_key text NOT NULL,
    metric_value_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: parcelles_pool_runs; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles_pool_runs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scope text DEFAULT 'parcelles'::text NOT NULL,
    options_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    total_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    profiling_progress jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_summary jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: parcelles_project_indesirables; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.parcelles_project_indesirables (
    project_id uuid NOT NULL,
    idu text NOT NULL,
    source_run_id uuid,
    rank integer,
    code_insee text,
    section text,
    numero text,
    surface_ha double precision,
    miller double precision,
    distance_km double precision,
    dist_hydro_m double precision,
    metrics_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patrimoine_naturel; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.patrimoine_naturel (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    uid text NOT NULL,
    type_patrimoine text,
    nom text,
    updated_at timestamp without time zone,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: preemption_espaces_naturels_sensibles; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.preemption_espaces_naturels_sensibles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    id_com text,
    code_com text,
    parcelle text,
    section text,
    idu text,
    id_par text,
    supf double precision,
    texte text,
    nom_zpens text,
    commune text,
    date_creat text,
    sup_calcul double precision,
    shape_leng double precision,
    shape_area double precision,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: remontee_de_nappes; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.remontee_de_nappes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    source_index bigint,
    classe text,
    fiab_mnt text,
    fiab_eso text,
    fiab_tot text,
    classefiab text,
    gridcode bigint,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: reserves_naturelles; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.reserves_naturelles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    categorie_reserve text NOT NULL,
    nom_site text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: routes; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.routes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    nature text,
    sens_de_circulation text,
    vitesse_moyenne_vl integer,
    cleabs text,
    nom_voie_ban_gauche text,
    geom_2154 public.geometry(LineString,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: TABLE routes; Type: COMMENT; Schema: ecocompensation_results; Owner: -
--

COMMENT ON TABLE ecocompensation_results.routes IS 'Tronçons de route BDTOPO intersectant l''AOI (source WFS data.geopf.fr).';


--
-- Name: sites_classes; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.sites_classes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    aoi_id uuid,
    nom_site text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: sous_ensembles; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.sous_ensembles (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    uf_id text NOT NULL,
    subset_id text NOT NULL,
    k integer NOT NULL,
    idus text[] NOT NULL,
    surface_ha double precision,
    geom_2154 public.geometry(Geometry,2154),
    miller double precision,
    dist_centre_m double precision,
    created_at timestamp with time zone DEFAULT now(),
    denomination text,
    siren text,
    veg_libelles text[] DEFAULT '{}'::text[] NOT NULL,
    fauna_distances jsonb DEFAULT '{}'::jsonb NOT NULL,
    veg_profile jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: COLUMN sous_ensembles.denomination; Type: COMMENT; Schema: ecocompensation_results; Owner: -
--

COMMENT ON COLUMN ecocompensation_results.sous_ensembles.denomination IS 'Raison sociale du propriétaire moral (PM)';


--
-- Name: COLUMN sous_ensembles.siren; Type: COMMENT; Schema: ecocompensation_results; Owner: -
--

COMMENT ON COLUMN ecocompensation_results.sous_ensembles.siren IS 'SIREN du propriétaire moral';


--
-- Name: sous_ensembles_id_seq; Type: SEQUENCE; Schema: ecocompensation_results; Owner: -
--

CREATE SEQUENCE ecocompensation_results.sous_ensembles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sous_ensembles_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation_results; Owner: -
--

ALTER SEQUENCE ecocompensation_results.sous_ensembles_id_seq OWNED BY ecocompensation_results.sous_ensembles.id;


--
-- Name: surfaces_hydros; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.surfaces_hydros (
    project_id uuid NOT NULL,
    cleabs text NOT NULL,
    code_hydrographique text,
    nature text,
    position_par_rapport_au_sol text,
    persistance text,
    salinite boolean,
    origine text,
    statut text,
    commentaire_sur_l_objet_hydro text,
    nom text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: troncons_hydros; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.troncons_hydros (
    project_id uuid NOT NULL,
    cleabs text NOT NULL,
    code_hydrographique text,
    nature text,
    persistance text,
    fosse boolean,
    navigabilite boolean,
    salinite boolean,
    numero_d_ordre integer,
    origine text,
    sens_de_l_ecoulement text,
    classe_de_largeur text,
    type_de_bras text,
    nom text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: unites_foncieres; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.unites_foncieres (
    id integer NOT NULL,
    project_id uuid NOT NULL,
    uf_id text NOT NULL,
    siren text NOT NULL,
    denomination text,
    forme_juridique text,
    nb_parcelles integer NOT NULL,
    surface_ha_uf double precision,
    idu text NOT NULL,
    surface_ha double precision,
    geom_2154 public.geometry(Geometry,2154),
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: unites_foncieres_id_seq; Type: SEQUENCE; Schema: ecocompensation_results; Owner: -
--

CREATE SEQUENCE ecocompensation_results.unites_foncieres_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: unites_foncieres_id_seq; Type: SEQUENCE OWNED BY; Schema: ecocompensation_results; Owner: -
--

ALTER SEQUENCE ecocompensation_results.unites_foncieres_id_seq OWNED BY ecocompensation_results.unites_foncieres.id;


--
-- Name: urba_cible_resultat; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.urba_cible_resultat (
    run_id uuid NOT NULL,
    cible_id text NOT NULL,
    cible_type text NOT NULL,
    synthese jsonb DEFAULT '{}'::jsonb NOT NULL,
    flags text[] DEFAULT '{}'::text[] NOT NULL,
    score jsonb
);


--
-- Name: urba_entite; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.urba_entite (
    run_id uuid NOT NULL,
    fid text NOT NULL,
    layer_key text NOT NULL,
    famille text NOT NULL,
    geom_kind text NOT NULL,
    display_name text,
    code text,
    libelle_cnig text,
    libelle text,
    attributs jsonb DEFAULT '{}'::jsonb NOT NULL,
    flags text[] DEFAULT '{}'::text[] NOT NULL,
    geom_2154 public.geometry(Geometry,2154)
);


--
-- Name: urba_parcelle_entite; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.urba_parcelle_entite (
    run_id uuid NOT NULL,
    cible_id text NOT NULL,
    cible_type text NOT NULL,
    layer_key text NOT NULL,
    fid text NOT NULL,
    bucket text NOT NULL,
    "position" text,
    surface_m2 double precision,
    pct_cible double precision,
    longueur_m double precision,
    nb_points integer,
    distance_m double precision
);


--
-- Name: urba_run; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.urba_run (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    layers text[] DEFAULT '{}'::text[] NOT NULL
);


--
-- Name: voies_ferrees; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.voies_ferrees (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    cleabs text,
    nature text,
    nombre_de_voies integer,
    geom_2154 public.geometry(LineString,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: TABLE voies_ferrees; Type: COMMENT; Schema: ecocompensation_results; Owner: -
--

COMMENT ON TABLE ecocompensation_results.voies_ferrees IS 'Tronçons de voie ferrée BDTOPO intersectant l''AOI (source WFS data.geopf.fr).';


--
-- Name: znieff; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.znieff (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid,
    znieff_type smallint NOT NULL,
    nom_site text,
    CONSTRAINT znieff_type_chk CHECK ((znieff_type = ANY (ARRAY[1, 2])))
);


--
-- Name: zone_de_vegetation; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.zone_de_vegetation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    id_src text,
    nature text,
    date_creat text,
    date_maj text,
    date_app date,
    date_conf date,
    acqu_plani text,
    prec_plani double precision,
    source text,
    id_source text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: zone_humide; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.zone_humide (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text,
    source text NOT NULL,
    inventaire_id text,
    libelle text,
    inv_nom text,
    geom_2154 public.geometry(Geometry,2154) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: zones_humides_probables; Type: TABLE; Schema: ecocompensation_results; Owner: -
--

CREATE TABLE ecocompensation_results.zones_humides_probables (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    aoi_id text NOT NULL,
    rid integer NOT NULL,
    value integer,
    geom public.geometry NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    project_id uuid
);


--
-- Name: canopy_analysis; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canopy_analysis (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    parcelle_id text NOT NULL,
    insee text,
    section text,
    numeros text[],
    bounds_l93 double precision[],
    area_m2 double precision,
    area_ha double precision,
    n_mnt_tiles integer,
    n_nuages_tiles integer,
    n_points_vegetation integer,
    n_hauteurs_valides integer,
    height_max_m double precision,
    height_p95_m double precision,
    height_p90_m double precision,
    height_median_m double precision,
    height_mean_m double precision,
    height_std_m double precision,
    n_trees integer,
    density_trees_ha double precision,
    tree_height_mean_m double precision,
    tree_height_max_m double precision,
    cover_gt_2m_pct double precision,
    cover_gt_5m_pct double precision,
    cover_gt_10m_pct double precision,
    cover_gt_15m_pct double precision,
    cover_gt_20m_pct double precision,
    canopy_closure_pct double precision,
    gap_fraction_pct double precision,
    shannon_diversity double precision,
    roughness_m double precision,
    strata_distribution jsonb,
    full_results jsonb,
    performance_metrics jsonb,
    vegetation_type text,
    vegetation_description text,
    maturity text,
    structure_type text,
    ecological_score double precision,
    ecological_interpretation text,
    summary_text text
);


--
-- Name: lidar_tiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lidar_tiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    tile_name text NOT NULL,
    layer_type text NOT NULL,
    ign_url text,
    supabase_url text,
    size_mb double precision,
    bounds_l93 double precision[],
    download_count integer DEFAULT 1,
    last_accessed timestamp with time zone DEFAULT now()
);


--
-- Name: parcelles_personnes_morales; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parcelles_personnes_morales (
    idu text,
    code_insee text,
    section text,
    numero text,
    siren text,
    denomination text,
    forme_juridique text,
    contenance double precision,
    geom_2154 public.geometry,
    code_direction text,
    nom_commune text,
    prefixe text,
    numero_voirie text,
    indice_repetition text,
    code_voie_majic text,
    code_voie_rivoli text,
    nature_voie text,
    nom_voie text,
    contenance_cadastrale text,
    suf text,
    nature_culture text,
    contenance_nature_culture text,
    code_droit_par text,
    numero_majic_par text,
    groupe_personne_par text,
    forme_juridique_abregee_par text,
    code_commune text,
    denomination_par text,
    forme_juridique_par text
);


--
-- Name: parcelles_personnes_morales_tmp; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parcelles_personnes_morales_tmp (
    idu text,
    siren text
);


--
-- Name: personnes_morales_tmp; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.personnes_morales_tmp (
    siren text,
    denomination text,
    forme_juridique text
);


--
-- Name: cadastre_parcelles_16; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_16 FOR VALUES IN ('16');


--
-- Name: cadastre_parcelles_17; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_17 FOR VALUES IN ('17');


--
-- Name: cadastre_parcelles_19; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_19 FOR VALUES IN ('19');


--
-- Name: cadastre_parcelles_23; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_23 FOR VALUES IN ('23');


--
-- Name: cadastre_parcelles_24; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_24 FOR VALUES IN ('24');


--
-- Name: cadastre_parcelles_33; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_33 FOR VALUES IN ('33');


--
-- Name: cadastre_parcelles_40; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_40 FOR VALUES IN ('40');


--
-- Name: cadastre_parcelles_47; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_47 FOR VALUES IN ('47');


--
-- Name: cadastre_parcelles_64; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_64 FOR VALUES IN ('64');


--
-- Name: cadastre_parcelles_79; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_79 FOR VALUES IN ('79');


--
-- Name: cadastre_parcelles_86; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_86 FOR VALUES IN ('86');


--
-- Name: cadastre_parcelles_87; Type: TABLE ATTACH; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles ATTACH PARTITION ecocompensation.cadastre_parcelles_87 FOR VALUES IN ('87');


--
-- Name: cesbio id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cesbio ALTER COLUMN id SET DEFAULT nextval('ecocompensation.cesbio_id_seq'::regclass);


--
-- Name: emprise_zpens id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.emprise_zpens ALTER COLUMN id SET DEFAULT nextval('ecocompensation.emprise_zpens_id_seq'::regclass);


--
-- Name: emprise_zpens_etl_log id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.emprise_zpens_etl_log ALTER COLUMN id SET DEFAULT nextval('ecocompensation.emprise_zpens_etl_log_id_seq'::regclass);


--
-- Name: faune id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.faune ALTER COLUMN id SET DEFAULT nextval('ecocompensation.faune_id_seq'::regclass);


--
-- Name: parcelles_syndicates id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.parcelles_syndicates ALTER COLUMN id SET DEFAULT nextval('ecocompensation.parcelles_syndicates_id_seq'::regclass);


--
-- Name: projets_environnementaux id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projets_environnementaux ALTER COLUMN id SET DEFAULT nextval('ecocompensation.projets_environnementaux_id_seq'::regclass);


--
-- Name: projets_environnementaux_etl_log id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projets_environnementaux_etl_log ALTER COLUMN id SET DEFAULT nextval('ecocompensation.projets_environnementaux_etl_log_id_seq'::regclass);


--
-- Name: reserve_fonciere id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.reserve_fonciere ALTER COLUMN id SET DEFAULT nextval('ecocompensation.reserve_fonciere_id_seq'::regclass);


--
-- Name: rf_contact id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rf_contact ALTER COLUMN id SET DEFAULT nextval('ecocompensation.rf_contact_id_seq'::regclass);


--
-- Name: rf_ilot id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rf_ilot ALTER COLUMN id SET DEFAULT nextval('ecocompensation.rf_ilot_id_seq'::regclass);


--
-- Name: vegetation_sur_cesbio id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.vegetation_sur_cesbio ALTER COLUMN id SET DEFAULT nextval('ecocompensation.vegetation_sur_cesbio_v2_id_seq'::regclass);


--
-- Name: vegetation_sur_cesbio_old id; Type: DEFAULT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.vegetation_sur_cesbio_old ALTER COLUMN id SET DEFAULT nextval('ecocompensation.vegetation_sur_cesbio_id_seq'::regclass);


--
-- Name: sous_ensembles id; Type: DEFAULT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.sous_ensembles ALTER COLUMN id SET DEFAULT nextval('ecocompensation_results.sous_ensembles_id_seq'::regclass);


--
-- Name: unites_foncieres id; Type: DEFAULT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.unites_foncieres ALTER COLUMN id SET DEFAULT nextval('ecocompensation_results.unites_foncieres_id_seq'::regclass);


--
-- Name: aoi aoi_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.aoi
    ADD CONSTRAINT aoi_pkey PRIMARY KEY (id);


--
-- Name: bv_spe_masse_d_eau bv_spe_masse_d_eau_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.bv_spe_masse_d_eau
    ADD CONSTRAINT bv_spe_masse_d_eau_pkey PRIMARY KEY (id);


--
-- Name: cadastre_parcelles cadastre_parcelles_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles
    ADD CONSTRAINT cadastre_parcelles_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_16 cadastre_parcelles_16_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_16
    ADD CONSTRAINT cadastre_parcelles_16_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_17 cadastre_parcelles_17_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_17
    ADD CONSTRAINT cadastre_parcelles_17_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_19 cadastre_parcelles_19_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_19
    ADD CONSTRAINT cadastre_parcelles_19_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_23 cadastre_parcelles_23_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_23
    ADD CONSTRAINT cadastre_parcelles_23_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_24 cadastre_parcelles_24_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_24
    ADD CONSTRAINT cadastre_parcelles_24_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_33 cadastre_parcelles_33_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_33
    ADD CONSTRAINT cadastre_parcelles_33_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_40 cadastre_parcelles_40_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_40
    ADD CONSTRAINT cadastre_parcelles_40_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_47 cadastre_parcelles_47_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_47
    ADD CONSTRAINT cadastre_parcelles_47_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_64 cadastre_parcelles_64_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_64
    ADD CONSTRAINT cadastre_parcelles_64_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_79 cadastre_parcelles_79_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_79
    ADD CONSTRAINT cadastre_parcelles_79_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_86 cadastre_parcelles_86_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_86
    ADD CONSTRAINT cadastre_parcelles_86_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_87 cadastre_parcelles_87_new_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_87
    ADD CONSTRAINT cadastre_parcelles_87_new_pkey PRIMARY KEY (idu, code_dep);


--
-- Name: cadastre_parcelles_histo cadastre_parcelles_histo_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cadastre_parcelles_histo
    ADD CONSTRAINT cadastre_parcelles_histo_pkey PRIMARY KEY (millesime, idu);


--
-- Name: cesbio cesbio_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.cesbio
    ADD CONSTRAINT cesbio_pkey PRIMARY KEY (id);


--
-- Name: data_interne_layer_notes data_interne_layer_notes_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.data_interne_layer_notes
    ADD CONSTRAINT data_interne_layer_notes_pkey PRIMARY KEY (layer_key);


--
-- Name: durete_fonciere_cache durete_fonciere_cache_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.durete_fonciere_cache
    ADD CONSTRAINT durete_fonciere_cache_pkey PRIMARY KEY (siren, cache_version);


--
-- Name: emprise_zpens emprise_zpens_code_zpens_key; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.emprise_zpens
    ADD CONSTRAINT emprise_zpens_code_zpens_key UNIQUE (code_zpens);


--
-- Name: emprise_zpens_etl_log emprise_zpens_etl_log_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.emprise_zpens_etl_log
    ADD CONSTRAINT emprise_zpens_etl_log_pkey PRIMARY KEY (id);


--
-- Name: emprise_zpens emprise_zpens_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.emprise_zpens
    ADD CONSTRAINT emprise_zpens_pkey PRIMARY KEY (id);


--
-- Name: espaces_boises_classes espaces_boises_classes_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.espaces_boises_classes
    ADD CONSTRAINT espaces_boises_classes_pkey PRIMARY KEY (cle);


--
-- Name: faune_import faune_import_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.faune_import
    ADD CONSTRAINT faune_import_pkey PRIMARY KEY (id);


--
-- Name: faune faune_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.faune
    ADD CONSTRAINT faune_pkey PRIMARY KEY (id);


--
-- Name: filiation_vers_actuel filiation_vers_actuel_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.filiation_vers_actuel
    ADD CONSTRAINT filiation_vers_actuel_pkey PRIMARY KEY (id_hist, id_actuel);


--
-- Name: flore_import flore_import_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.flore_import
    ADD CONSTRAINT flore_import_pkey PRIMARY KEY (id);


--
-- Name: flore_observation flore_obs_empreinte_uq; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.flore_observation
    ADD CONSTRAINT flore_obs_empreinte_uq UNIQUE (empreinte);


--
-- Name: flore_observation flore_observation_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.flore_observation
    ADD CONSTRAINT flore_observation_pkey PRIMARY KEY (id);


--
-- Name: foncier foncier_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.foncier
    ADD CONSTRAINT foncier_pkey PRIMARY KEY (id);


--
-- Name: geomce_lin geomce_lin_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.geomce_lin
    ADD CONSTRAINT geomce_lin_pkey PRIMARY KEY (id);


--
-- Name: geomce_pct geomce_pct_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.geomce_pct
    ADD CONSTRAINT geomce_pct_pkey PRIMARY KEY (id);


--
-- Name: geomce_surf geomce_surf_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.geomce_surf
    ADD CONSTRAINT geomce_surf_pkey PRIMARY KEY (id);


--
-- Name: natura_2000 natura_2000_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.natura_2000
    ADD CONSTRAINT natura_2000_pkey PRIMARY KEY (uid);


--
-- Name: observation_faune observation_faune_empreinte_uq; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.observation_faune
    ADD CONSTRAINT observation_faune_empreinte_uq UNIQUE (empreinte);


--
-- Name: observation_faune observation_faune_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.observation_faune
    ADD CONSTRAINT observation_faune_pkey PRIMARY KEY (id);


--
-- Name: parcelles_prospects_filtered parcelles_prospects_filtered_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.parcelles_prospects_filtered
    ADD CONSTRAINT parcelles_prospects_filtered_pkey PRIMARY KEY (siren, idu);


--
-- Name: parcelles_syndicates parcelles_syndicates_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.parcelles_syndicates
    ADD CONSTRAINT parcelles_syndicates_pkey PRIMARY KEY (id);


--
-- Name: pm_commune_rang pm_commune_rang_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_commune_rang
    ADD CONSTRAINT pm_commune_rang_pkey PRIMARY KEY (owner_key, code_insee);


--
-- Name: pm_idu_coproprio pm_idu_coproprio_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_idu_coproprio
    ADD CONSTRAINT pm_idu_coproprio_pkey PRIMARY KEY (idu);


--
-- Name: pm_ilot_membre pm_ilot_membre_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_ilot_membre
    ADD CONSTRAINT pm_ilot_membre_pkey PRIMARY KEY (owner_key, idu);


--
-- Name: pm_ilot pm_ilot_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_ilot
    ADD CONSTRAINT pm_ilot_pkey PRIMARY KEY (owner_key, ilot_num);


--
-- Name: pm_parcelle pm_parcelle_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_parcelle
    ADD CONSTRAINT pm_parcelle_pkey PRIMARY KEY (owner_key, idu);


--
-- Name: pm_patch_owners pm_patch_owners_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_patch_owners
    ADD CONSTRAINT pm_patch_owners_pkey PRIMARY KEY (owner_key);


--
-- Name: pm_propension_typologie pm_propension_typologie_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_propension_typologie
    ADD CONSTRAINT pm_propension_typologie_pkey PRIMARY KEY (typologie);


--
-- Name: pm_proprietaire pm_proprietaire_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.pm_proprietaire
    ADD CONSTRAINT pm_proprietaire_pkey PRIMARY KEY (owner_key);


--
-- Name: project_parcelles project_parcelles_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.project_parcelles
    ADD CONSTRAINT project_parcelles_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: projets_environnementaux_etl_log projets_environnementaux_etl_log_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projets_environnementaux_etl_log
    ADD CONSTRAINT projets_environnementaux_etl_log_pkey PRIMARY KEY (id);


--
-- Name: projets_environnementaux projets_environnementaux_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projets_environnementaux
    ADD CONSTRAINT projets_environnementaux_pkey PRIMARY KEY (id);


--
-- Name: projets_environnementaux projets_environnementaux_recordsid_key; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projets_environnementaux
    ADD CONSTRAINT projets_environnementaux_recordsid_key UNIQUE (recordsid);


--
-- Name: remontee_de_nappes remontee_de_nappes_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.remontee_de_nappes
    ADD CONSTRAINT remontee_de_nappes_pkey PRIMARY KEY (id);


--
-- Name: reserve_fonciere reserve_fonciere_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.reserve_fonciere
    ADD CONSTRAINT reserve_fonciere_pkey PRIMARY KEY (id);


--
-- Name: rf_contact rf_contact_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rf_contact
    ADD CONSTRAINT rf_contact_pkey PRIMARY KEY (id);


--
-- Name: rf_ilot rf_ilot_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rf_ilot
    ADD CONSTRAINT rf_ilot_pkey PRIMARY KEY (id);


--
-- Name: rpg2025_ilot_membre rpg2025_ilot_membre_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rpg2025_ilot_membre
    ADD CONSTRAINT rpg2025_ilot_membre_pkey PRIMARY KEY (num_ilot, idu);


--
-- Name: sitadel_autorisation sitadel_autorisation_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.sitadel_autorisation
    ADD CONSTRAINT sitadel_autorisation_pkey PRIMARY KEY (dataset, num_autorisation);


--
-- Name: sitadel_emprise sitadel_emprise_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.sitadel_emprise
    ADD CONSTRAINT sitadel_emprise_pkey PRIMARY KEY (dataset, num_autorisation);


--
-- Name: sitadel_parcelle sitadel_parcelle_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.sitadel_parcelle
    ADD CONSTRAINT sitadel_parcelle_pkey PRIMARY KEY (dataset, num_autorisation, rang, idu);


--
-- Name: surfaces_hydros surfaces_hydros_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.surfaces_hydros
    ADD CONSTRAINT surfaces_hydros_pkey PRIMARY KEY (cleabs);


--
-- Name: troncons_hydros troncons_hydros_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.troncons_hydros
    ADD CONSTRAINT troncons_hydros_pkey PRIMARY KEY (cleabs);


--
-- Name: vegetation_sur_cesbio_old vegetation_sur_cesbio_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.vegetation_sur_cesbio_old
    ADD CONSTRAINT vegetation_sur_cesbio_pkey PRIMARY KEY (id);


--
-- Name: vegetation_sur_cesbio vegetation_sur_cesbio_v2_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.vegetation_sur_cesbio
    ADD CONSTRAINT vegetation_sur_cesbio_v2_pkey PRIMARY KEY (id);


--
-- Name: znieff znieff_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.znieff
    ADD CONSTRAINT znieff_pkey PRIMARY KEY (cle);


--
-- Name: zone_humide zone_humide_pkey; Type: CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.zone_humide
    ADD CONSTRAINT zone_humide_pkey PRIMARY KEY (id);


--
-- Name: arrachage_vignes arrachage_vignes_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.arrachage_vignes
    ADD CONSTRAINT arrachage_vignes_pkey PRIMARY KEY (id);


--
-- Name: bd_topo_et_cesbio bd_topo_et_cesbio_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.bd_topo_et_cesbio
    ADD CONSTRAINT bd_topo_et_cesbio_pkey PRIMARY KEY (id);


--
-- Name: carhab carhab_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.carhab
    ADD CONSTRAINT carhab_pkey PRIMARY KEY (id);


--
-- Name: cesbio cesbio_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.cesbio
    ADD CONSTRAINT cesbio_pkey PRIMARY KEY (id);


--
-- Name: cosia cosia_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.cosia
    ADD CONSTRAINT cosia_pkey PRIMARY KEY (id);


--
-- Name: ebc ebc_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.ebc
    ADD CONSTRAINT ebc_pkey PRIMARY KEY (id);


--
-- Name: espaces_naturels_sensibles_ens espaces_naturels_sensibles_ens_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.espaces_naturels_sensibles_ens
    ADD CONSTRAINT espaces_naturels_sensibles_ens_pkey PRIMARY KEY (id);


--
-- Name: fragmentation_polygons fragmentation_polygons_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.fragmentation_polygons
    ADD CONSTRAINT fragmentation_polygons_pkey PRIMARY KEY (id);


--
-- Name: frayeres frayeres_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.frayeres
    ADD CONSTRAINT frayeres_pkey PRIMARY KEY (id);


--
-- Name: ilots_rpg ilots_rpg_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.ilots_rpg
    ADD CONSTRAINT ilots_rpg_pkey PRIMARY KEY (project_id, num_ilot);


--
-- Name: mesures_compensatoire_lin mesures_compensatoire_lin_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.mesures_compensatoire_lin
    ADD CONSTRAINT mesures_compensatoire_lin_pkey PRIMARY KEY (id);


--
-- Name: mesures_compensatoire_pct mesures_compensatoire_pct_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.mesures_compensatoire_pct
    ADD CONSTRAINT mesures_compensatoire_pct_pkey PRIMARY KEY (id);


--
-- Name: mesures_compensatoire_surf mesures_compensatoire_surf_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.mesures_compensatoire_surf
    ADD CONSTRAINT mesures_compensatoire_surf_pkey PRIMARY KEY (id);


--
-- Name: mnt_dalles mnt_dalles_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.mnt_dalles
    ADD CONSTRAINT mnt_dalles_pkey PRIMARY KEY (aoi_id, filename);


--
-- Name: natura2000 natura2000_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.natura2000
    ADD CONSTRAINT natura2000_pkey PRIMARY KEY (rid);


--
-- Name: parcelles_pool_indesirables parcelles_pool_indesirables_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.parcelles_pool_indesirables
    ADD CONSTRAINT parcelles_pool_indesirables_pkey PRIMARY KEY (run_id, idu);


--
-- Name: parcelles_pool_metrics parcelles_pool_metrics_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.parcelles_pool_metrics
    ADD CONSTRAINT parcelles_pool_metrics_pkey PRIMARY KEY (run_id, idu, metric_key);


--
-- Name: parcelles_pool parcelles_pool_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.parcelles_pool
    ADD CONSTRAINT parcelles_pool_pkey PRIMARY KEY (run_id, idu);


--
-- Name: parcelles_pool_runs parcelles_pool_runs_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.parcelles_pool_runs
    ADD CONSTRAINT parcelles_pool_runs_pkey PRIMARY KEY (id);


--
-- Name: parcelles_project_indesirables parcelles_project_indesirables_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.parcelles_project_indesirables
    ADD CONSTRAINT parcelles_project_indesirables_pkey PRIMARY KEY (project_id, idu);


--
-- Name: patrimoine_naturel patrimoine_naturel_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.patrimoine_naturel
    ADD CONSTRAINT patrimoine_naturel_pkey PRIMARY KEY (id);


--
-- Name: preemption_espaces_naturels_sensibles preemption_espaces_naturels_sensibles_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.preemption_espaces_naturels_sensibles
    ADD CONSTRAINT preemption_espaces_naturels_sensibles_pkey PRIMARY KEY (id);


--
-- Name: remontee_de_nappes remontee_de_nappes_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.remontee_de_nappes
    ADD CONSTRAINT remontee_de_nappes_pkey PRIMARY KEY (id);


--
-- Name: reserves_naturelles reserves_naturelles_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.reserves_naturelles
    ADD CONSTRAINT reserves_naturelles_pkey PRIMARY KEY (id);


--
-- Name: routes routes_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.routes
    ADD CONSTRAINT routes_pkey PRIMARY KEY (id);


--
-- Name: sites_classes sites_classes_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.sites_classes
    ADD CONSTRAINT sites_classes_pkey PRIMARY KEY (id);


--
-- Name: sous_ensembles sous_ensembles_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.sous_ensembles
    ADD CONSTRAINT sous_ensembles_pkey PRIMARY KEY (id);


--
-- Name: surfaces_hydros surfaces_hydros_results_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.surfaces_hydros
    ADD CONSTRAINT surfaces_hydros_results_pkey PRIMARY KEY (project_id, cleabs);


--
-- Name: troncons_hydros troncons_hydros_results_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.troncons_hydros
    ADD CONSTRAINT troncons_hydros_results_pkey PRIMARY KEY (project_id, cleabs);


--
-- Name: unites_foncieres unites_foncieres_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.unites_foncieres
    ADD CONSTRAINT unites_foncieres_pkey PRIMARY KEY (id);


--
-- Name: urba_cible_resultat urba_cible_resultat_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.urba_cible_resultat
    ADD CONSTRAINT urba_cible_resultat_pkey PRIMARY KEY (run_id, cible_id, cible_type);


--
-- Name: urba_entite urba_entite_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.urba_entite
    ADD CONSTRAINT urba_entite_pkey PRIMARY KEY (run_id, layer_key, fid);


--
-- Name: urba_parcelle_entite urba_parcelle_entite_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.urba_parcelle_entite
    ADD CONSTRAINT urba_parcelle_entite_pkey PRIMARY KEY (run_id, cible_id, cible_type, layer_key, fid);


--
-- Name: urba_run urba_run_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.urba_run
    ADD CONSTRAINT urba_run_pkey PRIMARY KEY (run_id);


--
-- Name: voies_ferrees voies_ferrees_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.voies_ferrees
    ADD CONSTRAINT voies_ferrees_pkey PRIMARY KEY (id);


--
-- Name: znieff znieff_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.znieff
    ADD CONSTRAINT znieff_pkey PRIMARY KEY (id);


--
-- Name: zone_de_vegetation zone_de_vegetation_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.zone_de_vegetation
    ADD CONSTRAINT zone_de_vegetation_pkey PRIMARY KEY (id);


--
-- Name: zone_humide zone_humide_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.zone_humide
    ADD CONSTRAINT zone_humide_pkey PRIMARY KEY (id);


--
-- Name: zones_humides_probables zones_humides_probables_pkey; Type: CONSTRAINT; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ONLY ecocompensation_results.zones_humides_probables
    ADD CONSTRAINT zones_humides_probables_pkey PRIMARY KEY (id);


--
-- Name: canopy_analysis canopy_analysis_parcelle_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canopy_analysis
    ADD CONSTRAINT canopy_analysis_parcelle_id_key UNIQUE (parcelle_id);


--
-- Name: canopy_analysis canopy_analysis_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canopy_analysis
    ADD CONSTRAINT canopy_analysis_pkey PRIMARY KEY (id);


--
-- Name: lidar_tiles lidar_tiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lidar_tiles
    ADD CONSTRAINT lidar_tiles_pkey PRIMARY KEY (id);


--
-- Name: lidar_tiles lidar_tiles_tile_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lidar_tiles
    ADD CONSTRAINT lidar_tiles_tile_name_key UNIQUE (tile_name);


--
-- Name: cadastre_parcelles_key_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_key_idx ON ONLY ecocompensation.cadastre_parcelles USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_16_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_16_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_16 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_abs_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_abs_idx ON ONLY ecocompensation.cadastre_parcelles USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_16_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_16_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_16 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_gix ON ONLY ecocompensation.cadastre_parcelles USING gist (geom);


--
-- Name: cadastre_parcelles_16_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_16_new_geom_idx ON ecocompensation.cadastre_parcelles_16 USING gist (geom);


--
-- Name: cadastre_parcelles_17_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_17_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_17 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_17_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_17_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_17 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_17_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_17_new_geom_idx ON ecocompensation.cadastre_parcelles_17 USING gist (geom);


--
-- Name: cadastre_parcelles_19_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_19_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_19 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_19_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_19_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_19 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_19_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_19_new_geom_idx ON ecocompensation.cadastre_parcelles_19 USING gist (geom);


--
-- Name: cadastre_parcelles_23_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_23_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_23 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_23_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_23_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_23 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_23_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_23_new_geom_idx ON ecocompensation.cadastre_parcelles_23 USING gist (geom);


--
-- Name: cadastre_parcelles_24_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_24_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_24 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_24_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_24_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_24 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_24_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_24_new_geom_idx ON ecocompensation.cadastre_parcelles_24 USING gist (geom);


--
-- Name: cadastre_parcelles_33_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_33_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_33 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_33_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_33_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_33 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_33_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_33_new_geom_idx ON ecocompensation.cadastre_parcelles_33 USING gist (geom);


--
-- Name: cadastre_parcelles_40_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_40_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_40 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_40_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_40_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_40 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_40_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_40_new_geom_idx ON ecocompensation.cadastre_parcelles_40 USING gist (geom);


--
-- Name: cadastre_parcelles_47_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_47_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_47 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_47_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_47_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_47 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_47_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_47_new_geom_idx ON ecocompensation.cadastre_parcelles_47 USING gist (geom);


--
-- Name: cadastre_parcelles_64_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_64_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_64 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_64_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_64_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_64 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_64_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_64_new_geom_idx ON ecocompensation.cadastre_parcelles_64 USING gist (geom);


--
-- Name: cadastre_parcelles_79_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_79_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_79 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_79_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_79_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_79 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_79_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_79_new_geom_idx ON ecocompensation.cadastre_parcelles_79 USING gist (geom);


--
-- Name: cadastre_parcelles_86_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_86_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_86 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_86_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_86_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_86 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_86_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_86_new_geom_idx ON ecocompensation.cadastre_parcelles_86 USING gist (geom);


--
-- Name: cadastre_parcelles_87_new_code_insee_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_87_new_code_insee_section_numero_idx ON ecocompensation.cadastre_parcelles_87 USING btree (code_insee, section, numero);


--
-- Name: cadastre_parcelles_87_new_com_abs_section_numero_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_87_new_com_abs_section_numero_idx ON ecocompensation.cadastre_parcelles_87 USING btree (com_abs, section, numero);


--
-- Name: cadastre_parcelles_87_new_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_87_new_geom_idx ON ecocompensation.cadastre_parcelles_87 USING gist (geom);


--
-- Name: cadastre_parcelles_histo_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_histo_gix ON ecocompensation.cadastre_parcelles_histo USING gist (geom);


--
-- Name: cadastre_parcelles_histo_k; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cadastre_parcelles_histo_k ON ecocompensation.cadastre_parcelles_histo USING btree (dep, section, numero);


--
-- Name: carhab_clean_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX carhab_clean_geom_idx ON ecocompensation.carhab_clean USING gist (geom);


--
-- Name: cesbio_conif_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cesbio_conif_idx ON ecocompensation.cesbio USING btree (coniferes);


--
-- Name: cesbio_dept_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cesbio_dept_idx ON ecocompensation.cesbio USING btree (departement);


--
-- Name: cesbio_eau_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cesbio_eau_idx ON ecocompensation.cesbio USING btree (eau);


--
-- Name: cesbio_feuil_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cesbio_feuil_idx ON ecocompensation.cesbio USING btree (feuillus);


--
-- Name: cesbio_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX cesbio_geom_idx ON ecocompensation.cesbio USING gist (geom);


--
-- Name: filiation_vers_actuel_actuel_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX filiation_vers_actuel_actuel_idx ON ecocompensation.filiation_vers_actuel USING btree (id_actuel);


--
-- Name: filiation_vers_actuel_cle_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX filiation_vers_actuel_cle_idx ON ecocompensation.filiation_vers_actuel USING btree (code_dep, code_insee, section, numero);


--
-- Name: flore_observation_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX flore_observation_geom_gix ON ecocompensation.flore_observation USING gist (geom);


--
-- Name: flore_observation_import_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX flore_observation_import_idx ON ecocompensation.flore_observation USING btree (import_id);


--
-- Name: flore_observation_taxon_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX flore_observation_taxon_idx ON ecocompensation.flore_observation USING btree (taxon_key);


--
-- Name: geomce_lin_identifiant_uidx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE UNIQUE INDEX geomce_lin_identifiant_uidx ON ecocompensation.geomce_lin USING btree (identifiant);


--
-- Name: geomce_pct_identifiant_uidx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE UNIQUE INDEX geomce_pct_identifiant_uidx ON ecocompensation.geomce_pct USING btree (identifiant);


--
-- Name: geomce_surf_identifiant_uidx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE UNIQUE INDEX geomce_surf_identifiant_uidx ON ecocompensation.geomce_surf USING btree (identifiant);


--
-- Name: idx_aoi_project_id; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_aoi_project_id ON ecocompensation.aoi USING btree (project_id);


--
-- Name: idx_arrachage_vignes_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_arrachage_vignes_geom_2154 ON ecocompensation.arrachage_vignes USING gist (geom_2154);


--
-- Name: idx_bv_spe_masse_d_eau_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_bv_spe_masse_d_eau_geom_2154 ON ecocompensation.bv_spe_masse_d_eau USING gist (geom_2154);


--
-- Name: idx_cartofriches_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_cartofriches_geometry ON ecocompensation.cartofriches USING gist (geometry);


--
-- Name: idx_composition_des_sols_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_composition_des_sols_geom_2154 ON ecocompensation.composition_des_sols USING gist (geom_2154);


--
-- Name: idx_composition_des_sols_wfs_id; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE UNIQUE INDEX idx_composition_des_sols_wfs_id ON ecocompensation.composition_des_sols USING btree (wfs_id);


--
-- Name: idx_durete_cache_expires_at; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_durete_cache_expires_at ON ecocompensation.durete_fonciere_cache USING btree (expires_at);


--
-- Name: idx_ebc_classe; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ebc_classe ON ecocompensation.espaces_boises_classes USING btree (classe);


--
-- Name: idx_ebc_ref_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ebc_ref_geom ON ecocompensation.espaces_boises_classes USING gist (geom_2154);


--
-- Name: idx_ebc_ref_stype; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ebc_ref_stype ON ecocompensation.espaces_boises_classes USING btree (stypepsc);


--
-- Name: idx_ecocomp_zone_humide_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ecocomp_zone_humide_geom ON ecocompensation.zone_humide USING gist (geom_2154);


--
-- Name: idx_emprise_zpens_code; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_emprise_zpens_code ON ecocompensation.emprise_zpens USING btree (code_zpens);


--
-- Name: idx_emprise_zpens_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_emprise_zpens_geom_2154 ON ecocompensation.emprise_zpens USING gist (geom_2154);


--
-- Name: idx_emprise_zpens_geom_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_emprise_zpens_geom_3857 ON ecocompensation.emprise_zpens USING gist (geom_3857);


--
-- Name: idx_emprise_zpens_hash; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_emprise_zpens_hash ON ecocompensation.emprise_zpens USING btree (content_hash);


--
-- Name: idx_emprise_zpens_jsonb; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_emprise_zpens_jsonb ON ecocompensation.emprise_zpens USING gin (attributs_source);


--
-- Name: idx_ens_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ens_geom_2154 ON ecocompensation.espaces_naturels_sensibles_ens USING gist (geom_2154);


--
-- Name: idx_ens_geom_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ens_geom_3857 ON ecocompensation.espaces_naturels_sensibles_ens USING gist (geom_3857);


--
-- Name: idx_etat_cours_d_eau_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_etat_cours_d_eau_geom_2154 ON ecocompensation.etat_cours_d_eau USING gist (geom_2154);


--
-- Name: idx_etat_masses_d_eau_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_etat_masses_d_eau_geom_2154 ON ecocompensation.etat_masses_d_eau USING gist (geom_2154);


--
-- Name: idx_fauna_cd_ref; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_cd_ref ON ecocompensation.fauna USING btree (cd_ref);


--
-- Name: idx_fauna_date_debut; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_date_debut ON ecocompensation.fauna USING btree (date_debut);


--
-- Name: idx_fauna_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_geom ON ecocompensation.fauna USING gist (geometry);


--
-- Name: idx_fauna_geom_4326_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_geom_4326_gist ON ecocompensation.fauna USING gist (public.st_transform(geometry, 4326));


--
-- Name: idx_fauna_geom_id; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_geom_id ON ecocompensation.fauna USING btree (geom_id);


--
-- Name: idx_fauna_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_geometry ON ecocompensation.fauna USING gist (geometry);


--
-- Name: idx_fauna_id_obs; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_id_obs ON ecocompensation.fauna USING btree (id_obs);


--
-- Name: idx_fauna_taxa_ref_tax; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_fauna_taxa_ref_tax ON ecocompensation.fauna_taxa_ref USING btree (tax);


--
-- Name: idx_faune_categorie; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_categorie ON ecocompensation.faune USING btree (categorie);


--
-- Name: idx_faune_enjeu_na; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_enjeu_na ON ecocompensation.faune USING btree (enjeu_conservation_na);


--
-- Name: idx_faune_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_geom ON ecocompensation.fauna USING gist (geometry);


--
-- Name: idx_faune_groupe; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_groupe ON ecocompensation.faune USING btree (groupe_especes);


--
-- Name: idx_faune_lin_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_lin_geometry ON ecocompensation.faune_lin USING gist (geometry);


--
-- Name: idx_faune_liste_rouge_fr; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_liste_rouge_fr ON ecocompensation.faune USING btree (liste_rouge_uicn_france);


--
-- Name: idx_faune_nom_scientifique; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_nom_scientifique ON ecocompensation.faune USING btree (nom_scientifique);


--
-- Name: idx_faune_nom_vernaculaire; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_nom_vernaculaire ON ecocompensation.faune USING btree (nom_vernaculaire);


--
-- Name: idx_faune_pct_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_pct_geometry ON ecocompensation.faune_pct USING gist (geometry);


--
-- Name: idx_faune_protection; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_protection ON ecocompensation.faune USING btree (protection_nationale);


--
-- Name: idx_faune_search; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_search ON ecocompensation.faune USING gin (to_tsvector('french'::regconfig, (((nom_vernaculaire)::text || ' '::text) || (nom_scientifique)::text)));


--
-- Name: idx_faune_surf_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_faune_surf_geometry ON ecocompensation.faune_surf USING gist (geometry);


--
-- Name: idx_geomce_lin_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_lin_geom ON ecocompensation.geomce_lin USING gist (geom_2154);


--
-- Name: idx_geomce_lin_identifiant; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_lin_identifiant ON ecocompensation.geomce_lin USING btree (identifiant);


--
-- Name: idx_geomce_pct_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_pct_geom ON ecocompensation.geomce_pct USING gist (geom_2154);


--
-- Name: idx_geomce_pct_identifiant; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_pct_identifiant ON ecocompensation.geomce_pct USING btree (identifiant);


--
-- Name: idx_geomce_surf_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_surf_geom ON ecocompensation.geomce_surf USING gist (geom_2154);


--
-- Name: idx_geomce_surf_identifiant; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_geomce_surf_identifiant ON ecocompensation.geomce_surf USING btree (identifiant);


--
-- Name: idx_ocs_ge_geometry; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ocs_ge_geometry ON ecocompensation.ocs_ge USING gist (geometry);


--
-- Name: idx_ocs_ge_libelle_cs; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ocs_ge_libelle_cs ON ecocompensation.ocs_ge USING btree (libelle_cs);


--
-- Name: idx_ocs_ge_libelle_us; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ocs_ge_libelle_us ON ecocompensation.ocs_ge USING btree (libelle_us);


--
-- Name: idx_parcelles_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_parcelles_geom_2154 ON ecocompensation.parcelles USING gist (geom_2154);


--
-- Name: idx_parcelles_syndicates_geo_parcel; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_parcelles_syndicates_geo_parcel ON ecocompensation.parcelles_syndicates USING btree (geo_parcel);


--
-- Name: idx_parcelles_syndicates_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_parcelles_syndicates_geom_2154 ON ecocompensation.parcelles_syndicates USING gist (geom_2154);


--
-- Name: idx_parcelles_syndicates_geom_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_parcelles_syndicates_geom_3857 ON ecocompensation.parcelles_syndicates USING gist (geom_3857);


--
-- Name: idx_parcelles_syndicates_idu; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_parcelles_syndicates_idu ON ecocompensation.parcelles_syndicates USING btree (idu);


--
-- Name: idx_ppf_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ppf_geom ON ecocompensation.parcelles_prospects_filtered USING gist (geom_2154);


--
-- Name: idx_ppf_idu; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ppf_idu ON ecocompensation.parcelles_prospects_filtered USING btree (idu);


--
-- Name: idx_ppf_insee; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ppf_insee ON ecocompensation.parcelles_prospects_filtered USING btree (code_insee);


--
-- Name: idx_ppf_siren; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_ppf_siren ON ecocompensation.parcelles_prospects_filtered USING btree (siren);


--
-- Name: idx_preemption_ens_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_preemption_ens_geom_2154 ON ecocompensation.preemption_espaces_naturels_sensibles USING gist (geom_2154);


--
-- Name: idx_project_parcelles_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_project_parcelles_geom ON ecocompensation.project_parcelles USING gist (geom_2154);


--
-- Name: idx_project_parcelles_project_id; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_project_parcelles_project_id ON ecocompensation.project_parcelles USING btree (project_id);


--
-- Name: idx_projects_study_type; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projects_study_type ON ecocompensation.projects USING btree (study_type);


--
-- Name: idx_projets_environnementaux_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_geom_2154 ON ecocompensation.projets_environnementaux USING gist (geom_2154);


--
-- Name: idx_projets_environnementaux_geom_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_geom_3857 ON ecocompensation.projets_environnementaux USING gist (geom_3857);


--
-- Name: idx_projets_environnementaux_hash; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_hash ON ecocompensation.projets_environnementaux USING btree (content_hash);


--
-- Name: idx_projets_environnementaux_jsonb; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_jsonb ON ecocompensation.projets_environnementaux USING gin (attributs_source);


--
-- Name: idx_projets_environnementaux_recordsid; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_recordsid ON ecocompensation.projets_environnementaux USING btree (recordsid);


--
-- Name: idx_projets_environnementaux_statut; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_projets_environnementaux_statut ON ecocompensation.projets_environnementaux USING btree (statut);


--
-- Name: idx_remontee_de_nappes_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_remontee_de_nappes_geom ON ecocompensation.remontee_de_nappes USING gist (geom_2154);


--
-- Name: idx_remontee_de_nappes_source_index; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_remontee_de_nappes_source_index ON ecocompensation.remontee_de_nappes USING btree (source_index);


--
-- Name: idx_reserve_fonciere_geom_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_geom_2154 ON ecocompensation.reserve_fonciere USING gist (geom_2154);


--
-- Name: idx_reserve_fonciere_geom_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_geom_3857 ON ecocompensation.reserve_fonciere USING gist (geom_3857);


--
-- Name: idx_reserve_fonciere_idu; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_idu ON ecocompensation.reserve_fonciere USING btree (idu);


--
-- Name: idx_reserve_fonciere_jsonb; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_jsonb ON ecocompensation.reserve_fonciere USING gin (attributs_source);


--
-- Name: idx_reserve_fonciere_source; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_source ON ecocompensation.reserve_fonciere USING btree (source_type);


--
-- Name: idx_reserve_fonciere_source_idu; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_source_idu ON ecocompensation.reserve_fonciere USING btree (source_type, idu);


--
-- Name: idx_reserve_fonciere_statut; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_reserve_fonciere_statut ON ecocompensation.reserve_fonciere USING btree (statut_prospection);


--
-- Name: idx_rpg2025_ilots_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_rpg2025_ilots_geom ON ecocompensation.rpg2025_ilots USING gist (geom);


--
-- Name: idx_rpg2025_parcelles_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_rpg2025_parcelles_geom ON ecocompensation.rpg2025_parcelles USING gist (geom);


--
-- Name: idx_rpg2025_sna_arbres_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_rpg2025_sna_arbres_geom ON ecocompensation.rpg2025_sna_arbres USING gist (geom);


--
-- Name: idx_rpg2025_sna_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_rpg2025_sna_geom ON ecocompensation.rpg2025_sna USING gist (geom);


--
-- Name: idx_rpg2025_zdh_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_rpg2025_zdh_geom ON ecocompensation.rpg2025_zdh USING gist (geom);


--
-- Name: idx_znieff_geom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_znieff_geom ON ecocompensation.znieff USING gist (geom_2154);


--
-- Name: idx_znieff_type; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX idx_znieff_type ON ecocompensation.znieff USING btree (type_znieff);


--
-- Name: ix_ecocompensation_faune_lin_index; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX ix_ecocompensation_faune_lin_index ON ecocompensation.faune_lin USING btree (index);


--
-- Name: ix_ecocompensation_faune_pct_index; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX ix_ecocompensation_faune_pct_index ON ecocompensation.faune_pct USING btree (index);


--
-- Name: ix_ecocompensation_faune_surf_index; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX ix_ecocompensation_faune_surf_index ON ecocompensation.faune_surf USING btree (index);


--
-- Name: natura_2000_gix_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX natura_2000_gix_2154 ON ecocompensation.natura_2000 USING gist (geom_2154);


--
-- Name: natura_2000_gix_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX natura_2000_gix_3857 ON ecocompensation.natura_2000 USING gist (geom_3857);


--
-- Name: natura_2000_idx_type; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX natura_2000_idx_type ON ecocompensation.natura_2000 USING btree (type);


--
-- Name: observation_faune_details_gin; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX observation_faune_details_gin ON ecocompensation.observation_faune USING gin (details);


--
-- Name: observation_faune_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX observation_faune_geom_gix ON ecocompensation.observation_faune USING gist (geom);


--
-- Name: observation_faune_import_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX observation_faune_import_idx ON ecocompensation.observation_faune USING btree (import_id);


--
-- Name: observation_faune_nom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX observation_faune_nom_idx ON ecocompensation.observation_faune USING btree (lower(nom_espece));


--
-- Name: pm_commune_rang_code_insee_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_commune_rang_code_insee_idx ON ecocompensation.pm_commune_rang USING btree (code_insee);


--
-- Name: pm_ilot_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_ilot_geom_idx ON ecocompensation.pm_ilot USING gist (geom);


--
-- Name: pm_ilot_membre_idu_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_ilot_membre_idu_idx ON ecocompensation.pm_ilot_membre USING btree (idu);


--
-- Name: pm_parcelle_code_insee_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_parcelle_code_insee_idx ON ecocompensation.pm_parcelle USING btree (code_insee);


--
-- Name: pm_parcelle_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_parcelle_geom_idx ON ecocompensation.pm_parcelle USING gist (geom);


--
-- Name: pm_parcelle_idu_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_parcelle_idu_idx ON ecocompensation.pm_parcelle USING btree (idu);


--
-- Name: pm_parcelle_typologie_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_parcelle_typologie_idx ON ecocompensation.pm_parcelle USING btree (typologie);


--
-- Name: pm_proprietaire_centre_patrimoine_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_proprietaire_centre_patrimoine_idx ON ecocompensation.pm_proprietaire USING gist (centre_patrimoine);


--
-- Name: pm_proprietaire_niveau_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_proprietaire_niveau_idx ON ecocompensation.pm_proprietaire USING btree (niveau_propension);


--
-- Name: pm_proprietaire_siren_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX pm_proprietaire_siren_idx ON ecocompensation.pm_proprietaire USING btree (siren);


--
-- Name: rf_idu_actif_uq; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE UNIQUE INDEX rf_idu_actif_uq ON ecocompensation.reserve_fonciere USING btree (idu) WHERE ((idu IS NOT NULL) AND (archived_at IS NULL));


--
-- Name: rf_ilot_contact_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rf_ilot_contact_idx ON ecocompensation.rf_ilot USING btree (contact_id);


--
-- Name: rf_ilot_statut_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rf_ilot_statut_idx ON ecocompensation.rf_ilot USING btree (statut_prospection);


--
-- Name: rf_parcelle_ilot_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rf_parcelle_ilot_idx ON ecocompensation.reserve_fonciere USING btree (ilot_id);


--
-- Name: rpg2025_ilot_membre_dep_insee_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilot_membre_dep_insee_idx ON ecocompensation.rpg2025_ilot_membre USING btree (code_dep, code_insee);


--
-- Name: rpg2025_ilot_membre_idu_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilot_membre_idu_idx ON ecocompensation.rpg2025_ilot_membre USING btree (idu);


--
-- Name: rpg2025_ilots_code_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilots_code_dep_idx ON ecocompensation.rpg2025_ilots USING btree (code_dep);


--
-- Name: rpg2025_ilots_geom_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilots_geom_gist ON ecocompensation.rpg2025_ilots USING gist (geom);


--
-- Name: rpg2025_ilots_num_ilot_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilots_num_ilot_idx ON ecocompensation.rpg2025_ilots USING btree (num_ilot);


--
-- Name: rpg2025_ilots_pm_dominant_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_ilots_pm_dominant_idx ON ecocompensation.rpg2025_ilots USING btree (pm_dominant);


--
-- Name: rpg2025_parcelles_code_cultu_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_parcelles_code_cultu_idx ON ecocompensation.rpg2025_parcelles USING btree (code_cultu);


--
-- Name: rpg2025_parcelles_code_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_parcelles_code_dep_idx ON ecocompensation.rpg2025_parcelles USING btree (code_dep);


--
-- Name: rpg2025_parcelles_code_group_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_parcelles_code_group_idx ON ecocompensation.rpg2025_parcelles USING btree (code_group);


--
-- Name: rpg2025_parcelles_geom_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_parcelles_geom_gist ON ecocompensation.rpg2025_parcelles USING gist (geom);


--
-- Name: rpg2025_parcelles_num_ilot_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_parcelles_num_ilot_idx ON ecocompensation.rpg2025_parcelles USING btree (num_ilot);


--
-- Name: rpg2025_sna_arbres_code_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_sna_arbres_code_dep_idx ON ecocompensation.rpg2025_sna_arbres USING btree (code_dep);


--
-- Name: rpg2025_sna_arbres_geom_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_sna_arbres_geom_gist ON ecocompensation.rpg2025_sna_arbres USING gist (geom);


--
-- Name: rpg2025_sna_code_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_sna_code_dep_idx ON ecocompensation.rpg2025_sna USING btree (code_dep);


--
-- Name: rpg2025_sna_geom_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_sna_geom_gist ON ecocompensation.rpg2025_sna USING gist (geom);


--
-- Name: rpg2025_sna_type_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_sna_type_idx ON ecocompensation.rpg2025_sna USING btree (type);


--
-- Name: rpg2025_zdh_code_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_zdh_code_dep_idx ON ecocompensation.rpg2025_zdh USING btree (code_dep);


--
-- Name: rpg2025_zdh_geom_gist; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_zdh_geom_gist ON ecocompensation.rpg2025_zdh USING gist (geom);


--
-- Name: rpg2025_zdh_prorata_ordre_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX rpg2025_zdh_prorata_ordre_idx ON ecocompensation.rpg2025_zdh USING btree (prorata_ordre);


--
-- Name: sitadel_autorisation_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_autorisation_dep_idx ON ecocompensation.sitadel_autorisation USING btree (code_dep);


--
-- Name: sitadel_autorisation_insee_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_autorisation_insee_idx ON ecocompensation.sitadel_autorisation USING btree (code_insee);


--
-- Name: sitadel_autorisation_motif_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_autorisation_motif_idx ON ecocompensation.sitadel_autorisation USING btree (motif);


--
-- Name: sitadel_autorisation_pt_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_autorisation_pt_gix ON ecocompensation.sitadel_autorisation USING gist (pt);


--
-- Name: sitadel_emprise_dep; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_emprise_dep ON ecocompensation.sitadel_emprise USING btree (dep);


--
-- Name: sitadel_emprise_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_emprise_gix ON ecocompensation.sitadel_emprise USING gist (geom);


--
-- Name: sitadel_lien_parcelle_idu; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_lien_parcelle_idu ON ecocompensation.sitadel_lien_parcelle USING btree (idu);


--
-- Name: sitadel_lien_parcelle_k; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_lien_parcelle_k ON ecocompensation.sitadel_lien_parcelle USING btree (dep, dataset, num_autorisation, rang);


--
-- Name: sitadel_locaux_comm_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_locaux_comm_idx ON ecocompensation.sitadel_locaux USING btree (comm);


--
-- Name: sitadel_locaux_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_locaux_dep_idx ON ecocompensation.sitadel_locaux USING btree (dep_code);


--
-- Name: sitadel_locaux_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_locaux_geom_gix ON ecocompensation.sitadel_locaux USING gist (geom);


--
-- Name: sitadel_locaux_num_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_locaux_num_idx ON ecocompensation.sitadel_locaux USING btree (num_autorisation);


--
-- Name: sitadel_locaux_siren_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_locaux_siren_idx ON ecocompensation.sitadel_locaux USING btree (siren_dem) WHERE (siren_dem IS NOT NULL);


--
-- Name: sitadel_logements_comm_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_logements_comm_idx ON ecocompensation.sitadel_logements USING btree (comm);


--
-- Name: sitadel_logements_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_logements_dep_idx ON ecocompensation.sitadel_logements USING btree (dep_code);


--
-- Name: sitadel_logements_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_logements_geom_gix ON ecocompensation.sitadel_logements USING gist (geom);


--
-- Name: sitadel_logements_num_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_logements_num_idx ON ecocompensation.sitadel_logements USING btree (num_autorisation);


--
-- Name: sitadel_logements_siren_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_logements_siren_idx ON ecocompensation.sitadel_logements USING btree (siren_dem) WHERE (siren_dem IS NOT NULL);


--
-- Name: sitadel_pa_comm_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pa_comm_idx ON ecocompensation.sitadel_pa USING btree (comm);


--
-- Name: sitadel_pa_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pa_dep_idx ON ecocompensation.sitadel_pa USING btree (dep_code);


--
-- Name: sitadel_pa_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pa_geom_gix ON ecocompensation.sitadel_pa USING gist (geom);


--
-- Name: sitadel_pa_num_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pa_num_idx ON ecocompensation.sitadel_pa USING btree (num_autorisation);


--
-- Name: sitadel_pa_siren_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pa_siren_idx ON ecocompensation.sitadel_pa USING btree (siren_dem) WHERE (siren_dem IS NOT NULL);


--
-- Name: sitadel_parcelle_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_parcelle_dep_idx ON ecocompensation.sitadel_parcelle USING btree (code_dep);


--
-- Name: sitadel_parcelle_dossier_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_parcelle_dossier_idx ON ecocompensation.sitadel_parcelle USING btree (code_dep, dataset, num_autorisation);


--
-- Name: sitadel_parcelle_idu_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_parcelle_idu_idx ON ecocompensation.sitadel_parcelle USING btree (idu);


--
-- Name: sitadel_parcelle_origine_k; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_parcelle_origine_k ON ecocompensation.sitadel_parcelle_origine USING btree (dep, dataset, num_autorisation);


--
-- Name: sitadel_pd_comm_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pd_comm_idx ON ecocompensation.sitadel_pd USING btree (comm);


--
-- Name: sitadel_pd_dep_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pd_dep_idx ON ecocompensation.sitadel_pd USING btree (dep_code);


--
-- Name: sitadel_pd_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pd_geom_gix ON ecocompensation.sitadel_pd USING gist (geom);


--
-- Name: sitadel_pd_num_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pd_num_idx ON ecocompensation.sitadel_pd USING btree (num_autorisation);


--
-- Name: sitadel_pd_siren_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_pd_siren_idx ON ecocompensation.sitadel_pd USING btree (siren_dem) WHERE (siren_dem IS NOT NULL);


--
-- Name: sitadel_ref_parcelle_k; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_ref_parcelle_k ON ecocompensation.sitadel_ref_parcelle USING btree (dep, dataset, num_autorisation, rang);


--
-- Name: sitadel_test_pc33_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_test_pc33_geom_idx ON ecocompensation.sitadel_test_pc33 USING gist (geom);


--
-- Name: sitadel_test_pc33_num_autorisation_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX sitadel_test_pc33_num_autorisation_idx ON ecocompensation.sitadel_test_pc33 USING btree (num_autorisation);


--
-- Name: surfaces_hydros_gix_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_gix_2154 ON ecocompensation.surfaces_hydros USING gist (geom_2154);


--
-- Name: surfaces_hydros_gix_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_gix_3857 ON ecocompensation.surfaces_hydros USING gist (geom_3857);


--
-- Name: surfaces_hydros_idx_codehydro; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_idx_codehydro ON ecocompensation.surfaces_hydros USING btree (code_hydrographique);


--
-- Name: surfaces_hydros_idx_nature; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_idx_nature ON ecocompensation.surfaces_hydros USING btree (nature);


--
-- Name: surfaces_hydros_idx_nom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_idx_nom ON ecocompensation.surfaces_hydros USING btree (nom);


--
-- Name: surfaces_hydros_idx_position; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX surfaces_hydros_idx_position ON ecocompensation.surfaces_hydros USING btree (position_par_rapport_au_sol);


--
-- Name: tmp_bdtopo_sub_geom_2154_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX tmp_bdtopo_sub_geom_2154_idx ON ecocompensation.tmp_bdtopo_sub USING gist (geom_2154);


--
-- Name: tmp_cesbio_sub_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX tmp_cesbio_sub_geom_idx ON ecocompensation.tmp_cesbio_sub USING gist (geom);


--
-- Name: troncons_hydros_gix_2154; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_gix_2154 ON ecocompensation.troncons_hydros USING gist (geom_2154);


--
-- Name: troncons_hydros_gix_3857; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_gix_3857 ON ecocompensation.troncons_hydros USING gist (geom_3857);


--
-- Name: troncons_hydros_idx_codehydro; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_idx_codehydro ON ecocompensation.troncons_hydros USING btree (code_hydrographique);


--
-- Name: troncons_hydros_idx_fosse; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_idx_fosse ON ecocompensation.troncons_hydros USING btree (fosse);


--
-- Name: troncons_hydros_idx_nature; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_idx_nature ON ecocompensation.troncons_hydros USING btree (nature);


--
-- Name: troncons_hydros_idx_nom; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX troncons_hydros_idx_nom ON ecocompensation.troncons_hydros USING btree (nom);


--
-- Name: vegetation_sur_cesbio_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX vegetation_sur_cesbio_geom_idx ON ecocompensation.vegetation_sur_cesbio_old USING gist (geom);


--
-- Name: vegetation_sur_cesbio_sub_geom_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX vegetation_sur_cesbio_sub_geom_idx ON ecocompensation.vegetation_sur_cesbio_sub USING gist (geom);


--
-- Name: vegetation_sur_cesbio_v2_geom_gix; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX vegetation_sur_cesbio_v2_geom_gix ON ecocompensation.vegetation_sur_cesbio USING gist (geom);


--
-- Name: vegetation_sur_cesbio_v2_libelle_prio_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX vegetation_sur_cesbio_v2_libelle_prio_idx ON ecocompensation.vegetation_sur_cesbio USING btree (libelle_prio);


--
-- Name: vegetation_sur_cesbio_v2_tile_idx; Type: INDEX; Schema: ecocompensation; Owner: -
--

CREATE INDEX vegetation_sur_cesbio_v2_tile_idx ON ecocompensation.vegetation_sur_cesbio USING btree (tile_idx);


--
-- Name: fragmentation_polygons_aoi_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX fragmentation_polygons_aoi_idx ON ecocompensation_results.fragmentation_polygons USING btree (aoi_id);


--
-- Name: fragmentation_polygons_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX fragmentation_polygons_geom_gix ON ecocompensation_results.fragmentation_polygons USING gist (geom);


--
-- Name: fragmentation_polygons_project_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX fragmentation_polygons_project_idx ON ecocompensation_results.fragmentation_polygons USING btree (project_id);


--
-- Name: idx_ecocomp_results_arrachage_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_arrachage_aoi ON ecocompensation_results.arrachage_vignes USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_arrachage_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_arrachage_geom ON ecocompensation_results.arrachage_vignes USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_arrachage_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_arrachage_project ON ecocompensation_results.arrachage_vignes USING btree (project_id);


--
-- Name: idx_ecocomp_results_bdtopo_cesbio_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_bdtopo_cesbio_geom ON ecocompensation_results.bd_topo_et_cesbio USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_bdtopo_cesbio_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_bdtopo_cesbio_project ON ecocompensation_results.bd_topo_et_cesbio USING btree (project_id);


--
-- Name: idx_ecocomp_results_carhab_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_carhab_geom ON ecocompensation_results.carhab USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_carhab_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_carhab_project ON ecocompensation_results.carhab USING btree (project_id);


--
-- Name: idx_ecocomp_results_cesbio_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_cesbio_geom ON ecocompensation_results.cesbio USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_cesbio_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_cesbio_project ON ecocompensation_results.cesbio USING btree (project_id);


--
-- Name: idx_ecocomp_results_cosia_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_cosia_aoi ON ecocompensation_results.cosia USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_cosia_aoi_tile; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_cosia_aoi_tile ON ecocompensation_results.cosia USING btree (aoi_id, tile_name);


--
-- Name: idx_ecocomp_results_cosia_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_cosia_geom ON ecocompensation_results.cosia USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_ebc_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_ebc_aoi ON ecocompensation_results.ebc USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_ebc_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_ebc_geom ON ecocompensation_results.ebc USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_ebc_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_ebc_project ON ecocompensation_results.ebc USING btree (project_id);


--
-- Name: idx_ecocomp_results_ens_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_ens_geom ON ecocompensation_results.espaces_naturels_sensibles_ens USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_ens_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_ens_project ON ecocompensation_results.espaces_naturels_sensibles_ens USING btree (project_id);


--
-- Name: idx_ecocomp_results_frayeres_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_frayeres_aoi ON ecocompensation_results.frayeres USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_frayeres_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_frayeres_geom ON ecocompensation_results.frayeres USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_frayeres_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_frayeres_project ON ecocompensation_results.frayeres USING btree (project_id);


--
-- Name: idx_ecocomp_results_mnt_dalles_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_mnt_dalles_project ON ecocompensation_results.mnt_dalles USING btree (project_id);


--
-- Name: idx_ecocomp_results_preemption_ens_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_preemption_ens_geom ON ecocompensation_results.preemption_espaces_naturels_sensibles USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_preemption_ens_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_preemption_ens_project ON ecocompensation_results.preemption_espaces_naturels_sensibles USING btree (project_id);


--
-- Name: idx_ecocomp_results_remontee_de_nappes_classefiab; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_remontee_de_nappes_classefiab ON ecocompensation_results.remontee_de_nappes USING btree (classefiab);


--
-- Name: idx_ecocomp_results_remontee_de_nappes_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_remontee_de_nappes_geom ON ecocompensation_results.remontee_de_nappes USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_remontee_de_nappes_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_remontee_de_nappes_project ON ecocompensation_results.remontee_de_nappes USING btree (project_id);


--
-- Name: idx_ecocomp_results_surfaces_hydros_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_surfaces_hydros_geom ON ecocompensation_results.surfaces_hydros USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_surfaces_hydros_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_surfaces_hydros_project ON ecocompensation_results.surfaces_hydros USING btree (project_id);


--
-- Name: idx_ecocomp_results_troncons_hydros_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_troncons_hydros_geom ON ecocompensation_results.troncons_hydros USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_troncons_hydros_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_troncons_hydros_project ON ecocompensation_results.troncons_hydros USING btree (project_id);


--
-- Name: idx_ecocomp_results_zdv_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zdv_aoi ON ecocompensation_results.zone_de_vegetation USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_zdv_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zdv_geom ON ecocompensation_results.zone_de_vegetation USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_zdv_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zdv_project ON ecocompensation_results.zone_de_vegetation USING btree (project_id);


--
-- Name: idx_ecocomp_results_zh_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zh_aoi ON ecocompensation_results.zone_humide USING btree (aoi_id);


--
-- Name: idx_ecocomp_results_zh_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zh_geom ON ecocompensation_results.zone_humide USING gist (geom_2154);


--
-- Name: idx_ecocomp_results_zh_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ecocomp_results_zh_project ON ecocompensation_results.zone_humide USING btree (project_id);


--
-- Name: idx_fauna_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_fauna_geom ON ecocompensation_results.fauna USING gist (geom_2154);


--
-- Name: idx_fauna_nom_vernaculaire_norm; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_fauna_nom_vernaculaire_norm ON ecocompensation_results.fauna USING btree (lower(btrim(nom_vernaculaire)));


--
-- Name: idx_fauna_project_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_fauna_project_id ON ecocompensation_results.fauna USING btree (project_id);


--
-- Name: idx_ilots_rpg_fauna; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ilots_rpg_fauna ON ecocompensation_results.ilots_rpg USING gin (fauna_distances);


--
-- Name: idx_ilots_rpg_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ilots_rpg_geom ON ecocompensation_results.ilots_rpg USING gist (geom_2154);


--
-- Name: idx_ilots_rpg_veg; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ilots_rpg_veg ON ecocompensation_results.ilots_rpg USING gin (veg_libelles);


--
-- Name: idx_mesures_compensatoire_lin_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_lin_aoi ON ecocompensation_results.mesures_compensatoire_lin USING btree (aoi_id);


--
-- Name: idx_mesures_compensatoire_lin_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_lin_geom ON ecocompensation_results.mesures_compensatoire_lin USING gist (geom_2154);


--
-- Name: idx_mesures_compensatoire_lin_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_lin_project ON ecocompensation_results.mesures_compensatoire_lin USING btree (project_id);


--
-- Name: idx_mesures_compensatoire_pct_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_pct_aoi ON ecocompensation_results.mesures_compensatoire_pct USING btree (aoi_id);


--
-- Name: idx_mesures_compensatoire_pct_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_pct_geom ON ecocompensation_results.mesures_compensatoire_pct USING gist (geom_2154);


--
-- Name: idx_mesures_compensatoire_pct_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_pct_project ON ecocompensation_results.mesures_compensatoire_pct USING btree (project_id);


--
-- Name: idx_mesures_compensatoire_surf_aoi; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_surf_aoi ON ecocompensation_results.mesures_compensatoire_surf USING btree (aoi_id);


--
-- Name: idx_mesures_compensatoire_surf_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_surf_geom ON ecocompensation_results.mesures_compensatoire_surf USING gist (geom_2154);


--
-- Name: idx_mesures_compensatoire_surf_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_mesures_compensatoire_surf_project ON ecocompensation_results.mesures_compensatoire_surf USING btree (project_id);


--
-- Name: idx_natura2000_categorie; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_natura2000_categorie ON ecocompensation_results.natura2000 USING btree (natura_categorie);


--
-- Name: idx_natura2000_cd_sig; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_natura2000_cd_sig ON ecocompensation_results.natura2000 USING btree (cd_sig);


--
-- Name: idx_natura2000_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_natura2000_geom ON ecocompensation_results.natura2000 USING gist (geom_2154);


--
-- Name: idx_natura2000_id_mnhn; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_natura2000_id_mnhn ON ecocompensation_results.natura2000 USING btree (id_mnhn);


--
-- Name: idx_natura2000_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_natura2000_project ON ecocompensation_results.natura2000 USING btree (project_id);


--
-- Name: idx_parcelles_geom_2154; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_geom_2154 ON ecocompensation_results.parcelles USING gist (geom_2154);


--
-- Name: idx_parcelles_results_aoi_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_aoi_id ON ecocompensation_results.parcelles USING btree (aoi_id);


--
-- Name: idx_parcelles_results_fauna; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_fauna ON ecocompensation_results.parcelles USING gin (fauna_distances);


--
-- Name: idx_parcelles_results_flora; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_flora ON ecocompensation_results.parcelles USING gin (flora_distances);


--
-- Name: idx_parcelles_results_geom_2154; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_geom_2154 ON ecocompensation_results.parcelles USING gist (geom_2154);


--
-- Name: idx_parcelles_results_idu; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_idu ON ecocompensation_results.parcelles USING btree (idu);


--
-- Name: idx_parcelles_results_project_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_project_id ON ecocompensation_results.parcelles USING btree (project_id);


--
-- Name: idx_parcelles_results_veg; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_parcelles_results_veg ON ecocompensation_results.parcelles USING gin (veg_libelles);


--
-- Name: idx_pool_indesirables_project_run; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_pool_indesirables_project_run ON ecocompensation_results.parcelles_pool_indesirables USING btree (project_id, run_id);


--
-- Name: idx_pool_metrics_project_run_idu; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_pool_metrics_project_run_idu ON ecocompensation_results.parcelles_pool_metrics USING btree (project_id, run_id, idu);


--
-- Name: idx_pool_project_run; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_pool_project_run ON ecocompensation_results.parcelles_pool USING btree (project_id, run_id);


--
-- Name: idx_pool_runs_project_created; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_pool_runs_project_created ON ecocompensation_results.parcelles_pool_runs USING btree (project_id, created_at DESC);


--
-- Name: idx_project_indesirables_project_updated; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_project_indesirables_project_updated ON ecocompensation_results.parcelles_project_indesirables USING btree (project_id, updated_at DESC);


--
-- Name: idx_reserves_naturelles_categorie; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_reserves_naturelles_categorie ON ecocompensation_results.reserves_naturelles USING btree (categorie_reserve);


--
-- Name: idx_reserves_naturelles_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_reserves_naturelles_geom ON ecocompensation_results.reserves_naturelles USING gist (geom_2154);


--
-- Name: idx_reserves_naturelles_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_reserves_naturelles_project ON ecocompensation_results.reserves_naturelles USING btree (project_id);


--
-- Name: idx_sites_classes_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_sites_classes_geom ON ecocompensation_results.sites_classes USING gist (geom_2154);


--
-- Name: idx_sites_classes_project; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_sites_classes_project ON ecocompensation_results.sites_classes USING btree (project_id);


--
-- Name: idx_ss_fauna; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_fauna ON ecocompensation_results.sous_ensembles USING gin (fauna_distances);


--
-- Name: idx_ss_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_geom ON ecocompensation_results.sous_ensembles USING gist (geom_2154);


--
-- Name: idx_ss_k; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_k ON ecocompensation_results.sous_ensembles USING btree (k);


--
-- Name: idx_ss_project_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_project_id ON ecocompensation_results.sous_ensembles USING btree (project_id);


--
-- Name: idx_ss_uf_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_uf_id ON ecocompensation_results.sous_ensembles USING btree (uf_id);


--
-- Name: idx_ss_veg; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_ss_veg ON ecocompensation_results.sous_ensembles USING gin (veg_libelles);


--
-- Name: idx_uf_geom; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_uf_geom ON ecocompensation_results.unites_foncieres USING gist (geom_2154);


--
-- Name: idx_uf_project_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_uf_project_id ON ecocompensation_results.unites_foncieres USING btree (project_id);


--
-- Name: idx_uf_siren; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_uf_siren ON ecocompensation_results.unites_foncieres USING btree (siren);


--
-- Name: idx_uf_uf_id; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX idx_uf_uf_id ON ecocompensation_results.unites_foncieres USING btree (uf_id);


--
-- Name: patrimoine_naturel_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX patrimoine_naturel_geom_gix ON ecocompensation_results.patrimoine_naturel USING gist (geom_2154);


--
-- Name: patrimoine_naturel_project_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX patrimoine_naturel_project_idx ON ecocompensation_results.patrimoine_naturel USING btree (project_id);


--
-- Name: routes_aoi_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX routes_aoi_idx ON ecocompensation_results.routes USING btree (aoi_id);


--
-- Name: routes_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX routes_geom_gix ON ecocompensation_results.routes USING gist (geom_2154);


--
-- Name: sous_ensembles_geom_2154_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX sous_ensembles_geom_2154_idx ON ecocompensation_results.sous_ensembles USING gist (geom_2154);


--
-- Name: sous_ensembles_project_id_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX sous_ensembles_project_id_idx ON ecocompensation_results.sous_ensembles USING btree (project_id);


--
-- Name: sous_ensembles_uf_id_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX sous_ensembles_uf_id_idx ON ecocompensation_results.sous_ensembles USING btree (uf_id);


--
-- Name: urba_entite_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX urba_entite_geom_gix ON ecocompensation_results.urba_entite USING gist (geom_2154);


--
-- Name: urba_parcelle_entite_run_cible_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX urba_parcelle_entite_run_cible_idx ON ecocompensation_results.urba_parcelle_entite USING btree (run_id, cible_id);


--
-- Name: voies_ferrees_aoi_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX voies_ferrees_aoi_idx ON ecocompensation_results.voies_ferrees USING btree (aoi_id);


--
-- Name: voies_ferrees_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX voies_ferrees_geom_gix ON ecocompensation_results.voies_ferrees USING gist (geom_2154);


--
-- Name: znieff_aoi_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX znieff_aoi_idx ON ecocompensation_results.znieff USING btree (aoi_id);


--
-- Name: znieff_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX znieff_geom_gix ON ecocompensation_results.znieff USING gist (geom_2154);


--
-- Name: znieff_project_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX znieff_project_idx ON ecocompensation_results.znieff USING btree (project_id);


--
-- Name: znieff_type_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX znieff_type_idx ON ecocompensation_results.znieff USING btree (znieff_type);


--
-- Name: zones_humides_probables_aoi_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX zones_humides_probables_aoi_idx ON ecocompensation_results.zones_humides_probables USING btree (aoi_id);


--
-- Name: zones_humides_probables_geom_gix; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX zones_humides_probables_geom_gix ON ecocompensation_results.zones_humides_probables USING gist (geom);


--
-- Name: zones_humides_probables_project_idx; Type: INDEX; Schema: ecocompensation_results; Owner: -
--

CREATE INDEX zones_humides_probables_project_idx ON ecocompensation_results.zones_humides_probables USING btree (project_id);


--
-- Name: idx_canopy_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canopy_created ON public.canopy_analysis USING btree (created_at DESC);


--
-- Name: idx_canopy_insee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canopy_insee ON public.canopy_analysis USING btree (insee);


--
-- Name: idx_canopy_parcelle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canopy_parcelle ON public.canopy_analysis USING btree (parcelle_id);


--
-- Name: idx_tiles_layer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tiles_layer ON public.lidar_tiles USING btree (layer_type);


--
-- Name: idx_tiles_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tiles_name ON public.lidar_tiles USING btree (tile_name);


--
-- Name: ppm_geom_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ppm_geom_idx ON public.parcelles_personnes_morales USING gist (geom_2154);


--
-- Name: ppm_idu_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ppm_idu_idx ON public.parcelles_personnes_morales USING btree (idu);


--
-- Name: ppm_siren_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ppm_siren_idx ON public.parcelles_personnes_morales USING btree (siren);


--
-- Name: cadastre_parcelles_16_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_16_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_16_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_16_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_16_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_16_new_geom_idx;


--
-- Name: cadastre_parcelles_16_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_16_new_pkey;


--
-- Name: cadastre_parcelles_17_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_17_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_17_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_17_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_17_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_17_new_geom_idx;


--
-- Name: cadastre_parcelles_17_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_17_new_pkey;


--
-- Name: cadastre_parcelles_19_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_19_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_19_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_19_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_19_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_19_new_geom_idx;


--
-- Name: cadastre_parcelles_19_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_19_new_pkey;


--
-- Name: cadastre_parcelles_23_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_23_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_23_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_23_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_23_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_23_new_geom_idx;


--
-- Name: cadastre_parcelles_23_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_23_new_pkey;


--
-- Name: cadastre_parcelles_24_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_24_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_24_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_24_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_24_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_24_new_geom_idx;


--
-- Name: cadastre_parcelles_24_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_24_new_pkey;


--
-- Name: cadastre_parcelles_33_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_33_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_33_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_33_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_33_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_33_new_geom_idx;


--
-- Name: cadastre_parcelles_33_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_33_new_pkey;


--
-- Name: cadastre_parcelles_40_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_40_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_40_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_40_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_40_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_40_new_geom_idx;


--
-- Name: cadastre_parcelles_40_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_40_new_pkey;


--
-- Name: cadastre_parcelles_47_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_47_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_47_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_47_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_47_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_47_new_geom_idx;


--
-- Name: cadastre_parcelles_47_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_47_new_pkey;


--
-- Name: cadastre_parcelles_64_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_64_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_64_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_64_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_64_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_64_new_geom_idx;


--
-- Name: cadastre_parcelles_64_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_64_new_pkey;


--
-- Name: cadastre_parcelles_79_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_79_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_79_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_79_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_79_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_79_new_geom_idx;


--
-- Name: cadastre_parcelles_79_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_79_new_pkey;


--
-- Name: cadastre_parcelles_86_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_86_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_86_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_86_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_86_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_86_new_geom_idx;


--
-- Name: cadastre_parcelles_86_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_86_new_pkey;


--
-- Name: cadastre_parcelles_87_new_code_insee_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_key_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_87_new_code_insee_section_numero_idx;


--
-- Name: cadastre_parcelles_87_new_com_abs_section_numero_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_abs_idx ATTACH PARTITION ecocompensation.cadastre_parcelles_87_new_com_abs_section_numero_idx;


--
-- Name: cadastre_parcelles_87_new_geom_idx; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_gix ATTACH PARTITION ecocompensation.cadastre_parcelles_87_new_geom_idx;


--
-- Name: cadastre_parcelles_87_new_pkey; Type: INDEX ATTACH; Schema: ecocompensation; Owner: -
--

ALTER INDEX ecocompensation.cadastre_parcelles_pkey ATTACH PARTITION ecocompensation.cadastre_parcelles_87_new_pkey;


--
-- Name: reserve_fonciere reserve_fonciere_touch; Type: TRIGGER; Schema: ecocompensation; Owner: -
--

CREATE TRIGGER reserve_fonciere_touch BEFORE UPDATE ON ecocompensation.reserve_fonciere FOR EACH ROW EXECUTE FUNCTION ecocompensation.touch_updated_at();


--
-- Name: rf_contact rf_contact_touch; Type: TRIGGER; Schema: ecocompensation; Owner: -
--

CREATE TRIGGER rf_contact_touch BEFORE UPDATE ON ecocompensation.rf_contact FOR EACH ROW EXECUTE FUNCTION ecocompensation.touch_updated_at();


--
-- Name: rf_ilot rf_ilot_touch; Type: TRIGGER; Schema: ecocompensation; Owner: -
--

CREATE TRIGGER rf_ilot_touch BEFORE UPDATE ON ecocompensation.rf_ilot FOR EACH ROW EXECUTE FUNCTION ecocompensation.touch_updated_at();


--
-- Name: faune update_faune_updated_at; Type: TRIGGER; Schema: ecocompensation; Owner: -
--

CREATE TRIGGER update_faune_updated_at BEFORE UPDATE ON ecocompensation.faune FOR EACH ROW EXECUTE FUNCTION ecocompensation.update_faune_updated_at();


--
-- Name: flore_observation flore_observation_import_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.flore_observation
    ADD CONSTRAINT flore_observation_import_id_fkey FOREIGN KEY (import_id) REFERENCES ecocompensation.flore_import(id) ON DELETE CASCADE;


--
-- Name: observation_faune observation_faune_import_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.observation_faune
    ADD CONSTRAINT observation_faune_import_id_fkey FOREIGN KEY (import_id) REFERENCES ecocompensation.faune_import(id) ON DELETE CASCADE;


--
-- Name: projects projects_aoi_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projects
    ADD CONSTRAINT projects_aoi_id_fkey FOREIGN KEY (aoi_id) REFERENCES ecocompensation.aoi(id) ON DELETE SET NULL;


--
-- Name: projects projects_foncier_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.projects
    ADD CONSTRAINT projects_foncier_id_fkey FOREIGN KEY (foncier_id) REFERENCES ecocompensation.foncier(id);


--
-- Name: reserve_fonciere reserve_fonciere_ilot_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.reserve_fonciere
    ADD CONSTRAINT reserve_fonciere_ilot_id_fkey FOREIGN KEY (ilot_id) REFERENCES ecocompensation.rf_ilot(id) ON DELETE CASCADE;


--
-- Name: rf_ilot rf_ilot_contact_id_fkey; Type: FK CONSTRAINT; Schema: ecocompensation; Owner: -
--

ALTER TABLE ONLY ecocompensation.rf_ilot
    ADD CONSTRAINT rf_ilot_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES ecocompensation.rf_contact(id) ON DELETE SET NULL;


--
-- Name: faune Lecture publique faune; Type: POLICY; Schema: ecocompensation; Owner: -
--

CREATE POLICY "Lecture publique faune" ON ecocompensation.faune FOR SELECT USING (true);


--
-- Name: faune Modification utilisateurs auth; Type: POLICY; Schema: ecocompensation; Owner: -
--

CREATE POLICY "Modification utilisateurs auth" ON ecocompensation.faune USING ((auth.role() = 'authenticated'::text));


--
-- Name: data_interne_layer_notes; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.data_interne_layer_notes ENABLE ROW LEVEL SECURITY;

--
-- Name: faune; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.faune ENABLE ROW LEVEL SECURITY;

--
-- Name: faune_import; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.faune_import ENABLE ROW LEVEL SECURITY;

--
-- Name: flore_import; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.flore_import ENABLE ROW LEVEL SECURITY;

--
-- Name: flore_observation; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.flore_observation ENABLE ROW LEVEL SECURITY;

--
-- Name: geomce_lin; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.geomce_lin ENABLE ROW LEVEL SECURITY;

--
-- Name: geomce_pct; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.geomce_pct ENABLE ROW LEVEL SECURITY;

--
-- Name: geomce_surf; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.geomce_surf ENABLE ROW LEVEL SECURITY;

--
-- Name: observation_faune; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.observation_faune ENABLE ROW LEVEL SECURITY;

--
-- Name: pm_prospectables_raffinee_cesbio; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.pm_prospectables_raffinee_cesbio ENABLE ROW LEVEL SECURITY;

--
-- Name: rf_contact; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.rf_contact ENABLE ROW LEVEL SECURITY;

--
-- Name: rf_ilot; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.rf_ilot ENABLE ROW LEVEL SECURITY;

--
-- Name: tmp_bdtopo_sub; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.tmp_bdtopo_sub ENABLE ROW LEVEL SECURITY;

--
-- Name: tmp_cesbio_sub; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.tmp_cesbio_sub ENABLE ROW LEVEL SECURITY;

--
-- Name: vegetation_sur_cesbio_sub; Type: ROW SECURITY; Schema: ecocompensation; Owner: -
--

ALTER TABLE ecocompensation.vegetation_sur_cesbio_sub ENABLE ROW LEVEL SECURITY;

--
-- Name: surfaces_hydros; Type: ROW SECURITY; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ecocompensation_results.surfaces_hydros ENABLE ROW LEVEL SECURITY;

--
-- Name: troncons_hydros; Type: ROW SECURITY; Schema: ecocompensation_results; Owner: -
--

ALTER TABLE ecocompensation_results.troncons_hydros ENABLE ROW LEVEL SECURITY;

--
-- Name: canopy_analysis; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.canopy_analysis ENABLE ROW LEVEL SECURITY;

--
-- Name: canopy_analysis canopy_public_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY canopy_public_insert ON public.canopy_analysis FOR INSERT WITH CHECK (true);


--
-- Name: canopy_analysis canopy_public_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY canopy_public_read ON public.canopy_analysis FOR SELECT USING (true);


--
-- Name: canopy_analysis canopy_public_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY canopy_public_update ON public.canopy_analysis FOR UPDATE USING (true);


--
-- Name: lidar_tiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lidar_tiles ENABLE ROW LEVEL SECURITY;

--
-- Name: lidar_tiles tiles_public_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tiles_public_all ON public.lidar_tiles USING (true) WITH CHECK (true);


--
-- PostgreSQL database dump complete
--



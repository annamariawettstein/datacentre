-- UK data centre pipeline — data spine (Phase 0)
--
-- One table, `application`, holding every planning application PlanIt returns for
-- our keyword sweep, plus the analysis columns the later phases populate. PostGIS
-- geometry is here from day one so Phase 3 spatial joins to GSP / DNO regions need
-- no migration.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS application (
    -- Identity (PlanIt `name` is unique nationally on their system).
    name                     text PRIMARY KEY,
    uid                      text,
    altid                    text,
    reference                text,
    associated_id            text,

    -- Authority / source.
    scraper_name             text,
    area_id                  integer,
    area_name                text,

    -- Core application fields (all knowable pre-decision except decided_date).
    description              text,
    address                  text,
    postcode                 text,
    app_size                 text,   -- Small | Medium | Large (PlanIt band)
    app_state                text,   -- Undecided | Permitted | Rejected | ...
    app_type                 text,   -- Full | Outline | Conditions | ...

    -- Dates.
    start_date               date,
    decided_date             date,
    consulted_date           date,
    last_scraped             timestamptz,
    last_changed             timestamptz,
    last_different           timestamptz,

    -- Portal + PlanIt links.
    url                      text,   -- council portal
    link                     text,   -- PlanIt page

    -- Geometry (WGS84). NULL where the application has no location.
    geom                     geometry(Point, 4326),

    -- Everything else PlanIt gives us, verbatim.
    other_fields             jsonb,
    raw                      jsonb NOT NULL,

    -- Provenance for this pipeline.
    first_seen               timestamptz NOT NULL DEFAULT now(),  -- set once, on insert
    last_ingested            timestamptz NOT NULL DEFAULT now(),

    -- Phase 0 classification (LLM pass over description).
    is_datacentre            boolean,
    classification_confidence real,
    classification_reason    text,
    classification_model     text,
    classified_at            timestamptz,

    -- Precision/scale audit: a genuine data centre may still be a hospital server
    -- room. dc_material flags facilities that plausibly draw significant grid power
    -- (hyperscale / colocation / large enterprise) — the ones that matter for the gap.
    dc_category              text,   -- hyperscale|colocation|enterprise|edge_telecom|ancillary_small|not_datacentre
    dc_material              boolean,
    dc_category_confidence   real,
    dc_category_reason       text,
    dc_category_at           timestamptz,

    -- Phase 2 capacity extraction.
    capacity_mw              real,
    capacity_source          text CHECK (capacity_source IN ('stated', 'extracted', 'inferred')),

    -- Phase 1 survival model.
    p_built                  real,
    p_built_features         jsonb,   -- top-3 drivers, for the map to explain itself
    p_built_model            text,
    p_built_at               timestamptz,

    -- Applicant/agent resolved by scraping the council portal (PlanIt only carries
    -- a "See source" placeholder for the applicant). See enrich.py.
    applicant                text,
    applicant_address        text,
    agent_resolved           text,
    applicant_source         text,    -- portal system the value came from (e.g. 'idox')
    applicant_scraped_at     timestamptz,

    -- Phase 3 denominator.
    gsp                      text,    -- Grid Supply Point the site maps to

    -- Site-level dedupe: applications sharing a physical scheme get one site_id
    -- (see dedupe.py — union-find over postcode / proximity / associated_id / address).
    site_id                  text
);

-- Phase 2 energy-profile extraction (capacity.py). `capacity_mw` / `capacity_source`
-- above hold the single headline figure the Phase 3 denominator sums; the columns
-- below hold the full extracted profile plus its provenance (which document, which
-- model, verbatim evidence). Added via ALTER so an already-populated spine picks them
-- up without a rebuild.
ALTER TABLE application
    ADD COLUMN IF NOT EXISTS it_load_mw                   real,     -- IT/compute load
    ADD COLUMN IF NOT EXISTS grid_connection_mw           real,     -- utility supply capacity
    ADD COLUMN IF NOT EXISTS headline_capacity_mw         real,     -- stated overall "NN MW data centre"
    ADD COLUMN IF NOT EXISTS grid_connection_voltage_kv   real,
    -- Prime generation runs the site (gas turbine / CHP / behind-the-meter); distinct
    -- from standby generation that only fires on a grid outage. Keeping them apart
    -- matters: prime generation offsets grid draw, backup does not.
    ADD COLUMN IF NOT EXISTS prime_generation_type        text,     -- gas|chp|other|none|unknown
    ADD COLUMN IF NOT EXISTS prime_generation_mw          real,
    ADD COLUMN IF NOT EXISTS backup_generation_type       text,     -- diesel|gas|other|none|unknown
    ADD COLUMN IF NOT EXISTS backup_generation_mw         real,
    ADD COLUMN IF NOT EXISTS backup_generator_count       integer,
    ADD COLUMN IF NOT EXISTS bess_present                 boolean,
    ADD COLUMN IF NOT EXISTS bess_mwh                     real,
    ADD COLUMN IF NOT EXISTS onsite_solar                 boolean,
    ADD COLUMN IF NOT EXISTS solar_mwp                    real,
    ADD COLUMN IF NOT EXISTS onsite_wind                  boolean,
    ADD COLUMN IF NOT EXISTS gross_floor_area_sqm         real,
    ADD COLUMN IF NOT EXISTS energy_evidence              jsonb,    -- verbatim quotes, per figure
    ADD COLUMN IF NOT EXISTS energy_extraction_confidence real,
    ADD COLUMN IF NOT EXISTS energy_doc_url               text,     -- PDF the figures came from
    ADD COLUMN IF NOT EXISTS energy_doc_title             text,
    ADD COLUMN IF NOT EXISTS capacity_model               text,
    ADD COLUMN IF NOT EXISTS capacity_extracted_at        timestamptz;

CREATE INDEX IF NOT EXISTS application_geom_idx        ON application USING gist (geom);
CREATE INDEX IF NOT EXISTS application_area_idx        ON application (area_name);
CREATE INDEX IF NOT EXISTS application_start_date_idx  ON application (start_date);
CREATE INDEX IF NOT EXISTS application_is_dc_idx       ON application (is_datacentre);
CREATE INDEX IF NOT EXISTS application_app_state_idx   ON application (app_state);
CREATE INDEX IF NOT EXISTS application_site_idx        ON application (site_id);

-- Log of each sweep run, so a broken LPA / partial run degrades visibly rather
-- than silently (Phase 0 risk note: scraping is fragile).
CREATE TABLE IF NOT EXISTS sweep_run (
    id           bigserial PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    mode         text,             -- 'full' | 'incremental'
    query        text,
    total_found  integer,
    inserted     integer,
    updated      integer,
    error        text
);

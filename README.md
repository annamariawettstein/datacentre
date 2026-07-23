# UK data centre pipeline

A map of the UK data-centre planning pipeline: every scheme in the planning system, its
status, its power demand, and the on-site generation, storage and renewables it brings —
built from a national planning sweep, LLM classification, deduplication to distinct
schemes, and capacity/energy extraction from the application documents.

## Phases

| Phase | What | Status |
|---|---|---|
| **Data spine** | PlanIt sweep → Postgres/PostGIS, LLM classification | ✓ built |
| **Dedup** | collapse applications into distinct physical sites | ✓ built |
| **Capacity & energy** | LLM over D&AS / EIA / energy PDFs → MW + on-site generation profile; floor-area→MW fallback | ✓ built (Idox portals) |
| **Surface** | MapLibre map + stats, coloured by planning status, on-site-generation filters | ✓ built |
| **Grid context** | map sites to Grid Supply Point, sum `capacity_mw` vs DNO headroom | ▫ planned |

## Setup

Requires Postgres 16+ with PostGIS, and Python 3.11+ (`uv` recommended).

```bash
# 1. Database (macOS/Homebrew: postgresql@18 + postgis; see db notes)
createdb datacentre

# 2. Python env
uv venv --python 3.12
uv pip install -e .

# 3. Config
cp .env.example .env      # set DATABASE_URL and ANTHROPIC_API_KEY

# 4. Schema
uv run datacentre init-db
```

## Usage

```bash
uv run datacentre sweep                 # full national keyword sweep into Postgres
uv run datacentre sweep --incremental 7 # cron mode: only records changed in last 7 days
uv run datacentre classify --limit 500  # LLM is_datacentre pass over unclassified rows
uv run datacentre dedupe                # collapse applications into distinct sites
uv run datacentre stats                 # summarise the database
uv run datacentre serve                 # launch the map + stats monitor (http://127.0.0.1:8000)
```

### Pipeline monitor UI

`datacentre serve` runs a FastAPI app (`src/datacentre/api.py`) serving the live data
(`/api/stats`, `/api/sites.geojson`) and a MapLibre dashboard in `web/` — a dark grid
control-room view with site nodes coloured by planning status, a pulsing live-pipeline
layer, status filters, and click-through site detail. The basemap uses CARTO dark tiles
(no key, needs internet); site nodes render from the local database.

The classifier is provider-agnostic: set `GEMINI_API_KEY` (default, uses
`gemini-2.5-flash`) or `ANTHROPIC_API_KEY` in `.env`. The provider is inferred from
whichever key is present (Gemini preferred); override with `CLASSIFY_PROVIDER`.

### Cron

The `sweep --incremental N` command is idempotent and cron-friendly. `first_seen` is
stamped once per record and never overwritten, so it survives re-runs and drives the
map's time dimension. Example crontab (daily incremental at 03:00):

```
0 3 * * * cd /path/to/datacentre && .venv/bin/datacentre sweep --incremental 2 >> sweep.log 2>&1
```

## Data model

Everything lives in one `application` table (`db/schema.sql`): the raw PlanIt fields, a
PostGIS `geom` point, provenance (`first_seen`, `last_ingested`), and the analysis columns
each phase fills in (`is_datacentre`, `dc_category`/`dc_material`, `capacity_mw`/
`capacity_source`, the on-site energy-profile columns, `site_id`, `gsp`). The full PlanIt
record is retained verbatim in `raw` JSONB. A `sweep_run` table logs every run so a partial
or failed sweep is visible rather than silent.

## Data sources

- **PlanIt** (planit.org.uk) — ~420 LPAs, free API, no key. The data spine.
- **Council planning portals** (Idox / PublicAccess) — application documents (energy / EIA
  / planning statements) for capacity and on-site-generation extraction.
- **DNO open data portals** (UKPN/SSEN/NGED/SPEN/NPg), **NESO** — grid headroom + GSP
  boundaries (planned grid-context phase).

See `.context/attachments/` for the full brief and the PlanIt API reference.

# UK data centre pipeline

Two artefacts sharing one data spine:

- **A** — a UK data centre pipeline map where each site carries a *probability it gets built* (`p_built`)
- **B** — a probability-weighted regional power-gap figure: pipeline demand vs. grid headroom, by Grid Supply Point

B depends on A. Survival weighting is what makes the headline gap defensible rather than a raw-queue press release.

## Phases

| Phase | What | Status |
|---|---|---|
| **0** | Data spine — PlanIt sweep → Postgres/PostGIS, LLM classification | ⏳ in progress |
| **1** | Survival model — LightGBM on all large commercial/industrial apps, applied to the DC subset, temporal validation | ▫ planned |
| **2** | Capacity extraction — Claude over D&AS / EIA / energy PDFs → MW; floor-area→MW fallback | ▫ planned |
| **3** | Denominator — map sites to GSP, sum `capacity_mw × p_built` vs DNO headroom | ▫ planned |
| **4** | Surface — MapLibre + PMTiles, coloured by `p_built`, time slider over `first_seen`, methodology page | ▫ planned |

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
Phase 4 time slider. Example crontab (daily incremental at 03:00):

```
0 3 * * * cd /path/to/datacentre && .venv/bin/datacentre sweep --incremental 2 >> sweep.log 2>&1
```

## Data model

Everything lives in one `application` table (`db/schema.sql`): the raw PlanIt fields, a
PostGIS `geom` point, provenance (`first_seen`, `last_ingested`), and the analysis columns
each later phase fills in (`is_datacentre`, `capacity_mw`/`capacity_source`, `p_built`, `gsp`).
The full PlanIt record is retained verbatim in `raw` JSONB. A `sweep_run` table logs every
run so a partial or failed sweep is visible rather than silent.

## Data sources

- **PlanIt** (planit.org.uk) — ~420 LPAs, free API, no key. The Phase 0 spine.
- **planning.data.gov.uk** — green belt, conservation, flood, local plan boundaries (Phase 1 features).
- **Companies House** — SPV → parent resolution (Phase 1 feature).
- **DNO open data portals** (UKPN/SSEN/NGED/SPEN/NPg), **NESO** — grid headroom + GSP boundaries (Phase 3).

See `.context/attachments/` for the full brief and the PlanIt API reference.

"""Web API + static site for the pipeline monitor (Phase 4 surface, early cut).

Serves the deduped, classified data straight from Postgres:
  GET /api/stats          — headline figures for the readout panels
  GET /api/sites.geojson  — one point per distinct site, for the map
  GET /                   — the monitor UI (web/)
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import config

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(title="UK Data Centre Grid — Pipeline Monitor")


def _conn() -> psycopg.Connection:
    return psycopg.connect(config.database_url)


# One representative, geocoded application per distinct site. Generation/storage flags
# are aggregated across the whole site (bool_or over the partition) so a flag set on the
# application that carried the documents still shows on the representative row.
_SITES_SQL = """
WITH ranked AS (
    SELECT site_id, area_name, app_state, app_type, description, link,
           other_fields->>'agent_company' AS agent,
           ST_X(geom) AS lng, ST_Y(geom) AS lat,
           count(*)       OVER (PARTITION BY site_id) AS n_apps,
           min(start_date) OVER (PARTITION BY site_id) AS first_seen,
           bool_or(backup_generation_type IS NOT NULL AND backup_generation_type <> 'none')
               OVER (PARTITION BY site_id) AS gen_backup,
           bool_or(prime_generation_type IS NOT NULL AND prime_generation_type <> 'none')
               OVER (PARTITION BY site_id) AS gen_prime,
           bool_or(bess_present)  OVER (PARTITION BY site_id) AS gen_bess,
           bool_or(onsite_solar)  OVER (PARTITION BY site_id) AS gen_solar,
           bool_or(onsite_wind)   OVER (PARTITION BY site_id) AS gen_wind,
           row_number()   OVER (PARTITION BY site_id ORDER BY
               (geom IS NOT NULL) DESC,
               CASE app_type WHEN 'Full' THEN 0 WHEN 'Outline' THEN 1 ELSE 2 END,
               start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre
)
SELECT site_id, area_name, app_state, app_type, n_apps, first_seen,
       lng, lat, left(description, 260) AS description, link, agent,
       gen_backup, gen_prime, gen_bess, gen_solar, gen_wind
FROM ranked
WHERE rn = 1 AND lng IS NOT NULL
"""


@app.get("/api/sites.geojson")
def sites_geojson() -> JSONResponse:
    with _conn() as conn:
        rows = conn.execute(_SITES_SQL).fetchall()
    cols = [
        "site_id", "area_name", "app_state", "app_type", "n_apps",
        "first_seen", "lng", "lat", "description", "link", "agent",
        "gen_backup", "gen_prime", "gen_bess", "gen_solar", "gen_wind",
    ]
    features = []
    for row in rows:
        r = dict(zip(cols, row))
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {
                "site_id": r["site_id"],
                "area": r["area_name"],
                "state": r["app_state"] or "Unknown",
                "type": r["app_type"],
                "n_apps": r["n_apps"],
                "year": r["first_seen"].year if r["first_seen"] else None,
                "agent": r["agent"],
                "description": r["description"],
                "link": r["link"],
                "backup": bool(r["gen_backup"]),
                "prime": bool(r["gen_prime"]),
                "bess": bool(r["gen_bess"]),
                "solar": bool(r["gen_solar"]),
                "wind": bool(r["gen_wind"]),
            },
        })
    return JSONResponse({"type": "FeatureCollection", "features": features})


@app.get("/api/stats")
def stats() -> JSONResponse:
    with _conn() as conn:
        apps, sites, mapped = conn.execute(
            """SELECT count(*), count(DISTINCT site_id),
                      count(DISTINCT site_id) FILTER (WHERE geom IS NOT NULL)
               FROM application WHERE is_datacentre"""
        ).fetchone()

        # Site-level outcome via the primary (Full/Outline) application.
        outcome = dict(conn.execute(
            """WITH p AS (
                   SELECT app_state, row_number() OVER (
                       PARTITION BY site_id ORDER BY start_date DESC NULLS LAST) rn
                   FROM application
                   WHERE is_datacentre AND app_type IN ('Full','Outline'))
               SELECT coalesce(app_state,'Unknown'), count(*) FROM p WHERE rn=1
               GROUP BY 1"""
        ).fetchall())

        refused = outcome.get("Rejected", 0)
        withdrawn = outcome.get("Withdrawn", 0)
        approved = outcome.get("Permitted", 0) + outcome.get("Conditions", 0)
        # "Undecided" sites split by whether the SITE has ever been decided. A site with
        # a live application but an earlier consent isn't new pipeline — it's a follow-up
        # (variation, reserved matters, condition discharge) on an already-decided scheme.
        undec = conn.execute(
            """WITH u AS (
                   SELECT DISTINCT site_id FROM application
                   WHERE is_datacentre AND app_state = 'Undecided')
               SELECT
                 count(*),
                 count(*) FILTER (WHERE NOT EXISTS (
                     SELECT 1 FROM application a WHERE a.site_id = u.site_id
                       AND a.app_state IN ('Permitted','Conditions','Rejected','Withdrawn'))),
                 count(*) FILTER (WHERE EXISTS (
                     SELECT 1 FROM application a WHERE a.site_id = u.site_id
                       AND a.app_state IN ('Permitted','Conditions','Rejected','Withdrawn')))
               FROM u"""
        ).fetchone()
        undecided_sites, undecided_new, undecided_followup = undec[0], undec[1], undec[2]

        material_sites = conn.execute(
            "SELECT count(DISTINCT site_id) FROM application "
            "WHERE is_datacentre AND dc_material"
        ).fetchone()[0]

        median_days = conn.execute(
            """SELECT round(percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY decided_date - (other_fields->>'date_validated')::date))
               FROM application
               WHERE is_datacentre AND app_type IN ('Full','Outline')
                 AND app_state IN ('Permitted','Conditions')
                 AND decided_date IS NOT NULL
                 AND (other_fields->>'date_validated') IS NOT NULL"""
        ).fetchone()[0]

        top_areas = conn.execute(
            """SELECT area_name, count(DISTINCT site_id) c
               FROM application WHERE is_datacentre
               GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""
        ).fetchall()

        # Lapsed consents: sites whose permission expired with NO later activity of
        # any kind — an upper bound on abandoned data-centre consents.
        lapsed_dead = conn.execute(
            """WITH expired AS (
                   SELECT name, site_id, start_date AS granted_start
                   FROM application
                   WHERE is_datacentre
                     AND (other_fields->>'permission_expires_date') ~ '^[0-9]{4}-'
                     AND (other_fields->>'permission_expires_date')::date < current_date
               ), classified AS (
                   SELECT e.site_id,
                     EXISTS (SELECT 1 FROM application a
                             WHERE a.site_id = e.site_id AND a.name <> e.name
                               AND a.start_date > e.granted_start) AS later_any
                   FROM expired e
               )
               SELECT count(*) FROM (
                   SELECT site_id FROM classified GROUP BY site_id
                   HAVING bool_or(later_any) = false
               ) t"""
        ).fetchone()[0]

        by_year = conn.execute(
            """WITH s AS (
                   SELECT site_id, extract(year FROM min(start_date))::int yr
                   FROM application WHERE is_datacentre AND start_date IS NOT NULL
                   GROUP BY site_id)
               SELECT yr, count(*) FROM s WHERE yr >= 2010 GROUP BY yr ORDER BY yr"""
        ).fetchall()

        # Phase 2 capacity, collapsed to one figure per distinct site. A site with
        # both a doc-stated MW and a floor-area estimate counts as `stated` (the
        # trustworthy figure wins); sites with only an estimate are reported
        # separately so the two are never conflated in the headline.
        cap = conn.execute(
            """WITH per_site AS (
                   SELECT site_id,
                     (array_agg(capacity_mw ORDER BY (capacity_source='stated') DESC,
                                capacity_mw DESC)
                        FILTER (WHERE capacity_mw IS NOT NULL))[1] AS mw,
                     bool_or(capacity_source='stated') AS has_stated
                   FROM application
                   WHERE is_datacentre AND capacity_mw IS NOT NULL
                   GROUP BY site_id)
               SELECT
                 count(*) FILTER (WHERE has_stated),
                 round(sum(mw) FILTER (WHERE has_stated)),
                 count(*) FILTER (WHERE NOT has_stated),
                 round(sum(mw) FILTER (WHERE NOT has_stated))
               FROM per_site"""
        ).fetchone()

        # On-site power the schemes bring themselves — generation, storage, renewables —
        # deduped to sites. Counts, plus summed capacity where the documents state it.
        emix = conn.execute(
            """SELECT
                   count(*) FILTER (WHERE backup),
                   count(*) FILTER (WHERE diesel),
                   round(sum(backup_mw)),
                   count(*) FILTER (WHERE prime),
                   round(sum(prime_mw)),
                   count(*) FILTER (WHERE bess),
                   round(sum(bess_mwh)),
                   count(*) FILTER (WHERE solar),
                   round(sum(solar_mwp)::numeric, 1),
                   count(*) FILTER (WHERE wind),
                   count(*) FILTER (WHERE backup OR prime OR bess OR solar OR wind)
               FROM (
                   SELECT site_id,
                     bool_or(backup_generation_type NOT IN ('none')
                             AND backup_generation_type IS NOT NULL) backup,
                     bool_or(backup_generation_type = 'diesel') diesel,
                     max(backup_generation_mw) backup_mw,
                     bool_or(prime_generation_type NOT IN ('none')
                             AND prime_generation_type IS NOT NULL) prime,
                     max(prime_generation_mw) prime_mw,
                     bool_or(bess_present) bess, max(bess_mwh) bess_mwh,
                     bool_or(onsite_solar) solar, max(solar_mwp) solar_mwp,
                     bool_or(onsite_wind) wind
                   FROM application WHERE is_datacentre GROUP BY site_id) t"""
        ).fetchone()

    refusal_pct = round(100 * refused / max(approved + refused, 1), 1)
    withdrawal_pct = round(
        100 * withdrawn / max(approved + refused + withdrawn, 1), 1)

    return JSONResponse({
        "applications": apps,
        "sites": sites,
        "mapped_sites": mapped,
        "approved": approved,
        "refused": refused,
        "withdrawn": withdrawn,
        "undecided_sites": undecided_sites,
        "undecided_new": undecided_new,
        "undecided_followup": undecided_followup,
        "material_sites": material_sites,
        "refusal_pct": refusal_pct,
        "withdrawal_pct": withdrawal_pct,
        "median_days": int(median_days) if median_days is not None else None,
        "lapsed_dead_sites": lapsed_dead,
        "outcome": outcome,
        "top_areas": [{"area": a, "sites": c} for a, c in top_areas],
        "by_year": [{"year": y, "sites": c} for y, c in by_year],
        "capacity": {
            "stated_sites": cap[0] or 0,
            "stated_mw": int(cap[1]) if cap[1] is not None else 0,
            "extracted_sites": cap[2] or 0,
            "extracted_mw": int(cap[3]) if cap[3] is not None else 0,
        },
        "generation": {
            "any_sites": emix[10] or 0,
            "backup_sites": emix[0] or 0,
            "diesel_sites": emix[1] or 0,
            "backup_mw": int(emix[2]) if emix[2] is not None else 0,
            "prime_sites": emix[3] or 0,
            "prime_mw": int(emix[4]) if emix[4] is not None else 0,
            "bess_sites": emix[5] or 0,
            "bess_mwh": int(emix[6]) if emix[6] is not None else 0,
            "solar_sites": emix[7] or 0,
            "solar_mwp": float(emix[8]) if emix[8] is not None else 0,
            "wind_sites": emix[9] or 0,
        },
    })


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

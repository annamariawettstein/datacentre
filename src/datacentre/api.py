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


# One representative, geocoded application per distinct site.
_SITES_SQL = """
WITH ranked AS (
    SELECT site_id, area_name, app_state, app_type, description, link,
           other_fields->>'agent_company' AS agent,
           ST_X(geom) AS lng, ST_Y(geom) AS lat,
           count(*)       OVER (PARTITION BY site_id) AS n_apps,
           min(start_date) OVER (PARTITION BY site_id) AS first_seen,
           row_number()   OVER (PARTITION BY site_id ORDER BY
               (geom IS NOT NULL) DESC,
               CASE app_type WHEN 'Full' THEN 0 WHEN 'Outline' THEN 1 ELSE 2 END,
               start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre
)
SELECT site_id, area_name, app_state, app_type, n_apps, first_seen,
       lng, lat, left(description, 260) AS description, link, agent
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
        undecided_sites = conn.execute(
            "SELECT count(DISTINCT site_id) FROM application "
            "WHERE is_datacentre AND app_state='Undecided'"
        ).fetchone()[0]

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
        "material_sites": material_sites,
        "refusal_pct": refusal_pct,
        "withdrawal_pct": withdrawal_pct,
        "median_days": int(median_days) if median_days is not None else None,
        "lapsed_dead_sites": lapsed_dead,
        "outcome": outcome,
        "top_areas": [{"area": a, "sites": c} for a, c in top_areas],
        "by_year": [{"year": y, "sites": c} for y, c in by_year],
    })


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

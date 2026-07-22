"""Phase 0 sweep: pull PlanIt records into Postgres.

Idempotent and cron-friendly. `first_seen` is stamped once, on the first insert
of a record, and never overwritten — that column is the backbone of the Phase 4
time slider. A `full` sweep walks the whole keyword query; an `incremental` sweep
only pulls records PlanIt has changed in the last N days.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import psycopg

from .config import config
from .keywords import search_expression
from .planit import PlanItClient

# Commit every N upserts so a mid-sweep failure leaves partial progress in place
# (and doesn't lose the whole run) — the "degrade quietly" requirement.
_COMMIT_EVERY = 200

# Columns we lift out of the PlanIt record into typed columns. Everything is also
# retained verbatim in `raw` (and the misc bag in `other_fields`).
_UPSERT_SQL = """
INSERT INTO application (
    name, uid, altid, reference, associated_id,
    scraper_name, area_id, area_name,
    description, address, postcode,
    app_size, app_state, app_type,
    start_date, decided_date, consulted_date,
    last_scraped, last_changed, last_different,
    url, link,
    geom,
    other_fields, raw,
    last_ingested
) VALUES (
    %(name)s, %(uid)s, %(altid)s, %(reference)s, %(associated_id)s,
    %(scraper_name)s, %(area_id)s, %(area_name)s,
    %(description)s, %(address)s, %(postcode)s,
    %(app_size)s, %(app_state)s, %(app_type)s,
    %(start_date)s, %(decided_date)s, %(consulted_date)s,
    %(last_scraped)s, %(last_changed)s, %(last_different)s,
    %(url)s, %(link)s,
    CASE WHEN %(lng)s::float8 IS NULL OR %(lat)s::float8 IS NULL
         THEN NULL
         ELSE ST_SetSRID(ST_MakePoint(%(lng)s::float8, %(lat)s::float8), 4326) END,
    %(other_fields)s, %(raw)s,
    now()
)
ON CONFLICT (name) DO UPDATE SET
    uid = EXCLUDED.uid,
    altid = EXCLUDED.altid,
    reference = EXCLUDED.reference,
    associated_id = EXCLUDED.associated_id,
    scraper_name = EXCLUDED.scraper_name,
    area_id = EXCLUDED.area_id,
    area_name = EXCLUDED.area_name,
    description = EXCLUDED.description,
    address = EXCLUDED.address,
    postcode = EXCLUDED.postcode,
    app_size = EXCLUDED.app_size,
    app_state = EXCLUDED.app_state,
    app_type = EXCLUDED.app_type,
    start_date = EXCLUDED.start_date,
    decided_date = EXCLUDED.decided_date,
    consulted_date = EXCLUDED.consulted_date,
    last_scraped = EXCLUDED.last_scraped,
    last_changed = EXCLUDED.last_changed,
    last_different = EXCLUDED.last_different,
    url = EXCLUDED.url,
    link = EXCLUDED.link,
    geom = EXCLUDED.geom,
    other_fields = EXCLUDED.other_fields,
    raw = EXCLUDED.raw,
    last_ingested = now()
    -- first_seen deliberately untouched
RETURNING (xmax = 0) AS inserted
"""


def _clean(value):
    """PlanIt uses '' and 'None' as nulls in places; normalise to None."""
    if value in ("", "None", "null"):
        return None
    return value


def _row(rec: dict) -> dict:
    loc = rec.get("location") or {}
    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    lng, lat = (coords[0], coords[1]) if coords and len(coords) >= 2 else (None, None)
    # Fall back to the flat location_x / location_y fields if present.
    lng = lng if lng is not None else rec.get("location_x")
    lat = lat if lat is not None else rec.get("location_y")

    return {
        "name": rec["name"],
        "uid": _clean(rec.get("uid")),
        "altid": _clean(rec.get("altid")),
        "reference": _clean(rec.get("reference")),
        "associated_id": _clean(rec.get("associated_id")),
        "scraper_name": _clean(rec.get("scraper_name")),
        "area_id": rec.get("area_id"),
        "area_name": _clean(rec.get("area_name")),
        "description": _clean(rec.get("description")),
        "address": _clean(rec.get("address")),
        "postcode": _clean(rec.get("postcode")),
        "app_size": _clean(rec.get("app_size")),
        "app_state": _clean(rec.get("app_state")),
        "app_type": _clean(rec.get("app_type")),
        "start_date": _clean(rec.get("start_date")),
        "decided_date": _clean(rec.get("decided_date")),
        "consulted_date": _clean(rec.get("consulted_date")),
        "last_scraped": _clean(rec.get("last_scraped")),
        "last_changed": _clean(rec.get("last_changed")),
        "last_different": _clean(rec.get("last_different")),
        "url": _clean(rec.get("url")),
        "link": _clean(rec.get("link")),
        "lng": lng,
        "lat": lat,
        "other_fields": json.dumps(rec.get("other_fields") or {}),
        "raw": json.dumps(rec),
    }


@dataclass
class SweepResult:
    total_found: int
    inserted: int
    updated: int


def _upsert_all(conn: psycopg.Connection, records: Iterable[dict]) -> SweepResult:
    inserted = updated = total = 0
    with conn.cursor() as cur:
        for rec in records:
            total += 1
            cur.execute(_UPSERT_SQL, _row(rec))
            if cur.fetchone()[0]:
                inserted += 1
            else:
                updated += 1
            if total % _COMMIT_EVERY == 0:
                conn.commit()
    conn.commit()
    return SweepResult(total_found=total, inserted=inserted, updated=updated)


def sweep(
    *,
    incremental_days: int | None = None,
    terms: list[str] | None = None,
    contact: str | None = None,
) -> SweepResult:
    """Run a keyword sweep and upsert results.

    incremental_days: if set, only pull records PlanIt changed within this many
    days (cheap cron mode). Otherwise walk the full keyword query.

    The sweep-run log and the upserts are committed independently so a mid-sweep
    failure still records the error and keeps whatever was ingested so far.
    """
    query = search_expression(terms)
    params: dict = {"search": query}
    mode = "full"
    if incremental_days is not None:
        params["changed"] = incremental_days
        mode = "incremental"

    conn = psycopg.connect(config.database_url, autocommit=False)
    try:
        run = conn.execute(
            "INSERT INTO sweep_run (mode, query) VALUES (%s, %s) RETURNING id",
            (mode, query),
        ).fetchone()[0]
        conn.commit()
        try:
            with PlanItClient(contact=contact) as client:
                result = _upsert_all(conn, client.search(params))
            conn.execute(
                """UPDATE sweep_run SET finished_at = now(), total_found = %s,
                   inserted = %s, updated = %s WHERE id = %s""",
                (result.total_found, result.inserted, result.updated, run),
            )
            conn.commit()
            return result
        except Exception as exc:
            conn.rollback()  # drop the failed in-flight upsert only
            conn.execute(
                "UPDATE sweep_run SET finished_at = now(), error = %s WHERE id = %s",
                (str(exc), run),
            )
            conn.commit()
            raise
    finally:
        conn.close()

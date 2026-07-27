"""Grid-connection context from a GridScope substations export.

The GridScope database (a private Copia Supabase export — NOT in this repo) carries UK
primary-substation headroom: per substation a location, its Grid Supply Point (`gsp`),
and demand/generation headroom in MW. This module loads that one table and matches each
geolocated data-centre site to its nearest primary substation, populating `gsp` and the
`grid_*` columns on `application`.

Usage: `datacentre load-grid <path-to-backup-dir-or-.sql.gz>`. The GSP-level rollup
(pipeline demand vs summed headroom) is the "Grid context" section of db/analysis.sql.

Caveats worth remembering: the export covers only some DNOs; nearest-primary is a proxy
for the real connection point (large data centres connect at GSP/transmission, above
primary level); and headroom is a point-in-time snapshot.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import psycopg

from .config import config

SUBSTATIONS = "uk_primary_substations"

_CREATE = f"""
DROP TABLE IF EXISTS {SUBSTATIONS};
CREATE TABLE {SUBSTATIONS} (
  id text, name text, lat double precision, lng double precision,
  geom geometry(Point,4326), dno text, type text, voltage text, transformers text,
  location text, commissioned text, licence text,
  demand_headroom_mw double precision, gen_headroom_mw double precision,
  demand_rag text, gen_rag text, bsp text, gsp text,
  max_demand_mva double precision, contracted_bess_mva double precision,
  demand_constraint text, gen_constraint text
);
"""

# Nearest primary substation per geolocated site, via the GiST KNN operator. Computed in
# a CTE first because Postgres won't let an UPDATE target be referenced inside its own
# FROM. Distance in metres via geography; the <-> ordering stays planar (index-backed).
_MATCH = f"""
WITH matched AS (
  SELECT a.name,
    (SELECT u.id FROM {SUBSTATIONS} u ORDER BY a.geom <-> u.geom LIMIT 1) AS uid
  FROM application a WHERE a.is_datacentre AND a.geom IS NOT NULL
)
UPDATE application a SET
  gsp = u.gsp, grid_substation = u.name, grid_dno = u.dno,
  grid_dist_m = ST_Distance(a.geom::geography, u.geom::geography),
  grid_demand_headroom_mw = u.demand_headroom_mw,
  grid_gen_headroom_mw = u.gen_headroom_mw, grid_demand_rag = u.demand_rag
FROM matched m JOIN {SUBSTATIONS} u ON u.id = m.uid
WHERE a.name = m.name
"""


def _find_dump(src: str) -> Path:
    """Resolve `src` to the primary-substations dump (accepts the backup dir or the file)."""
    p = Path(src).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        cand = p / "tables" / f"{SUBSTATIONS}.sql.gz"
        if cand.exists():
            return cand
        found = next(p.rglob(f"{SUBSTATIONS}.sql.gz"), None)
        if found:
            return found
    raise FileNotFoundError(f"no {SUBSTATIONS}.sql.gz found at {src}")


def load_grid(src: str) -> tuple[int, int]:
    """Load the substations dump and match sites. Returns (substations, sites_matched)."""
    dump = _find_dump(src)
    conn = psycopg.connect(config.database_url)
    try:
        conn.execute(_CREATE)
        conn.commit()

        # Stream the COPY block straight from the gzipped pg_dump into a COPY.
        with gzip.open(dump, "rt") as f:
            cols = None
            for line in f:
                if line.startswith("COPY ") and SUBSTATIONS in line:
                    cols = line[line.index("(") + 1 : line.index(")")]
                    break
            if cols is None:
                raise RuntimeError(f"no COPY block for {SUBSTATIONS} in {dump.name}")
            with conn.cursor() as cur, cur.copy(
                f"COPY {SUBSTATIONS} ({cols}) FROM STDIN"
            ) as cp:
                for line in f:
                    if line.startswith("\\."):
                        break
                    cp.write(line)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ups_geom_idx ON {SUBSTATIONS} USING gist (geom)"
        )
        conn.commit()
        n_subs = conn.execute(f"SELECT count(*) FROM {SUBSTATIONS}").fetchone()[0]

        matched = conn.execute(_MATCH).rowcount
        conn.commit()
        return n_subs, matched
    finally:
        conn.close()

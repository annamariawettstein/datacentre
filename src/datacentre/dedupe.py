"""Collapse applications into distinct physical sites (`site_id`).

A single data centre generates many planning applications over its life — the
original scheme, condition discharges, non-material amendments, reserved matters.
Counting applications as sites would multiply-count a scheme's megawatts, so
before any capacity/gap figure we group applications that refer to the same
physical development.

Method: union-find (connected components) over five linkage rules, applied only
to confirmed data centres. Two applications are the same site if ANY hold:

  1. same full postcode (normalised) — national, so it also merges schemes that
     are cross-listed under a development corporation *and* its host borough
     (e.g. Old Oak Park Royal vs Ealing/Brent).
  2. their locations are within 180 m of each other (PostGIS ST_DWithin).
  3. one's `associated_id` is the other's `uid` in the same authority — the
     portal's own explicit parent/child link.
  4. same normalised street address (length-gated to avoid trivial matches).
  5. same address *prefix* AND same description *prefix* — catches the same scheme
     consulted across a council boundary (app types ADJLPA / OA / NAC / cross-border
     EIA), which carry no postcode or geometry so rules 1-2 can't see them, and whose
     address differs only in a trailing county/postcode so rule 4's exact match fails.
     Requiring both a long address prefix and a long description prefix keeps this
     precise (two distinct schemes sharing both is implausible).

The method favours precision (not merging genuinely distinct sites) over recall:
leftover duplicates are transparent, whereas over-merging would silently deflate
the site count and inflate per-site capacity.
"""

from __future__ import annotations

import re
from collections import defaultdict

import psycopg

from .config import config

# ~180 m: comfortably groups a single campus's filings without swallowing the
# neighbouring plot in dense clusters like Park Royal / Slough.
PROXIMITY_METRES = 180
# Only trust an address match once it's specific enough to be a real address.
MIN_ADDRESS_LEN = 12
# Rule 5 (cross-boundary consultations): both prefixes must be this long before the
# pair is even considered — a 28-char land-parcel address prefix plus a 150-char
# description prefix is distinctive enough that a false merge is implausible.
ADDR_PREFIX_LEN = 28
DESC_PREFIX_LEN = 150


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _norm_postcode(pc: str | None) -> str | None:
    if not pc:
        return None
    s = re.sub(r"\s+", "", pc).upper()
    return s or None


def _norm_address(addr: str | None) -> str | None:
    if not addr:
        return None
    s = re.sub(r"[^a-z0-9]", "", addr.lower())
    return s if len(s) >= MIN_ADDRESS_LEN else None


def _prefix_key(text: str | None, n: int) -> str | None:
    """Normalised first `n` alphanumeric chars, or None if the source is too short.

    Used by Rule 5: a prefix (rather than the whole string) so a scheme's address
    still matches when a trailing county / postcode differs between authorities, and
    a description still matches when one authority appends a full stop.
    """
    if not text:
        return None
    s = re.sub(r"[^a-z0-9]", "", text.lower())
    return s[:n] if len(s) >= n else None


def dedupe() -> tuple[int, int]:
    """Assign site_id to every confirmed data centre. Returns (apps, sites)."""
    conn = psycopg.connect(config.database_url)
    try:
        rows = conn.execute(
            """SELECT name, uid, area_name, postcode, address, associated_id, description
               FROM application WHERE is_datacentre"""
        ).fetchall()
        names = [r[0] for r in rows]
        uf = _UnionFind(names)

        by_postcode: dict[str, list[str]] = defaultdict(list)
        by_address: dict[str, list[str]] = defaultdict(list)
        by_scheme: dict[tuple[str, str], list[str]] = defaultdict(list)  # Rule 5 key
        uid_to_name: dict[tuple, str] = {}
        assoc: list[tuple[str, tuple]] = []

        for name, uid, area, postcode, address, associated_id, description in rows:
            if uid:
                uid_to_name[(area, uid)] = name
            pc = _norm_postcode(postcode)
            if pc:
                by_postcode[pc].append(name)
            ad = _norm_address(address)
            if ad:
                by_address[ad].append(name)
            if associated_id:
                assoc.append((name, (area, associated_id)))
            # Rule 5 key: only formed when BOTH prefixes are long enough to be specific.
            addr_pre = _prefix_key(address, ADDR_PREFIX_LEN)
            desc_pre = _prefix_key(description, DESC_PREFIX_LEN)
            if addr_pre and desc_pre:
                by_scheme[(addr_pre, desc_pre)].append(name)

        # Rules 1, 4 & 5: union within each shared postcode / address / scheme group.
        for group in (
            list(by_postcode.values())
            + list(by_address.values())
            + list(by_scheme.values())
        ):
            for other in group[1:]:
                uf.union(group[0], other)

        # Rule 3: explicit parent/child links.
        for child, key in assoc:
            parent = uid_to_name.get(key)
            if parent:
                uf.union(child, parent)

        # Rule 2: spatial proximity (done in PostGIS, only geocoded rows).
        pairs = conn.execute(
            """SELECT a.name, b.name
               FROM application a JOIN application b
                 ON a.name < b.name
                AND ST_DWithin(a.geom::geography, b.geom::geography, %s)
               WHERE a.is_datacentre AND b.is_datacentre
                 AND a.geom IS NOT NULL AND b.geom IS NOT NULL""",
            (PROXIMITY_METRES,),
        ).fetchall()
        for a, b in pairs:
            uf.union(a, b)

        # Assign each component a stable site_id (its root application name).
        with conn.cursor() as cur:
            for name in names:
                cur.execute(
                    "UPDATE application SET site_id = %s WHERE name = %s",
                    (uf.find(name), name),
                )
        conn.commit()

        sites = len({uf.find(n) for n in names})
        return len(names), sites
    finally:
        conn.close()

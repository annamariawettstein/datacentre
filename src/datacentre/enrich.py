"""Resolve the applicant (and agent) from council portals.

PlanIt only carries a "See source" placeholder for the applicant — the actual
name sits on the council's portal page behind the stored `url`. This module
scrapes it. Idox ("online-applications" / PublicAccess) is the dominant, most
consistent system (~half of all sites) and is the only one supported here;
everything else degrades quietly (skipped, logged), per the brief's guidance
that portal scraping is fragile and a broken LPA must not fail the run.

Public planning-record data only. Polite: identifies itself, rate-limits, and
never hammers a portal.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass

import httpx
import psycopg

from .config import config

REQUEST_DELAY_SECS = 1.5
IDOX_MARKERS = ("online-applications", "applicationDetails.do")


@dataclass
class PortalRecord:
    applicant: str | None
    applicant_address: str | None
    agent: str | None
    source: str


def _is_idox(url: str) -> bool:
    return all(m in url for m in ("applicationDetails.do",)) and (
        "online-applications" in url or "publicaccess" in url
    )


def _details_url(url: str) -> str:
    """Point an Idox application URL at its Details tab (where applicant lives)."""
    if "activeTab=" in url:
        return re.sub(r"activeTab=[A-Za-z]+", "activeTab=details", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}activeTab=details"


def _parse_idox(page: str) -> PortalRecord:
    """Extract the <th>label</th><td>value</td> pairs Idox uses on the Details tab."""
    pairs: dict[str, str] = {}
    for m in re.finditer(
        r"<th[^>]*>\s*(.*?)\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.I | re.S
    ):
        key = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip().lower()
        val = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if key and val:
            pairs[key] = val

    def pick(*labels: str) -> str | None:
        for label in labels:
            if label in pairs and pairs[label] not in ("", "-"):
                return pairs[label][:500]
        return None

    return PortalRecord(
        applicant=pick("applicant name", "applicant"),
        applicant_address=pick("applicant address"),
        agent=pick("agent name", "agent company name", "agent"),
        source="idox",
    )


def _fetch(client: httpx.Client, url: str) -> PortalRecord | None:
    resp = client.get(_details_url(url))
    if resp.status_code != 200 or "Applicant" not in resp.text:
        return None
    return _parse_idox(resp.text)


# One representative application per target site that we can actually scrape
# (Idox URL, not yet resolved). Prefer the principal Full/Outline filing.
_SELECT = """
WITH targets AS (
    SELECT name, site_id, url,
           row_number() OVER (PARTITION BY site_id ORDER BY
               CASE app_type WHEN 'Full' THEN 0 WHEN 'Outline' THEN 1 ELSE 2 END,
               start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre AND applicant_scraped_at IS NULL
      AND url ~ 'applicationDetails.do'
      AND (url ~ 'online-applications' OR url ~ 'publicaccess')
      {site_filter}
)
SELECT name, url FROM targets WHERE rn = 1 LIMIT %(limit)s
"""

# Sites whose permission lapsed with no later activity — the highest-value set.
_LAPSED_FILTER = """
  AND site_id IN (
    WITH expired AS (
        SELECT name, site_id, start_date g FROM application
        WHERE is_datacentre
          AND (other_fields->>'permission_expires_date') ~ '^[0-9]{4}-'
          AND (other_fields->>'permission_expires_date')::date < current_date)
    SELECT site_id FROM (
        SELECT e.site_id,
          EXISTS (SELECT 1 FROM application a
                  WHERE a.site_id = e.site_id AND a.name <> e.name
                    AND a.start_date > e.g) la
        FROM expired e) x
    GROUP BY site_id HAVING bool_or(la) = false)
"""


def resolve_applicants(*, lapsed: bool = False, limit: int = 50) -> tuple[int, int]:
    """Scrape applicant/agent for target sites. Returns (attempted, resolved)."""
    sql = _SELECT.format(site_filter=_LAPSED_FILTER if lapsed else "")
    conn = psycopg.connect(config.database_url)
    client = httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "uk-datacentre-pipeline applicant resolver "
                f"(contact: {config.planit_contact})"
            )
        },
    )
    attempted = resolved = 0
    try:
        rows = conn.execute(sql, {"limit": limit}).fetchall()
        for name, url in rows:
            attempted += 1
            try:
                rec = _fetch(client, url)
            except Exception as exc:
                print(f"  ! {name}: {exc}")
                rec = None
            if rec and rec.applicant:
                conn.execute(
                    """UPDATE application SET applicant = %s, applicant_address = %s,
                         agent_resolved = %s, applicant_source = %s,
                         applicant_scraped_at = now() WHERE name = %s""",
                    (rec.applicant, rec.applicant_address, rec.agent, rec.source, name),
                )
                resolved += 1
                print(f"  ✓ {rec.applicant}")
            else:
                # Mark as attempted so we don't retry endlessly; source records why.
                conn.execute(
                    """UPDATE application SET applicant_source = 'unresolved',
                         applicant_scraped_at = now() WHERE name = %s""",
                    (name,),
                )
            conn.commit()
            time.sleep(REQUEST_DELAY_SECS)
        return attempted, resolved
    finally:
        conn.close()
        client.close()

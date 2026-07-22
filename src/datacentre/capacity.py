"""Phase 2: capacity & energy-profile extraction from planning PDFs.

The description tells us a site is a data centre; it almost never tells us how much
power it draws — only ~2% of descriptions state an MW figure. That number lives in
the application documents (Energy Statements, EIA screening/scoping reports, Planning
Statements) as PDFs behind the council portal. This module:

  1. finds the documents tab for a site's principal application (Idox only, like
     enrich.py — the dominant, most-consistent portal system),
  2. ranks the document list towards the ones that carry energy figures,
  3. hands the top few PDFs to the LLM, which reads them natively and returns a
     structured, *quote-grounded* energy profile,
  4. merges the docs, derives the single `capacity_mw` the Phase 3 denominator needs,
     and writes the lot back to `application`.

Every numeric figure is stored with the verbatim sentence it came from
(`energy_evidence`), so the pipeline is auditable rather than a black box.

Public planning-record data only. Polite: identifies itself, rate-limits, and tolerates
a broken LPA quietly (logged, skipped) rather than failing the batch — portal scraping
is fragile by nature.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
from dataclasses import dataclass

import httpx
import psycopg

from .classify import GEMINI_URL
from .config import config

REQUEST_DELAY_SECS = 1.5
MAX_PDF_BYTES = 15 * 1024 * 1024  # skip huge scans; inline PDF has request-size limits
DEFAULT_MAX_DOCS = 3              # top-ranked PDFs to read per site

# Fallback when only a floor area is stated: MW ≈ gross_floor_area × density.
# CALIBRATED (2026-07-21) from the 32 `stated` sites that also carry a floor area:
# after removing 9 floor/MW mismatches (density outside 0.4–3.0 kW/m²), the plausible
# band gives median 1.12, mean 1.29, and aggregate ΣMW/Σm² = 1.20 kW/m² — IT-load and
# grid-connection sites agree at ~1.1. 1.15 kW/m² sits between the robust median and the
# aggregate ratio. Always flagged capacity_source='extracted'; note the floor area itself
# is the dominant error source (~28% of even stated sites had a mismatched area).
DENSITY_MW_PER_SQM = 0.00115

# Document-title ranking. Higher tier = more likely to carry power/energy figures.
# Matched case-insensitively as substrings of the whole table-row text.
_DOC_TIERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (10, ("energy statement", "energy strategy", "energy and sustainability",
          "sustainability statement", "energy report")),
    (9,  ("environmental statement", "air quality", "eia", "screening", "scoping",
          "environmental impact")),
    (8,  ("planning statement", "design and access", "planning support",
          "planning, design and access")),
    (6,  ("utilities", "infrastructure", "engineering services", "services strategy",
          "electrical")),
    (3,  ("application form", "proposal of application", "supporting statement",
          "covering letter", "pan")),
)
# Pure drawings / boilerplate never carry the figures — drop them unless they also
# hit a positive keyword above.
_DOC_NEGATIVE = (
    "location plan", "site plan", "block plan", "elevation", "floor plan", "section",
    "landscape", "drawing", "red line", "redline", "photograph", "site notice",
    "certificate", "decision notice", "consultation", "neighbour", "comment",
)


@dataclass
class EnergyProfile:
    it_load_mw: float | None = None
    grid_connection_mw: float | None = None
    headline_capacity_mw: float | None = None
    grid_connection_voltage_kv: float | None = None
    prime_generation_type: str | None = None
    prime_generation_mw: float | None = None
    backup_generation_type: str | None = None
    backup_generation_mw: float | None = None
    backup_generator_count: int | None = None
    bess_present: bool | None = None
    bess_mwh: float | None = None
    onsite_solar: bool | None = None
    solar_mwp: float | None = None
    onsite_wind: bool | None = None
    gross_floor_area_sqm: float | None = None
    evidence: list[str] = None            # type: ignore[assignment]
    confidence: float = 0.0
    doc_url: str | None = None
    doc_title: str | None = None

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


SYSTEM_PROMPT = """You extract the ENERGY & POWER profile of a proposed UK data centre \
from a planning-application document.

Rules:
- Extract ONLY figures explicitly stated in the document. Never infer, estimate, or \
convert. If a value is not stated, use null.
- CRITICAL — demand, not generation. Capture only the DATA CENTRE's own power DEMAND: \
its IT load, total electrical load, or grid IMPORT / connection capacity. Do NOT record \
electricity the site GENERATES FOR EXPORT to the grid (energy-from-waste plants, power \
stations, standalone renewable generators exporting to the network) as grid_connection_mw \
or any other demand field — that is supply, not demand. "prime_generation_mw" is on-site \
generation used to SUPPLY the data centre's load, never power exported to the grid.
- If the document is not actually about a data centre's power consumption (e.g. it \
describes a waste-incineration or power-generation scheme), leave every MW field null.
- For every numeric figure you return, add the verbatim sentence/phrase it came from to \
"evidence_quotes".
- Distinguish PRIME generation (on-site plant that runs the facility's load — gas \
turbines, CHP, "behind-the-meter" generation) from BACKUP/STANDBY generation (emergency \
generators, usually diesel, that only run on a grid outage). Do not conflate them.
- "it_load_mw" is the IT/compute load. "grid_connection_mw" is the utility supply / grid \
connection capacity (often stated as ~49.9 MW to stay under the 50 MW NSIP/DCO threshold, \
or much higher). "headline_capacity_mw" is any stated overall figure like "a 75 MW data \
centre" or "200 MW demand" that is NOT clearly labelled as IT load or grid connection.

Return ONLY this JSON object (numbers as plain numbers, no units):
{
  "it_load_mw": number|null,
  "grid_connection_mw": number|null,
  "headline_capacity_mw": number|null,
  "grid_connection_voltage_kv": number|null,
  "prime_generation_type": "gas"|"chp"|"other"|"none"|"unknown",
  "prime_generation_mw": number|null,
  "backup_generation_type": "diesel"|"gas"|"other"|"none"|"unknown",
  "backup_generation_mw": number|null,
  "backup_generator_count": number|null,
  "bess_present": true|false|null,
  "bess_mwh": number|null,
  "onsite_solar": true|false|null,
  "solar_mwp": number|null,
  "onsite_wind": true|false|null,
  "gross_floor_area_sqm": number|null,
  "evidence_quotes": ["<verbatim snippet>", ...],
  "extraction_confidence": number
}"""


# One representative application per target site we can actually scrape (Idox, not yet
# extracted). Prefer the principal Full/Outline filing — that's where the substantive
# documents sit. Mirrors enrich.py's target selection.
_SELECT = """
WITH targets AS (
    SELECT name, site_id, url,
           row_number() OVER (PARTITION BY COALESCE(site_id, name) ORDER BY
               CASE app_type WHEN 'Full' THEN 0 WHEN 'Outline' THEN 1 ELSE 2 END,
               start_date DESC NULLS LAST) AS rn
    FROM application
    WHERE is_datacentre
      AND (%(force)s OR capacity_extracted_at IS NULL)
      AND (NOT %(material_only)s OR dc_material)
      AND url ~ 'applicationDetails.do'
      AND (url ~ 'online-applications' OR url ~ 'publicaccess')
)
SELECT name, url FROM targets WHERE rn = 1 LIMIT %(limit)s
"""


# -- portal / document plumbing -------------------------------------------------

def _docs_url(url: str) -> str:
    """Point an Idox application URL at its Documents tab."""
    if "activeTab=" in url:
        return re.sub(r"activeTab=[A-Za-z]+", "activeTab=documents", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}activeTab=documents"


def _origin(url: str) -> str:
    u = httpx.URL(url)
    return f"{u.scheme}://{u.host}" + (f":{u.port}" if u.port else "")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _clean_title(row_text: str) -> str:
    """Turn a raw Idox document-row into a readable title.

    Rows carry the "Select this document" link text, a publish date, a doc-type and a
    description; strip the boilerplate and the leading date so the stored title is the
    document's actual name.
    """
    t = html.unescape(_strip_tags(row_text))
    t = re.sub(r"\b(Select this document|View|Download)\b", "", t, flags=re.I)
    t = re.sub(r"^\s*\d{1,2}\s+\w{3,9}\s+\d{4}\s*", "", t)  # leading "31 Oct 2013"
    return re.sub(r"\s+", " ", t).strip(" -")[:200]


def _score_doc(title: str) -> int:
    t = title.lower()
    score = 0
    for tier, kws in _DOC_TIERS:
        if any(k in t for k in kws):
            score = max(score, tier)
    if score == 0 and any(n in t for n in _DOC_NEGATIVE):
        return -1
    return score


def _rank_documents(page_html: str, max_docs: int) -> list[tuple[str, str]]:
    """Parse the Idox documents table into (title, pdf_path) rows, best first.

    Only the first results page is parsed; very large applications paginate their
    document list and the tail is not followed (logged by the caller when it matters).
    """
    seen: set[str] = set()
    scored: list[tuple[int, str, str]] = []
    # Each document row contains a direct link to /online-applications/files/<hash>/pdf/…
    for m in re.finditer(
        r"<tr[^>]*>(.*?)</tr>", page_html, re.I | re.S
    ):
        row = m.group(1)
        link = re.search(r'href="([^"]*/files/[^"]+\.pdf)"', row, re.I)
        if not link:
            continue
        path = link.group(1)
        if path in seen:
            continue
        seen.add(path)
        title = _clean_title(row) or path.rsplit("/", 1)[-1]
        s = _score_doc(title)
        if s < 0:
            continue
        scored.append((s, title, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(t, p) for _, t, p in scored[:max_docs]]


def _open_documents(url: str) -> tuple[httpx.Client, str, list[tuple[str, str]]] | None:
    """Fetch a site's documents tab, returning (client, origin, ranked_docs).

    The returned client already holds the session cookie the file links require, so the
    caller must use *this* client to download and close it when done. Portals with a
    broken TLS chain (missing intermediate) are retried without verification — public
    read-only data, and several LPA portals ship incomplete chains.
    """
    headers = {
        "User-Agent": (
            f"uk-datacentre-pipeline capacity extractor (contact: {config.planit_contact})"
        )
    }
    for verify in (True, False):
        client = httpx.Client(
            timeout=60.0, follow_redirects=True, headers=headers, verify=verify
        )
        try:
            resp = client.get(_docs_url(url))
        except httpx.ConnectError as exc:
            client.close()
            if verify and re.search(r"certificate|ssl", str(exc), re.I):
                continue  # retry insecurely
            raise
        except Exception:
            client.close()
            raise
        if resp.status_code != 200 or "/files/" not in resp.text:
            client.close()
            return None
        docs = _rank_documents(resp.text, DEFAULT_MAX_DOCS)
        if not docs:
            client.close()
            return None
        return client, _origin(url), docs
    return None


def _download_pdf(client: httpx.Client, origin: str, path: str) -> bytes | None:
    resp = client.get(origin + path)
    body = resp.content
    if resp.status_code != 200 or body[:4] != b"%PDF" or len(body) > MAX_PDF_BYTES:
        return None
    return body


# -- extraction ----------------------------------------------------------------

def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        if m:
            return float(m.group(0))
    return None


def _enum(v, allowed: tuple[str, ...]) -> str | None:
    if isinstance(v, str) and v.strip().lower() in allowed:
        val = v.strip().lower()
        return None if val == "unknown" else val
    return None


def _extract_pdf(http: httpx.Client, pdf: bytes) -> EnergyProfile:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf",
                             "data": base64.standard_b64encode(pdf).decode()}},
            {"text": "Extract the energy profile from this document."},
        ]}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "temperature": 0,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    for attempt in range(1, 6):
        resp = http.post(
            GEMINI_URL.format(model=config.classify_model),
            params={"key": config.gemini_api_key},
            json=payload,
        )
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", min(2 ** attempt, 30))))
            continue
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        d = json.loads(text)
        quotes = d.get("evidence_quotes") or []
        cnt = _num(d.get("backup_generator_count"))
        return EnergyProfile(
            it_load_mw=_num(d.get("it_load_mw")),
            grid_connection_mw=_num(d.get("grid_connection_mw")),
            headline_capacity_mw=_num(d.get("headline_capacity_mw")),
            grid_connection_voltage_kv=_num(d.get("grid_connection_voltage_kv")),
            prime_generation_type=_enum(
                d.get("prime_generation_type"), ("gas", "chp", "other", "none", "unknown")
            ),
            prime_generation_mw=_num(d.get("prime_generation_mw")),
            backup_generation_type=_enum(
                d.get("backup_generation_type"),
                ("diesel", "gas", "other", "none", "unknown"),
            ),
            backup_generation_mw=_num(d.get("backup_generation_mw")),
            backup_generator_count=int(cnt) if cnt is not None else None,
            bess_present=d.get("bess_present") if isinstance(d.get("bess_present"), bool) else None,
            bess_mwh=_num(d.get("bess_mwh")),
            onsite_solar=d.get("onsite_solar") if isinstance(d.get("onsite_solar"), bool) else None,
            solar_mwp=_num(d.get("solar_mwp")),
            onsite_wind=d.get("onsite_wind") if isinstance(d.get("onsite_wind"), bool) else None,
            gross_floor_area_sqm=_num(d.get("gross_floor_area_sqm")),
            evidence=[str(q)[:300] for q in quotes if q][:12],
            # Clamp: models occasionally emit a 0-10 or 0-100 confidence.
            confidence=min(max(_num(d.get("extraction_confidence")) or 0.0, 0.0), 1.0),
        )
    raise RuntimeError("Gemini rate-limited after 5 attempts")


_NUMERIC_FIELDS = (
    "it_load_mw", "grid_connection_mw", "headline_capacity_mw",
    "grid_connection_voltage_kv", "prime_generation_mw", "backup_generation_mw",
    "backup_generator_count", "bess_mwh", "solar_mwp", "gross_floor_area_sqm",
)
_BOOL_FIELDS = ("bess_present", "onsite_solar", "onsite_wind")
_ENUM_FIELDS = ("prime_generation_type", "backup_generation_type")


def _merge(docs: list[tuple[str, EnergyProfile]]) -> EnergyProfile:
    """Combine per-document profiles: first non-null wins (docs already best-first).

    Evidence accumulates across docs; the doc that first supplied a headline capacity
    figure is recorded as the source, else the top-ranked doc that yielded anything.
    """
    merged = EnergyProfile()
    source_title = source_kind = None
    for title, p in docs:
        for f in _NUMERIC_FIELDS + _BOOL_FIELDS + _ENUM_FIELDS:
            if getattr(merged, f) is None and getattr(p, f) is not None:
                setattr(merged, f, getattr(p, f))
        merged.evidence = list(dict.fromkeys(merged.evidence + p.evidence))[:12]
        merged.confidence = max(merged.confidence, p.confidence)
        if source_title is None and (
            p.it_load_mw or p.grid_connection_mw or p.headline_capacity_mw
            or p.gross_floor_area_sqm
        ):
            source_title = title
    merged.doc_title = source_title or (docs[0][0] if docs else None)
    return merged


def _derive_capacity(p: EnergyProfile) -> tuple[float | None, str | None]:
    """The single figure Phase 3 sums, plus how we know it.

    Preference: IT load > grid connection > stated headline (all 'stated'), then a
    floor-area estimate ('extracted'). Grid connection is the truest proxy for GSP
    headroom consumed, but IT load is stated far more often and the two are close for
    planning purposes.
    """
    for val in (p.it_load_mw, p.grid_connection_mw, p.headline_capacity_mw):
        if val:
            return val, "stated"
    if p.gross_floor_area_sqm:
        return round(p.gross_floor_area_sqm * DENSITY_MW_PER_SQM, 1), "extracted"
    return None, None


def _write(conn: psycopg.Connection, name: str, p: EnergyProfile) -> None:
    cap_mw, cap_src = _derive_capacity(p)
    conn.execute(
        """UPDATE application SET
             capacity_mw = %s, capacity_source = %s,
             it_load_mw = %s, grid_connection_mw = %s, headline_capacity_mw = %s,
             grid_connection_voltage_kv = %s,
             prime_generation_type = %s, prime_generation_mw = %s,
             backup_generation_type = %s, backup_generation_mw = %s,
             backup_generator_count = %s,
             bess_present = %s, bess_mwh = %s,
             onsite_solar = %s, solar_mwp = %s, onsite_wind = %s,
             gross_floor_area_sqm = %s,
             energy_evidence = %s, energy_extraction_confidence = %s,
             energy_doc_url = %s, energy_doc_title = %s,
             capacity_model = %s, capacity_extracted_at = now()
           WHERE name = %s""",
        (
            cap_mw, cap_src,
            p.it_load_mw, p.grid_connection_mw, p.headline_capacity_mw,
            p.grid_connection_voltage_kv,
            p.prime_generation_type, p.prime_generation_mw,
            p.backup_generation_type, p.backup_generation_mw,
            p.backup_generator_count,
            p.bess_present, p.bess_mwh,
            p.onsite_solar, p.solar_mwp, p.onsite_wind,
            p.gross_floor_area_sqm,
            json.dumps(p.evidence), p.confidence,
            p.doc_url, p.doc_title,
            config.classify_model, name,
        ),
    )


def extract_capacity(
    *, limit: int = 50, force: bool = False, material_only: bool = True,
    max_docs: int = DEFAULT_MAX_DOCS,
) -> tuple[int, int]:
    """Extract energy profiles for up to `limit` target sites.

    Returns (attempted, extracted) where extracted = sites that yielded any figure.
    Sites with no scrapable documents are marked done (so they aren't retried forever)
    but count as attempted-only.
    """
    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    conn = psycopg.connect(config.database_url)
    http = httpx.Client(timeout=180.0)
    attempted = extracted = 0
    try:
        rows = conn.execute(
            _SELECT, {"limit": limit, "force": force, "material_only": material_only}
        ).fetchall()
        for name, url in rows:
            attempted += 1
            try:
                profile = _process_site(conn, http, url, max_docs)
            except Exception as exc:  # never let one portal kill the batch
                print(f"  ! {name}: {exc}")
                profile = None

            if profile is not None:
                _write(conn, name, profile)
                cap, src = _derive_capacity(profile)
                if cap is not None:
                    extracted += 1
                    print(f"  ✓ {name}: {cap} MW ({src})"
                          + (f", {profile.doc_title[:50]}" if profile.doc_title else ""))
                else:
                    print(f"  · {name}: docs read, no figure stated")
            else:
                # No scrapable Idox docs — record the attempt so we don't loop on it.
                conn.execute(
                    "UPDATE application SET capacity_source = 'inferred', "
                    "capacity_extracted_at = now() WHERE name = %s AND capacity_mw IS NULL",
                    (name,),
                )
                print(f"  – {name}: no readable documents")
            conn.commit()
            time.sleep(REQUEST_DELAY_SECS)
        return attempted, extracted
    finally:
        conn.close()
        http.close()


def _process_site(
    conn: psycopg.Connection, http: httpx.Client, url: str, max_docs: int
) -> EnergyProfile | None:
    opened = _open_documents(url)
    if opened is None:
        return None
    client, origin, docs = opened
    try:
        results: list[tuple[str, EnergyProfile]] = []
        first_url = None
        for title, path in docs[:max_docs]:
            pdf = _download_pdf(client, origin, path)
            if pdf is None:
                continue
            if first_url is None:
                first_url = origin + path
            prof = _extract_pdf(http, pdf)
            prof.doc_url = origin + path
            results.append((title, prof))
            time.sleep(REQUEST_DELAY_SECS)
    finally:
        client.close()

    if not results:
        return None
    merged = _merge(results)
    # Point doc_url at whichever doc supplied the source figure, else the first read.
    for title, prof in results:
        if title == merged.doc_title:
            merged.doc_url = prof.doc_url
            break
    else:
        merged.doc_url = first_url
    return merged

"""Scale/type audit over the confirmed data centres.

The is_datacentre pass answers a binary that's mostly right — but "data centre"
spans a 200 MW hyperscale campus and a hospital's server cupboard. Only the
power-material facilities matter for grid demand. This pass assigns each confirmed
site a category and a `dc_material` flag, catching the residual false positives
(telecom exchanges, ancillary server rooms) at the same time.
"""

from __future__ import annotations

import json
import time

import httpx
import psycopg

from .config import config
from .classify import GEMINI_URL

CATEGORIES = (
    "hyperscale", "colocation", "enterprise",
    "edge_telecom", "ancillary_small", "not_datacentre",
)

SYSTEM_PROMPT = """You audit UK planning applications ALREADY flagged as data centres, to
separate genuine power-material data centres from small/ancillary/telecom facilities and
false positives. Judge from the description.

Assign exactly ONE category:
- "hyperscale": purpose-built large data centre campus (cloud/AI/hyperscale), tens to
  hundreds of MW.
- "colocation": commercial multi-tenant / merchant data centre.
- "enterprise": an organisation's own dedicated data centre building (bank, corporate).
- "edge_telecom": telecom exchange, fibre/broadband facility, edge node, comms cabinet.
- "ancillary_small": a server/comms/equipment room inside a building whose primary use is
  something else (hospital, university, office IT, single small unit).
- "not_datacentre": not actually a data centre.

Also set "power_material" (boolean): true ONLY for a purpose-built data centre likely to
draw significant grid power (roughly >5 MW) — i.e. hyperscale, colocation, or a large
dedicated enterprise data centre. false for edge_telecom, ancillary_small, not_datacentre,
and any minor/small enterprise server room.

Respond with JSON ONLY: {"category": <one of the above>, "power_material": bool,
"confidence": float 0-1, "reason": "<= 20 words"}."""

_SELECT = """
SELECT name, description, app_type, app_size, area_name, applicant
FROM application
WHERE is_datacentre AND (%(force)s OR dc_category IS NULL)
ORDER BY start_date DESC NULLS LAST
LIMIT %(limit)s
"""


def _categorize_one(client: httpx.Client, rec: dict) -> dict:
    user = (
        f"Area: {rec.get('area_name')}\n"
        f"Applicant: {rec.get('applicant') or 'unknown'}\n"
        f"Type: {rec.get('app_type')}   Size band: {rec.get('app_size')}\n\n"
        f"Description:\n{rec.get('description')}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
            "temperature": 0,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    for attempt in range(1, 6):
        resp = client.post(
            GEMINI_URL.format(model=config.classify_model),
            params={"key": config.gemini_api_key},
            json=payload,
        )
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", min(2 ** attempt, 30))))
            continue
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        cat = data["category"] if data.get("category") in CATEGORIES else "not_datacentre"
        return {
            "category": cat,
            "material": bool(data.get("power_material", False)),
            "confidence": float(data.get("confidence", 0)),
            "reason": str(data.get("reason", ""))[:500],
        }
    raise RuntimeError("Gemini rate-limited after 5 attempts")


def categorize(*, limit: int = 3000, force: bool = False) -> int:
    """Assign category + dc_material to confirmed data centres. Returns count done."""
    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    conn = psycopg.connect(config.database_url)
    client = httpx.Client(timeout=60.0)
    cols = ["name", "description", "app_type", "app_size", "area_name", "applicant"]
    done = 0
    try:
        rows = conn.execute(_SELECT, {"limit": limit, "force": force}).fetchall()
        for row in rows:
            rec = dict(zip(cols, row))
            try:
                c = _categorize_one(client, rec)
            except Exception as exc:
                print(f"  ! {rec['name']}: {exc}")
                continue
            conn.execute(
                """UPDATE application SET dc_category = %s, dc_material = %s,
                     dc_category_confidence = %s, dc_category_reason = %s,
                     dc_category_at = now() WHERE name = %s""",
                (c["category"], c["material"], c["confidence"], c["reason"], rec["name"]),
            )
            conn.commit()
            done += 1
        return done
    finally:
        conn.close()
        client.close()

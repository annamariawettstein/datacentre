"""Phase 0 classification: is this application actually a data centre?

The keyword sweep is high-recall / low-precision — it pulls in solar "data centre"
mentions, telecoms cabinets, EV points that cite data centres, and so on. This pass
uses Claude over the description (plus a few structured fields) to decide, and to
catch genuine data centres filed as generic B8 industrial without the phrase.

Output per record: is_datacentre (bool), classification_confidence (0-1), and a short
reason. Runs in batches, skips already-classified rows unless forced.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx
import psycopg

from .config import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """You classify UK planning applications for a data centre pipeline tracker.

A TRUE data centre application is one whose primary purpose is a building (or extension, \
or fit-out, or infrastructure) to house computing/server/IT equipment at scale — including \
hyperscale, colocation, enterprise and edge data centres, and applications for their \
essential enabling infrastructure (dedicated grid connections, on-site substations, \
backup generators) where the data centre is the clear driver.

NOT true data centres, even if the words appear:
- Telecoms equipment cabinets, 5G masts, broadband street works
- Solar farms / battery storage whose text merely mentions powering data centres
- Small server/comms rooms ancillary to an unrelated main use (offices, hospitals)
- Generic warehouses/logistics (B8) with no computing purpose
- Documents that only reference a nearby or unrelated data centre

Watch for data centres filed under generic B8 "industrial/storage" use classes — if the \
description describes server halls, IT load, or computing infrastructure, it IS a data centre \
regardless of the stated use class.

Respond ONLY with a JSON object: {"is_datacentre": bool, "confidence": float 0-1, \
"reason": "<= 20 words"}."""

_SELECT_UNCLASSIFIED = """
SELECT name, description, app_type, app_size, area_name, address
FROM application
WHERE (%(force)s OR is_datacentre IS NULL)
ORDER BY start_date DESC NULLS LAST
LIMIT %(limit)s
"""


@dataclass
class Classification:
    is_datacentre: bool
    confidence: float
    reason: str


def _user_prompt(rec: dict) -> str:
    return (
        f"Area: {rec.get('area_name')}\n"
        f"Address: {rec.get('address')}\n"
        f"Application type: {rec.get('app_type')}   Size band: {rec.get('app_size')}\n\n"
        f"Description:\n{rec.get('description')}"
    )


def _parse(text: str) -> Classification:
    text = text.strip()
    if text.startswith("```"):  # tolerate code-fenced JSON
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(text)
    return Classification(
        is_datacentre=bool(data["is_datacentre"]),
        confidence=float(data["confidence"]),
        reason=str(data.get("reason", ""))[:500],
    )


class Classifier:
    """Provider-agnostic single-record classifier (Gemini or Anthropic)."""

    def __init__(self) -> None:
        self.provider = config.classify_provider
        self.model = config.classify_model
        if self.provider == "gemini":
            if not config.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._http = httpx.Client(timeout=60.0)
        elif self.provider == "anthropic":
            if not config.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            from anthropic import Anthropic

            self._anthropic = Anthropic(api_key=config.anthropic_api_key)
        else:
            raise RuntimeError(f"unknown CLASSIFY_PROVIDER: {self.provider}")

    def close(self) -> None:
        if self.provider == "gemini":
            self._http.close()

    def classify(self, rec: dict) -> Classification:
        if self.provider == "gemini":
            return self._gemini(rec)
        return self._anthropic_call(rec)

    def _gemini(self, rec: dict) -> Classification:
        url = GEMINI_URL.format(model=self.model)
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": _user_prompt(rec)}]}],
            "generationConfig": {
                "maxOutputTokens": 512,
                "responseMimeType": "application/json",
                "temperature": 0,
                # Disable "thinking" — otherwise 2.5 models can spend the whole
                # output budget reasoning and return truncated / empty JSON.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        for attempt in range(1, 6):
            resp = self._http.post(
                url, params={"key": config.gemini_api_key}, json=payload
            )
            if resp.status_code == 429:  # rate limited — back off
                wait = float(resp.headers.get("Retry-After", min(2 ** attempt, 30)))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return _parse(text)
        raise RuntimeError("Gemini rate-limited after 5 attempts")

    def _anthropic_call(self, rec: dict) -> Classification:
        resp = self._anthropic.messages.create(
            model=self.model,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_prompt(rec)}],
        )
        return _parse(resp.content[0].text)


def classify(*, limit: int = 500, force: bool = False) -> int:
    """Classify up to `limit` unclassified applications. Returns count classified."""
    classifier = Classifier()

    conn = psycopg.connect(config.database_url)
    try:
        rows = conn.execute(
            _SELECT_UNCLASSIFIED, {"limit": limit, "force": force}
        ).fetchall()
        cols = ["name", "description", "app_type", "app_size", "area_name", "address"]
        done = 0
        for row in rows:
            rec = dict(zip(cols, row))
            try:
                c = classifier.classify(rec)
            except Exception as exc:  # never let one bad row kill the batch
                print(f"  ! {rec['name']}: {exc}")
                continue
            conn.execute(
                """UPDATE application SET
                     is_datacentre = %s,
                     classification_confidence = %s,
                     classification_reason = %s,
                     classification_model = %s,
                     classified_at = now()
                   WHERE name = %s""",
                (c.is_datacentre, c.confidence, c.reason, config.classify_model, rec["name"]),
            )
            conn.commit()
            done += 1
        return done
    finally:
        conn.close()
        classifier.close()

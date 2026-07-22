"""PlanIt API client (https://www.planit.org.uk/api).

Handles pagination, the 429 rate limit (honouring Retry-After), and the API's
hard 5000-result-per-query ceiling by splitting a too-large query into date
windows. Only the endpoints Phase 0 needs are implemented.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

import httpx

from .config import config

BASE_URL = "https://www.planit.org.uk"

# The API caps any single query at 5000 results; keep pages well under the
# 1000 kB response ceiling too.
PAGE_SIZE = 200
MAX_RESULTS_PER_QUERY = 5000

# Be a polite client: a small delay between requests avoids tripping the site's
# bot/rate protection (which surfaces as 429s and occasional 403s).
REQUEST_DELAY_SECS = 1.0


@dataclass
class Page:
    records: list[dict]
    total: int
    frm: int
    to: int


class PlanItError(RuntimeError):
    pass


class PlanItClient:
    def __init__(self, contact: str | None = None, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "User-Agent": (
                    f"uk-datacentre-pipeline (contact: {contact or config.planit_contact})"
                ),
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlanItClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _get(self, path: str, params: dict) -> dict:
        """GET with retry on 429 (Retry-After) and transient network errors."""
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt >= 6:
                    raise PlanItError(f"network error after {attempt} tries: {exc}")
                time.sleep(min(2 ** attempt, 30))
                continue

            # 429 (rate limit) and 403 (bot protection tripped by request pace)
            # are both transient — back off and retry, honouring Retry-After.
            if resp.status_code in (429, 403):
                if attempt >= 6:
                    raise PlanItError(
                        f"{resp.status_code} from PlanIt after {attempt} tries"
                    )
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(2 ** attempt, 60)
                time.sleep(wait)
                continue

            if resp.status_code == 400:
                # API returns an explanatory 'error' field on 400.
                try:
                    msg = resp.json().get("error", resp.text)
                except Exception:
                    msg = resp.text
                raise PlanItError(f"400 from PlanIt: {msg}")

            resp.raise_for_status()
            if REQUEST_DELAY_SECS:
                time.sleep(REQUEST_DELAY_SECS)
            return resp.json()

    def _page(self, params: dict, page: int) -> Page:
        body = self._get(
            "/api/applics/json",
            {**params, "pg_sz": PAGE_SIZE, "page": page, "compress": "on"},
        )
        return Page(
            records=body.get("records", []),
            total=int(body.get("total", 0)),
            frm=int(body.get("from", 0)),
            to=int(body.get("to", 0)),
        )

    # -- high level --------------------------------------------------------

    def count(self, params: dict) -> int:
        """Total records the (unpaged) query would return."""
        return self._page(
            {**params, "select": "name", "sort": "name"}, page=1
        ).total

    def search(self, params: dict) -> Iterator[dict]:
        """Yield every record for a query, paging through results.

        If the query exceeds the API's 5000-result ceiling, it is split into
        successive start_date windows so nothing is silently dropped.
        """
        total = self.count(params)
        if total <= MAX_RESULTS_PER_QUERY:
            yield from self._paged(params)
            return

        # Too big — window by start_date. PlanIt data starts well after 2000.
        yield from self._windowed(params, date(2000, 1, 1), date.today())

    def _paged(self, params: dict) -> Iterator[dict]:
        page = 1
        seen = 0
        while True:
            pg = self._page(params, page)
            if not pg.records:
                break
            for rec in pg.records:
                yield rec
            seen += len(pg.records)
            if seen >= pg.total or pg.to + 1 >= pg.total:
                break
            page += 1

    def _windowed(self, params: dict, start: date, end: date) -> Iterator[dict]:
        """Recursively bisect [start, end] until each window is under the cap."""
        window_params = {
            **params,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        total = self.count(window_params)
        if total <= MAX_RESULTS_PER_QUERY or start >= end:
            yield from self._paged(window_params)
            return
        mid = start + (end - start) / 2
        yield from self._windowed(params, start, mid)
        yield from self._windowed(params, mid + timedelta(days=1), end)

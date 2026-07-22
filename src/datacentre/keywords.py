"""Keyword filter for the PlanIt sweep.

Deliberately broad: this is the *recall* stage. We want every plausible data
centre — including ones filed as generic B8 industrial — and accept a high false
positive rate here, because the Phase 0 LLM classification pass is the *precision*
stage that sorts true sites from noise.
"""

from __future__ import annotations

# Terms combined with PlanIt's search syntax: quoted phrases are matched as
# phrases, bare words as words, "or" is a logical OR. See the API docs.
DATACENTRE_TERMS: list[str] = [
    '"data centre"',
    '"data center"',
    "datacentre",
    "datacenter",
    '"data centres"',
    '"data centers"',
    "hyperscale",
    "colocation",
    '"co-location"',
    '"server farm"',
    '"digital infrastructure"',
]


def search_expression(terms: list[str] | None = None) -> str:
    """Build the PlanIt `search=` expression from the term list."""
    return " or ".join(terms or DATACENTRE_TERMS)

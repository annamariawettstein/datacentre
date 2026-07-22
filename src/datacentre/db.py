"""Thin PostgreSQL access layer (psycopg 3)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

from .config import config

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Yield a connection, committing on success and rolling back on error."""
    conn = psycopg.connect(config.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    """Apply db/schema.sql (idempotent)."""
    sql = SCHEMA_PATH.read_text()
    with connect() as conn:
        conn.execute(sql)

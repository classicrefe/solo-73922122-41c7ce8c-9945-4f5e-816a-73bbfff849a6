"""Create database tables and backfill the research observation index.

Runs on container start and for local setup. All steps are idempotent, so
existing volumes are upgraded in place without losing data.
"""
from __future__ import annotations

import time

from sqlalchemy import text

from app.database import engine, init_db_and_backfill


def wait_for_database(max_attempts: int = 30, delay: float = 1.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover - startup path
            if attempt == max_attempts:
                raise
            print(f"waiting for database... ({attempt}/{max_attempts}) {exc}")
            time.sleep(delay)


if __name__ == "__main__":
    wait_for_database()
    inspected = init_db_and_backfill()
    print(f"database schema ready; research index backfilled ({inspected} observations)")

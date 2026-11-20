"""Re-build the derived research observation index from stored resources.

Usage::

    python -m app.backfill_index

Safe to run against a live database: it is idempotent, commits per batch, and
only touches ``research_observation_index``. The container entrypoint runs the
same routine on every start (see ``app.db_init``).
"""
from __future__ import annotations

from app.database import SessionLocal, init_db
from app.services.research_index import backfill_research_index


def main() -> int:
    init_db()
    with SessionLocal() as session:
        inspected = backfill_research_index(session)
    print(f"research index backfilled: {inspected} observations inspected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

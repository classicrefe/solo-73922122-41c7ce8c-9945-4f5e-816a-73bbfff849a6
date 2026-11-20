"""Maintenance of the research observation index.

The index (``research_observation_index``) holds one row per Observation that
is *research usable*:

* the Observation's FHIR status is ``final``, and
* at least one linked DiagnosticReport has FHIR status ``final`` or
  ``amended``.

It is derived data, never edited by callers. Every mutation of an Observation,
a DiagnosticReport, or a report->observation link recomputes the index for the
affected observations **inside the same transaction**, so the index commits and
rolls back together with the source resources.

Concurrent corrections
----------------------

Two correction Bundles may simultaneously remove different qualifying report
references from the same Observation. Under READ COMMITTED each transaction
would otherwise decide from a snapshot that ignores the other's uncommitted
deletes (write skew) and the row could survive by accident.

Every recomputation therefore takes a PostgreSQL transaction-scoped advisory
lock keyed by the Observation id, acquired in sorted id order. The decision
query runs only after the lock is granted, so it observes every previously
committed withdrawal; the last committer recomputes from the true remaining
set. Locking in sorted order keeps overlapping bundles deadlock-free.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import DiagnosticReport, Observation, ReportLink, ResearchObservationIndex

ELIGIBLE_OBSERVATION_STATUSES = frozenset({"final"})
ELIGIBLE_REPORT_STATUSES = frozenset({"final", "amended"})

# Namespace for pg_advisory_xact_lock; keeps our locks separate from any other
# advisory-lock user of the database. Fits in a signed int4.
_INDEX_LOCK_NAMESPACE = 0x52494458  # "RIDX"

_BACKFILL_BATCH = 500


def _fhir_status(resource: object) -> str | None:
    if isinstance(resource, dict):
        status = resource.get("status")
        if isinstance(status, str):
            return status
    return None


def is_eligible_observation(resource: object) -> bool:
    return _fhir_status(resource) in ELIGIBLE_OBSERVATION_STATUSES


def is_eligible_report(resource: object) -> bool:
    return _fhir_status(resource) in ELIGIBLE_REPORT_STATUSES


def _is_postgres(session: Session) -> bool:
    return session.bind.dialect.name == "postgresql"  # type: ignore[union-attr]


def _lock_key(observation_id: str) -> int:
    """Stable signed-int32 advisory lock key for an observation id."""
    digest = hashlib.sha256(observation_id.encode("utf-8")).digest()
    unsigned = int.from_bytes(digest[:4], "big")
    return unsigned - 2**32 if unsigned >= 2**31 else unsigned


def _lock_observations(session: Session, observation_ids: list[str]) -> None:
    """Serialize index decisions per observation (PostgreSQL only).

    Locks are taken in ascending key order, the same order in every process, so
    overlapping bundles cannot deadlock each other. SQLite's single-writer model
    needs no equivalent (and its dialect has no advisory locks); concurrency
    tests are PostgreSQL-only.
    """
    if not _is_postgres(session):
        return
    keyed = sorted((_lock_key(oid), oid) for oid in observation_ids)
    for key, _oid in keyed:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": _INDEX_LOCK_NAMESPACE, "key": key},
        )


def sync_research_index(session: Session, observation_ids: Iterable[str | None]) -> None:
    """Recompute index rows for the given observations.

    Must be called after all source writes of the Bundle have been applied but
    before the transaction commits. Pending ORM changes are flushed first so
    the decision sees this Bundle's own link/status changes.

    Every Bundle that can change an Observation's index row lists that
    Observation here and therefore takes its advisory lock; the row (including
    the recorded version) is recomputed only under that lock. A concurrent
    Observation version bump and a concurrent report correction can then never
    leave a stale version behind: whichever transaction commits last recomputes
    from the other's committed state.
    """
    ids = sorted({oid for oid in observation_ids if oid})
    if not ids:
        return

    # Make this transaction's writes visible to its own subsequent queries
    # (sessions are configured with autoflush=False).
    session.flush()

    _lock_observations(session, ids)

    # Column-level reads deliberately bypass the ORM identity map so the
    # decision always reflects the database as of this statement (our own
    # uncommitted writes were flushed above).
    obs_rows = session.execute(
        select(
            Observation.id,
            Observation.patient_id,
            Observation.version,
            Observation.resource,
        ).where(Observation.id.in_(ids))
    ).all()
    obs_by_id = {row.id: row for row in obs_rows}

    linked_stmt = (
        select(ReportLink.observation_id, DiagnosticReport.resource)
        .join(DiagnosticReport, DiagnosticReport.id == ReportLink.report_id)
        .where(ReportLink.observation_id.in_(ids))
    )
    qualifying_linked: set[str] = set()
    for observation_id, report_resource in session.execute(linked_stmt).all():
        if is_eligible_report(report_resource):
            qualifying_linked.add(observation_id)

    current_rows = {
        row.observation_id: row
        for row in session.execute(
            select(ResearchObservationIndex).where(
                ResearchObservationIndex.observation_id.in_(ids)
            )
        ).scalars().all()
    }

    for oid in ids:
        obs = obs_by_id.get(oid)
        wanted = (
            obs is not None
            and is_eligible_observation(obs.resource)
            and oid in qualifying_linked
        )
        existing = current_rows.get(oid)
        if wanted:
            if existing is None:
                session.add(
                    ResearchObservationIndex(
                        observation_id=oid,
                        patient_id=obs.patient_id,  # type: ignore[union-attr]
                        version=obs.version,  # type: ignore[union-attr]
                    )
                )
            else:
                existing.patient_id = obs.patient_id  # type: ignore[union-attr]
                existing.version = obs.version  # type: ignore[union-attr]
        elif existing is not None:
            session.delete(existing)


def backfill_research_index(session: Session, batch_size: int = _BACKFILL_BATCH) -> int:
    """Bring the index in line with already-stored data.

    Idempotent: stale rows are deleted, missing rows are inserted, current rows
    are refreshed. Commits per batch so a large existing database does not
    require one giant transaction (each batch is atomic on its own, and the
    whole operation can simply be re-run). Returns the number of observations
    inspected.
    """
    observation_ids = session.execute(select(Observation.id)).scalars().all()
    for start in range(0, len(observation_ids), batch_size):
        sync_research_index(session, observation_ids[start : start + batch_size])
        session.commit()
    return len(observation_ids)

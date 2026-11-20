"""Research observation index: content, maintenance and concurrency."""
from __future__ import annotations

import concurrent.futures
import threading
import uuid

import pytest
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import ReportLink, ResearchObservationIndex
from app.services import research_index
from app.services.research_index import backfill_research_index
from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


def _submit(client, key, bundle, expect_code=200):
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})
    assert resp.status_code == expect_code, resp.text
    return resp.json()


def _index(client, patient_id):
    resp = client.get(f"/patients/{patient_id}/research-index")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patientId"] == patient_id
    assert body["total"] == len(body["entry"])
    return body["entry"]


def _index_row_ids(client, patient_id):
    return [e["observationId"] for e in _index(client, patient_id)]


def _ids_by_index(receipt):
    return {m["requestIndex"]: m["id"] for m in receipt["resourceMap"]}


def _seed(client, key, obs_status="final", report_status="final"):
    """Create Patient + Observation + DiagnosticReport in one bundle."""
    p_urn, o_urn, r_urn = urn(), urn(), urn()
    p, _ = patient_entry(p_urn, value=f"IDX-{uuid.uuid4().hex[:8]}")
    o = observation_entry(o_urn, p_urn)
    o["resource"]["status"] = obs_status
    r = report_entry(r_urn, p_urn, [o_urn])
    r["resource"]["status"] = report_status
    receipt = _submit(client, key, make_bundle([p, o, r]))
    ids = _ids_by_index(receipt)
    return ids[0], ids[1], ids[2]


def _put_observation(oid, pid, status="final", version=1):
    return {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": status,
            "code": {"coding": [{"code": "GLU"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "valueQuantity": {"value": 6.0, "unit": "mmol/L"},
        },
        "request": {
            "method": "PUT",
            "url": f"Observation/{oid}",
            "ifMatch": f'W/"{version}"',
        },
    }


def _put_report(rid, pid, result_ids, status="final", version=1):
    return {
        "resource": {
            "resourceType": "DiagnosticReport",
            "id": rid,
            "status": status,
            "code": {"coding": [{"code": "24323-8"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "result": [{"reference": f"Observation/{o}"} for o in result_ids],
        },
        "request": {
            "method": "PUT",
            "url": f"DiagnosticReport/{rid}",
            "ifMatch": f'W/"{version}"',
        },
    }


# --------------------------------------------------------------------------- #
# Content rules
# --------------------------------------------------------------------------- #

def test_eligible_final_observation_referenced_by_final_report_is_indexed(client):
    pid, oid, _rid = _seed(client, "IDX-BASE")
    assert _index(client, pid) == [
        {"observationId": oid, "version": 1}
    ]


def test_report_must_be_final_or_amended(client):
    pid, oid, rid = _seed(client, "IDX-PRELIM", report_status="preliminary")
    assert _index(client, pid) == []

    # Promoting the same report to amended brings the observation in.
    _submit(
        client,
        "IDX-PRELIM-2",
        make_bundle([_put_report(rid, pid, [oid], status="amended")]),
    )
    assert _index_row_ids(client, pid) == [oid]

    # ... and to final keeps it.
    _submit(
        client,
        "IDX-PRELIM-3",
        make_bundle([_put_report(rid, pid, [oid], status="final", version=2)]),
    )
    assert _index_row_ids(client, pid) == [oid]


def test_observation_must_be_final(client):
    pid, oid, _rid = _seed(client, "IDX-OBS-PRELIM", obs_status="preliminary")
    assert _index(client, pid) == []

    _submit(client, "IDX-OBS-PRELIM-2", make_bundle([_put_observation(oid, pid)]))
    assert _index_row_ids(client, pid) == [oid]

    # Changing the observation back out of final removes the row.
    _submit(
        client,
        "IDX-OBS-PRELIM-3",
        make_bundle([_put_observation(oid, pid, status="corrected", version=2)]),
    )
    assert _index(client, pid) == []


def test_observation_amended_is_not_usable(client):
    pid, _oid, _rid = _seed(client, "IDX-OBS-AMENDED", obs_status="amended")
    assert _index(client, pid) == []


def test_multiple_reports_share_one_row(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-SHARE-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = _submit(client, "IDX-SHARE", make_bundle([p, o, r1, r2]))
    ids = _ids_by_index(receipt)
    assert _index_row_ids(client, ids[0]) == [ids[1]]
    with SessionLocal() as session:
        rows = session.execute(
            select(ResearchObservationIndex).where(
                ResearchObservationIndex.observation_id == ids[1]
            )
        ).scalars().all()
        assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Withdrawal semantics
# --------------------------------------------------------------------------- #

def test_report_removing_reference_removes_index_row(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-REM-{uuid.uuid4().hex[:8]}")
    o1 = observation_entry(urn(), p_urn)
    o2 = observation_entry(urn(), p_urn)
    r = report_entry(urn(), p_urn, [o1["fullUrl"], o2["fullUrl"]])
    receipt = _submit(client, "IDX-REM", make_bundle([p, o1, o2, r]))
    ids = _ids_by_index(receipt)
    pid, oid1, oid2, rid = ids[0], ids[1], ids[2], ids[3]
    assert sorted(_index_row_ids(client, pid)) == sorted([oid1, oid2])

    # The corrected report only keeps oid2; oid1 loses its last qualifying link.
    _submit(client, "IDX-REM-2", make_bundle([_put_report(rid, pid, [oid2])]))
    assert _index_row_ids(client, pid) == [oid2]

    # Re-adding the reference restores the row.
    _submit(
        client,
        "IDX-REM-3",
        make_bundle([_put_report(rid, pid, [oid1, oid2], version=2)]),
    )
    assert sorted(_index_row_ids(client, pid)) == sorted([oid1, oid2])


def test_row_survives_while_another_qualifying_report_remains(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-KEEP-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = _submit(client, "IDX-KEEP", make_bundle([p, o, r1, r2]))
    ids = _ids_by_index(receipt)
    pid, oid, rid1, rid2 = ids[0], ids[1], ids[2], ids[3]
    assert _index_row_ids(client, pid) == [oid]

    # Withdraw report 1's eligibility: report 2 still qualifies.
    _submit(
        client,
        "IDX-KEEP-2",
        make_bundle([_put_report(rid1, pid, [oid], status="preliminary")]),
    )
    assert _index_row_ids(client, pid) == [oid]

    # Now withdraw report 2 as well: no qualifying report remains.
    _submit(
        client,
        "IDX-KEEP-3",
        make_bundle([_put_report(rid2, pid, [oid], status="preliminary")]),
    )
    assert _index(client, pid) == []


def test_downgrading_report_status_withdraws_row(client):
    pid, oid, rid = _seed(client, "IDX-STATUS")
    _submit(
        client,
        "IDX-STATUS-2",
        make_bundle([_put_report(rid, pid, [oid], status="cancelled")]),
    )
    assert _index(client, pid) == []


def test_index_version_tracks_observation_updates(client):
    pid, oid, _rid = _seed(client, "IDX-VER")
    assert _index(client, pid)[0]["version"] == 1

    _submit(client, "IDX-VER-2", make_bundle([_put_observation(oid, pid)]))
    assert _index(client, pid) == [
        {"observationId": oid, "version": 2}
    ]


# --------------------------------------------------------------------------- #
# Atomicity with the source data
# --------------------------------------------------------------------------- #

def test_failed_bundle_does_not_touch_index(client):
    pid, oid, rid = _seed(client, "IDX-ATOM")
    before = _index(client, pid)

    # A correction that would withdraw the reference but also references a
    # missing observation: the whole bundle (including index maintenance) rolls
    # back.
    bad = _put_report(rid, pid, [oid], status="cancelled")
    bad["resource"]["result"] = [{"reference": "Observation/does-not-exist"}]
    resp = client.post(
        "/fhir", json=make_bundle([bad]), headers={"Idempotency-Key": "IDX-ATOM-BAD"}
    )
    assert resp.status_code == 422
    assert _index(client, pid) == before


def test_backfill_rebuilds_index_for_existing_rows(client):
    pid, oid, _rid = _seed(client, "IDX-BACKFILL")
    # Simulate a database populated before the index existed (or drifted):
    # wipe the derived table and re-run the startup backfill.
    with SessionLocal() as session:
        for row in session.execute(select(ResearchObservationIndex)).scalars().all():
            session.delete(row)
        session.commit()
    assert _index(client, pid) == []

    with SessionLocal() as session:
        inspected = backfill_research_index(session)
    assert inspected >= 1
    assert _index_row_ids(client, pid) == [oid]

    # Running it again is a no-op.
    with SessionLocal() as session:
        backfill_research_index(session)
    assert _index_row_ids(client, pid) == [oid]


# --------------------------------------------------------------------------- #
# Concurrency: two corrections withdrawing different reports
# --------------------------------------------------------------------------- #

def _concurrent_withdrawals(client, monkeypatch, hold_at_decision: bool):
    """Two sessions each remove a different report's link to the same observation.

    The link deletes are ordinary writes that do not touch each other's rows;
    the only coordination point is the index decision itself. Returns the
    observation id after both transactions committed.
    """
    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-RACE-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = _submit(client, "IDX-RACE", make_bundle([p, o, r1, r2]))
    ids = _ids_by_index(receipt)
    pid, oid, rid1, rid2 = ids[0], ids[1], ids[2], ids[3]
    assert _index_row_ids(client, pid) == [oid]

    barrier = threading.Barrier(2, timeout=20)
    original_lock = research_index._lock_observations

    def barrier_then_lock(session, observation_ids):
        if hold_at_decision:
            barrier.wait()
        return original_lock(session, observation_ids)

    monkeypatch.setattr(research_index, "_lock_observations", barrier_then_lock)

    def worker(rid: str):
        session = SessionLocal()
        try:
            session.execute(
                delete(ReportLink).where(
                    ReportLink.report_id == rid,
                    ReportLink.observation_id == oid,
                )
            )
            session.flush()
            research_index.sync_research_index(session, [oid])
            session.commit()
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, rid1), pool.submit(worker, rid2)]
        for future in futures:
            future.result()

    return pid, oid


def test_new_report_attaching_existing_observation_indexes_it(client):
    p_urn, o_urn = urn(), urn()
    p, _ = patient_entry(p_urn, value=f"IDX-ATTACH-{uuid.uuid4().hex[:8]}")
    o = observation_entry(o_urn, p_urn)
    first = _submit(client, "IDX-ATTACH-1", make_bundle([p, o]))
    ids = _ids_by_index(first)
    pid, oid = ids[0], ids[1]
    assert _index(client, pid) == []  # no report yet

    r = report_entry(urn(), f"Patient/{pid}", [f"Observation/{oid}"])
    _submit(client, "IDX-ATTACH-2", make_bundle([r]))
    assert _index_row_ids(client, pid) == [oid]


def test_moving_observation_to_another_patient_moves_index_row(client):
    p1_urn, p2_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value=f"IDX-MV-A-{uuid.uuid4().hex[:8]}")
    p2, _ = patient_entry(p2_urn, value=f"IDX-MV-B-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p1_urn)
    r = report_entry(urn(), p1_urn, [o["fullUrl"]])
    first = _submit(client, "IDX-MV-1", make_bundle([p1, p2, o, r]))
    ids = _ids_by_index(first)
    pid1, pid2, oid, rid = ids[0], ids[1], ids[2], ids[3]
    assert _index_row_ids(client, pid1) == [oid]

    # Move both the observation and its report, as the alignment rules require.
    _submit(
        client,
        "IDX-MV-2",
        make_bundle(
            [
                _put_observation(oid, pid2),
                _put_report(rid, pid2, [oid]),
            ]
        ),
    )
    assert _index(client, pid1) == []
    assert _index_row_ids(client, pid2) == [oid]


def test_concurrent_correction_bundles_via_http(client):
    """The same withdrawal race driven through POST /fhir end to end."""
    from app import database

    if database.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL")

    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-HTTP-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = _submit(client, "IDX-HTTP-1", make_bundle([p, o, r1, r2]))
    ids = _ids_by_index(receipt)
    pid, oid, rid1, rid2 = ids[0], ids[1], ids[2], ids[3]

    def downgrade(rid, key):
        return client.post(
            "/fhir",
            json=make_bundle([_put_report(rid, pid, [oid], status="preliminary")]),
            headers={"Idempotency-Key": key},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(downgrade, rid1, "IDX-HTTP-A"),
            pool.submit(downgrade, rid2, "IDX-HTTP-B"),
        ]
        responses = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert _index(client, pid) == []


def test_concurrent_withdrawals_of_different_reports_delete_row(client, monkeypatch):
    from app import database

    if database.engine.dialect.name != "postgresql":
        pytest.skip("advisory-lock concurrency semantics require PostgreSQL")

    pid, oid = _concurrent_withdrawals(client, monkeypatch, hold_at_decision=True)
    assert _index(client, pid) == []
    with SessionLocal() as session:
        assert (
            session.execute(
                select(ResearchObservationIndex).where(
                    ResearchObservationIndex.observation_id == oid
                )
            ).scalars().first()
            is None
        )


def test_without_serialization_concurrent_withdrawals_would_keep_row(client, monkeypatch):
    """Documents the write skew the advisory lock prevents.

    This is the same scenario with locking disabled; it is expected to leave a
    stale index row. Kept as a regression guard: if the real test starts passing
    for the wrong reason, this one shows the race is otherwise live.
    """
    from app import database

    if database.engine.dialect.name != "postgresql":
        pytest.skip("advisory-lock concurrency semantics require PostgreSQL")

    original_lock = research_index._lock_observations

    def no_lock(session, observation_ids):
        barrier.wait()

    # Re-create the helper's flow manually so the barrier is shared.
    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-SKEW-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = client.post(
        "/fhir",
        json=make_bundle([p, o, r1, r2]),
        headers={"Idempotency-Key": "IDX-SKEW"},
    ).json()
    ids = _ids_by_index(receipt)
    pid, oid, rid1, rid2 = ids[0], ids[1], ids[2], ids[3]
    assert _index_row_ids(client, pid) == [oid]

    barrier = threading.Barrier(2, timeout=20)
    monkeypatch.setattr(research_index, "_lock_observations", no_lock)

    def worker(rid: str):
        session = SessionLocal()
        try:
            session.execute(
                delete(ReportLink).where(
                    ReportLink.report_id == rid,
                    ReportLink.observation_id == oid,
                )
            )
            session.flush()
            research_index.sync_research_index(session, [oid])
            session.commit()
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, rid1), pool.submit(worker, rid2)]
        for future in futures:
            future.result()

    # Write skew: both transactions decided from a snapshot containing the
    # other's not-yet-committed link, so the stale row survives.
    assert _index_row_ids(client, pid) == [oid]

    monkeypatch.setattr(research_index, "_lock_observations", original_lock)
    # A subsequent correction or the startup backfill removes it.
    with SessionLocal() as session:
        backfill_research_index(session)
    assert _index(client, pid) == []


def test_concurrent_withdrawal_and_other_report_update_keeps_row(client, monkeypatch):
    from app import database

    if database.engine.dialect.name != "postgresql":
        pytest.skip("advisory-lock concurrency semantics require PostgreSQL")

    p_urn = urn()
    p, _ = patient_entry(p_urn, value=f"IDX-RACE2-{uuid.uuid4().hex[:8]}")
    o = observation_entry(urn(), p_urn)
    r1 = report_entry(urn(), p_urn, [o["fullUrl"]])
    r2 = report_entry(urn(), p_urn, [o["fullUrl"]])
    receipt = _submit(client, "IDX-RACE2", make_bundle([p, o, r1, r2]))
    ids = _ids_by_index(receipt)
    pid, oid, rid1, rid2 = ids[0], ids[1], ids[2], ids[3]

    def downgrade():
        return client.post(
            "/fhir",
            json=make_bundle([_put_report(rid1, pid, [oid], status="preliminary")]),
            headers={"Idempotency-Key": "IDX-RACE2-A"},
        )

    def keep_final():
        # An unrelated correction to the still-qualifying report.
        return client.post(
            "/fhir",
            json=make_bundle([_put_report(rid2, pid, [oid], status="final")]),
            headers={"Idempotency-Key": "IDX-RACE2-B"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(downgrade), pool.submit(keep_final)]
        responses = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert _index_row_ids(client, pid) == [oid]

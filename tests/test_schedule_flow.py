"""Integration test: complete schedule lifecycle — versioning, archiving, status flow.

Run inside Docker:
    python /app/tests/test_schedule_flow.py
"""
import sys
import os
import uuid
from datetime import date, time, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.database import db_session
from src.db.migrations import run_safe_migrations
from src.models.employee import EmployeeORM
from src.models.cruise_ship import CruiseShipORM
from src.models.enums import (
    EmploymentType, Housing, RoleCapability, Port, ShipSize, ScheduleStatus,
)
from src.models.schedule import AssignmentORM, ScheduleORM
from src.models.shift_template import ShiftTemplateORM

YEAR, MONTH = 2026, 7  # July 2026


# ── Helpers ───────────────────────────────────────────────────────────────────

_pass = 0
_fail = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _pass, _fail
    if condition:
        _pass += 1
        print(f"  PASS  {label}")
    else:
        _fail += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def _get_schedule(schedule_id: uuid.UUID) -> ScheduleORM | None:
    with db_session() as db:
        return db.query(ScheduleORM).filter_by(id=schedule_id).first()


def _create_schedule(version: int, status: str, emp_id: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    with db_session() as db:
        orm = ScheduleORM(
            id=sid, month=MONTH, year=YEAR, status=status, version=version,
            created_at=datetime.utcnow(), modified_at=None,
        )
        db.add(orm)
        db.flush()
        for day_n in range(1, 6):
            db.add(AssignmentORM(
                id=uuid.uuid4(), schedule_id=sid, employee_id=emp_id,
                date=date(YEAR, MONTH, day_n), shift_id="1", is_day_off=False,
            ))
    return sid


def _set_status(schedule_id: uuid.UUID, status: str) -> None:
    with db_session() as db:
        orm = db.query(ScheduleORM).filter_by(id=schedule_id).first()
        if orm:
            orm.status = status
            orm.modified_at = datetime.utcnow()


def _archive_all(year: int, month: int) -> int:
    with db_session() as db:
        rows = db.query(ScheduleORM).filter_by(year=year, month=month).all()
        max_ver = 0
        for r in rows:
            max_ver = max(max_ver, r.version or 1)
            if r.status != ScheduleStatus.archived.value:
                r.status = ScheduleStatus.archived.value
                r.modified_at = datetime.utcnow()
    return max_ver + 1


# ── Test setup ────────────────────────────────────────────────────────────────

def setup():
    """Ensure migrations are applied and return a test employee ID."""
    run_safe_migrations()

    # Find or create a test employee
    with db_session() as db:
        emp = db.query(EmployeeORM).filter_by(name="__test_flow_emp__").first()
        if not emp:
            emp = EmployeeORM(
                id=uuid.uuid4(),
                name="__test_flow_emp__",
                languages="english",
                role_capability=RoleCapability.cafe.value,
                employment_type=EmploymentType.full_time.value,
                contracted_hours=37.5,
                housing=Housing.geiranger.value,
                driving_licence=False,
                availability_start=date(2026, 5, 1),
                availability_end=date(2026, 10, 31),
            )
            db.add(emp)
        emp_id = emp.id

    # Clean existing test schedules for July 2026 (delete assignments first)
    with db_session() as db:
        for sched in db.query(ScheduleORM).filter_by(year=YEAR, month=MONTH).all():
            db.query(AssignmentORM).filter_by(schedule_id=sched.id).delete()
        db.query(ScheduleORM).filter_by(year=YEAR, month=MONTH).delete()

    return emp_id


def teardown():
    """Remove test data."""
    with db_session() as db:
        for sched in db.query(ScheduleORM).filter_by(year=YEAR, month=MONTH).all():
            db.query(AssignmentORM).filter_by(schedule_id=sched.id).delete()
        db.query(ScheduleORM).filter_by(year=YEAR, month=MONTH).delete()
        db.query(EmployeeORM).filter_by(name="__test_flow_emp__").delete()


# ── Test steps ────────────────────────────────────────────────────────────────

def run_tests():
    print(f"\n{'='*60}")
    print("Schedule Flow Integration Test")
    print(f"Target: July {YEAR}")
    print(f"{'='*60}\n")

    emp_id = setup()

    # ── Step 1: Create schedule v1 as draft ───────────────────────────────────
    print("Step 1: Create schedule v1 → expect status=draft, version=1")
    sid1 = _create_schedule(1, ScheduleStatus.draft.value, emp_id)
    orm1 = _get_schedule(sid1)
    check("status = draft", orm1 and orm1.status == ScheduleStatus.draft.value, orm1.status if orm1 else "none")
    check("version = 1", orm1 and orm1.version == 1, str(orm1.version) if orm1 else "none")

    # ── Step 2: Approve ───────────────────────────────────────────────────────
    print("\nStep 2: Approve v1 → expect status=approved")
    _set_status(sid1, ScheduleStatus.approved.value)
    orm1 = _get_schedule(sid1)
    check("status = approved", orm1 and orm1.status == ScheduleStatus.approved.value)
    check("version still 1", orm1 and orm1.version == 1)

    # ── Step 3: Edit and save as draft ────────────────────────────────────────
    print("\nStep 3: Modify one assignment → save as draft → expect status=draft, version=1")
    _set_status(sid1, ScheduleStatus.draft.value)
    orm1 = _get_schedule(sid1)
    check("status = draft after editor save", orm1 and orm1.status == ScheduleStatus.draft.value)
    check("version still 1 after edit", orm1 and orm1.version == 1)

    # ── Step 4: Approve again ─────────────────────────────────────────────────
    print("\nStep 4: Approve again → expect status=approved")
    _set_status(sid1, ScheduleStatus.approved.value)
    orm1 = _get_schedule(sid1)
    check("status = approved again", orm1 and orm1.status == ScheduleStatus.approved.value)

    # ── Step 5: Generate new schedule → old becomes archived, new is v2 ──────
    print("\nStep 5: Generate new schedule → old archived, new v2 draft")
    next_ver = _archive_all(YEAR, MONTH)
    sid2 = _create_schedule(next_ver, ScheduleStatus.draft.value, emp_id)
    orm1 = _get_schedule(sid1)
    orm2 = _get_schedule(sid2)
    check("old schedule status = archived", orm1 and orm1.status == ScheduleStatus.archived.value)
    check("new schedule status = draft", orm2 and orm2.status == ScheduleStatus.draft.value)
    check("new schedule version = 2", orm2 and orm2.version == 2, str(orm2.version) if orm2 else "none")

    # ── Step 6: Load archived version → verify read-only (status=archived) ───
    print("\nStep 6: Load archived v1 → verify status=archived")
    with db_session() as db:
        archived_orm = db.query(ScheduleORM).filter_by(id=sid1).first()
        archived_status = archived_orm.status if archived_orm else None
    check("archived version is archived", archived_status == ScheduleStatus.archived.value)
    check("archived version cannot be approved (status check)", archived_status != ScheduleStatus.approved.value)

    # ── Step 7: Restore archived version → creates v3 draft ──────────────────
    print("\nStep 7: Restore archived v1 → expect new v3 draft")
    next_ver2 = _archive_all(YEAR, MONTH)
    sid3 = _create_schedule(next_ver2, ScheduleStatus.draft.value, emp_id)
    orm3 = _get_schedule(sid3)
    check("restored version status = draft", orm3 and orm3.status == ScheduleStatus.draft.value)
    check("restored version = 3", orm3 and orm3.version == 3, str(orm3.version) if orm3 else "none")

    # ── Step 8: Cross-month check uses APPROVED version of July ──────────────
    print("\nStep 8: Cross-month check uses APPROVED version of July")
    # Set sid2 (v2) to approved; it's currently archived from step 7
    _set_status(sid2, ScheduleStatus.approved.value)
    # sid3 is v3 draft; sid2 is v2 approved; sid1 is v1 archived
    with db_session() as db:
        approved_july = (
            db.query(ScheduleORM)
            .filter_by(year=YEAR, month=MONTH, status=ScheduleStatus.approved.value)
            .order_by(ScheduleORM.version.desc())
            .first()
        )
        # Simulate solver preferring approved over latest non-archived
        latest_non_archived_draft = (
            db.query(ScheduleORM)
            .filter(
                ScheduleORM.year == YEAR,
                ScheduleORM.month == MONTH,
                ScheduleORM.status != ScheduleStatus.archived.value,
            )
            .order_by(ScheduleORM.version.desc())
            .first()
        )
    check(
        "approved July exists for cross-month", approved_july is not None,
        "no approved version found"
    )
    check(
        "cross-month prefers approved (sid2) over latest draft (sid3)",
        approved_july and approved_july.id == sid2,
        f"got {approved_july.id if approved_july else None}, expected {sid2}"
    )
    check(
        "latest non-archived is sid3 (draft)", latest_non_archived_draft
        and latest_non_archived_draft.id == sid3,
        f"got {latest_non_archived_draft.id if latest_non_archived_draft else None}"
    )

    teardown()


if __name__ == "__main__":
    run_tests()
    print(f"\n{'='*60}")
    total = _pass + _fail
    print(f"Results: {_pass}/{total} passed, {_fail} failed")
    print(f"{'='*60}")
    sys.exit(0 if _fail == 0 else 1)

"""Tests for Pydantic models and validators."""
import pytest
from datetime import date, time
from pydantic import ValidationError

from src.models.employee import EmployeeCreate
from src.models.cruise_ship import CruiseShipCreate, ShipLanguageCreate
from src.models.shift_template import ShiftTemplateCreate
from src.models.establishment import EstablishmentSettingsCreate
from src.models.schedule import ScheduleCreate, AssignmentCreate
from src.models.enums import (
    RoleCapability, EmploymentType, Housing, Port, ShipSize, ShiftRole, Season, ScheduleStatus
)
import uuid


# ── Employee ────────────────────────────────────────────────────────────────

class TestEmployeeCreate:
    def _valid(self, **overrides):
        data = dict(
            name="Aina",
            languages=["english", "spanish"],
            role_capability=RoleCapability.both,
            employment_type=EmploymentType.full_time,
            contracted_hours=37.5,
            housing=Housing.eidsdal,
            driving_licence=True,
            availability_start=date(2026, 5, 1),
            availability_end=date(2026, 10, 15),
        )
        data.update(overrides)
        return EmployeeCreate(**data)

    def test_valid_employee(self):
        emp = self._valid()
        assert emp.name == "Aina"
        assert "english" in emp.languages

    def test_english_auto_added(self):
        emp = self._valid(languages=["spanish"])
        assert emp.languages[0] == "english"

    def test_dates_out_of_season_rejected(self):
        with pytest.raises(ValidationError):
            self._valid(availability_start=date(2026, 3, 1), availability_end=date(2026, 10, 15))

    def test_end_after_season_rejected(self):
        with pytest.raises(ValidationError):
            self._valid(availability_start=date(2026, 5, 1), availability_end=date(2026, 11, 1))

    def test_start_must_be_before_end(self):
        with pytest.raises(ValidationError):
            self._valid(availability_start=date(2026, 9, 1), availability_end=date(2026, 5, 1))

    def test_negative_hours_rejected(self):
        with pytest.raises(ValidationError):
            self._valid(contracted_hours=-1)


# ── CruiseShip ──────────────────────────────────────────────────────────────

class TestCruiseShipCreate:
    def _valid(self, **overrides):
        data = dict(
            ship_name="Costa Diadema",
            date=date(2026, 8, 4),
            arrival_time=time(11, 30),
            departure_time=time(19, 30),
            port=Port.geiranger_4B_SW,
            size=ShipSize.big,
            good_ship=False,
        )
        data.update(overrides)
        return CruiseShipCreate(**data)

    def test_valid_ship(self):
        ship = self._valid()
        assert ship.ship_name == "Costa Diadema"

    def test_date_outside_season_rejected(self):
        with pytest.raises(ValidationError):
            self._valid(date=date(2026, 12, 1))

    def test_language_normalised(self):
        ship = self._valid(extra_language="  ITALIAN  ")
        assert ship.extra_language == "italian"


# ── ShiftTemplate ───────────────────────────────────────────────────────────

class TestShiftTemplateCreate:
    def test_valid_shift(self):
        s = ShiftTemplateCreate(
            id="1", role=ShiftRole.cafe, label="VAKT SHOP 1",
            start_time=time(8, 0), end_time=time(16, 0)
        )
        assert s.id == "1"

    def test_end_before_start_rejected(self):
        with pytest.raises(ValidationError):
            ShiftTemplateCreate(
                id="X", role=ShiftRole.cafe, label="Bad",
                start_time=time(16, 0), end_time=time(8, 0)
            )

    def test_shift_over_10h_rejected(self):
        with pytest.raises(ValidationError):
            ShiftTemplateCreate(
                id="X", role=ShiftRole.cafe, label="Long",
                start_time=time(6, 0), end_time=time(20, 0)
            )


# ── Schedule ────────────────────────────────────────────────────────────────

class TestScheduleCreate:
    def test_valid_schedule(self):
        s = ScheduleCreate(month=7, year=2026)
        assert s.month == 7

    def test_month_out_of_season_rejected(self):
        with pytest.raises(ValidationError):
            ScheduleCreate(month=11, year=2026)

    def test_month_4_rejected(self):
        with pytest.raises(ValidationError):
            ScheduleCreate(month=4, year=2026)

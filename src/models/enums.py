from enum import Enum


class RoleCapability(str, Enum):
    cafe = "cafe"
    production = "production"
    both = "both"


class EmploymentType(str, Enum):
    full_time = "full_time"
    part_time = "part_time"


class Housing(str, Enum):
    geiranger = "geiranger"
    eidsdal = "eidsdal"


class Season(str, Enum):
    low = "low"
    mid = "mid"
    peak = "peak"


class Port(str, Enum):
    geiranger_4B_SW = "geiranger_4B_SW"
    geiranger_3S = "geiranger_3S"
    geiranger_2 = "geiranger_2"
    hellesylt = "hellesylt"


class ShipSize(str, Enum):
    big = "big"
    small = "small"


class ShiftRole(str, Enum):
    cafe = "cafe"
    production = "production"


class ScheduleStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    archived = "archived"

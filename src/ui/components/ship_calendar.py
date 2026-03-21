"""Ship calendar component — Phase 4.

Renders a monthly calendar view showing cruise ship arrivals per day.
"""
from __future__ import annotations

import calendar
from datetime import date

import streamlit.components.v1 as components

from src.models.cruise_ship import CruiseShipRead

_DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_SHIP_BG = "#DDEEFF"
_GOOD_SHIP_BG = "#FFFACD"
_TODAY_BORDER = "#FF6B00"
_EMPTY_BG = "#FAFAFA"


def _ship_badge_html(ship: CruiseShipRead) -> str:
    bg = _GOOD_SHIP_BG if ship.good_ship else _SHIP_BG
    port_short = str(ship.port).split("_")[0].capitalize()
    good_star = " ⭐" if ship.good_ship else ""
    lang = f" [{ship.extra_language}]" if ship.extra_language else ""
    return (
        f"<div style='background:{bg}; border-radius:4px; padding:2px 4px; "
        f"margin:1px 0; font-size:10px; white-space:nowrap; overflow:hidden; "
        f"text-overflow:ellipsis;'>"
        f"<b>{ship.ship_name[:18]}</b>{good_star}<br>"
        f"<span style='color:#555;'>{port_short} {str(ship.arrival_time)[:5]}–{str(ship.departure_time)[:5]}{lang}</span>"
        f"</div>"
    )


def render_ship_calendar(
    year: int,
    month: int,
    ships: list[CruiseShipRead],
    height: int = 500,
) -> None:
    """Render a monthly calendar grid with ship badges per day."""
    # Group ships by date
    ships_by_date: dict[date, list[CruiseShipRead]] = {}
    for s in ships:
        if s.date.year == year and s.date.month == month:
            ships_by_date.setdefault(s.date, []).append(s)

    # Calendar matrix: list of weeks, each week is [day_or_0 x 7]
    cal = calendar.monthcalendar(year, month)
    today = date.today()
    month_name = date(year, month, 1).strftime("%B %Y")

    # Build HTML
    header_cells = "".join(
        f"<th style='padding:6px 4px; text-align:center; background:#4A90D9; "
        f"color:white; width:14%; font-size:12px;'>{d}</th>"
        for d in _DAYS_OF_WEEK
    )

    table_rows = ""
    for week in cal:
        row = ""
        for day_num in week:
            if day_num == 0:
                row += "<td style='background:#F0F0F0; border:1px solid #ddd; height:80px; vertical-align:top;'></td>"
                continue

            d = date(year, month, day_num)
            day_ships = ships_by_date.get(d, [])
            num_ships = len(day_ships)

            # Background intensity based on ship count
            if num_ships >= 3:
                bg = "#B3D4FF"
            elif num_ships == 2:
                bg = "#CCE4FF"
            elif num_ships == 1:
                bg = "#E3F0FF"
            else:
                bg = _EMPTY_BG

            border = f"2px solid {_TODAY_BORDER}" if d == today else "1px solid #ddd"

            badges = "".join(_ship_badge_html(s) for s in day_ships)
            count_badge = (
                f"<span style='background:#4A90D9; color:white; border-radius:10px; "
                f"padding:1px 5px; font-size:10px;'>{num_ships} ship{'s' if num_ships != 1 else ''}</span>"
                if num_ships > 0 else ""
            )

            row += (
                f"<td style='background:{bg}; border:{border}; height:90px; "
                f"vertical-align:top; padding:4px; width:14%;'>"
                f"<div style='font-weight:bold; font-size:13px; margin-bottom:2px;'>"
                f"{day_num} {count_badge}</div>"
                f"{badges}"
                f"</td>"
            )
        table_rows += f"<tr>{row}</tr>"

    html = (
        f"<div style='font-family: Segoe UI, sans-serif;'>"
        f"<h3 style='text-align:center; color:#333; margin-bottom:8px;'>{month_name}</h3>"
        f"<table style='border-collapse:collapse; width:100%;'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        f"</table>"
        f"<div style='margin-top:8px; font-size:11px; color:#777;'>"
        f"⭐ = Good ship &nbsp;&nbsp; "
        f"<span style='background:#FFFACD; padding:1px 4px; border-radius:3px;'>Gold</span> = Good ship &nbsp;&nbsp;"
        f"<span style='background:#DDEEFF; padding:1px 4px; border-radius:3px;'>Blue</span> = Regular ship"
        f"</div></div>"
    )

    components.html(html, height=height, scrolling=True)

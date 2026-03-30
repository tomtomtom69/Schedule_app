Read all docs in /docs/ to refresh context, then implement the following changes. Write all code without requesting approval for each step. Only ask before running system commands.
This is a major update covering constraint logic, solver priorities, LLM behavior, data model changes, and UI fixes. Work through each section in order.
---
SECTION 1: DATA MODEL UPDATE — EMPLOYEE AGE
Add a date_of_birth field to the Employee model (Pydantic + SQLAlchemy):
- Field type: date, optional (nullable — existing employees without it still work)
- Update the employee CSV parser to accept an optional date_of_birth column (format YYYY-MM-DD)
- Update the Employees UI page to show and edit date_of_birth
- Add a helper function get_age_on_date(date_of_birth: date, target_date: date) -> int that calculates age on any given day
- Add a helper function get_age_category(age: int) -> str returning "under_15", "age_15_18", or "adult"
- Run a DB migration to add the column without dropping existing data
---
SECTION 2: SHIFT HOUR COUNTING FIX
A shift template spans 8 hours (e.g., 08:00–16:00) but the employee works 7.5 hours + 0.5 hour mandatory break. Fix all hour calculations throughout the codebase:
- When counting hours worked per employee per week or month, count 7.5 hours per standard shift, NOT the template duration of 8 hours
- Exception: shift 6 (10:00–17:00) is 7 hours total, so 6.5 hours worked + 0.5 break
- Add a worked_hours property to the ShiftTemplate model that returns template duration minus 0.5 (the break)
- Update the solver, validator, summary stats, and Excel export to use worked_hours instead of raw duration
- The contracted 37.5 hours/week = exactly 5 × 7.5h shifts
---
SECTION 3: AGE-BASED CONSTRAINTS
Add age-specific hard constraints to the solver. On each day, calculate the employee's age and apply:
Under 15:
- Max 7 hours worked per day (only shift 6 is valid — 10:00–17:00, 6.5h worked)
- Max 35 hours per week
- No overtime, ever
Age 15–18:
- Max 8 hours worked per day (standard shifts OK, 7.5h worked)
- Max 40 hours per week
- No overtime, ever
Adult (18+):
- Normal rules: 7.5h standard day, max 10h absolute, 37.5h normal week, max 48h averaged
If an employee has no date_of_birth set, treat them as adult.
---
SECTION 4: STAFFING PRIORITY WATERFALL
This is the most important change. Rewrite the solver's objective function to follow this strict priority order:
Priority 1 (HARD — never violate): Meet minimum staffing requirements for every day. Never go below the minimum café or production count defined by the seasonal rules.
Priority 2 (HIGH): Fill good-ship cruise days first. On days with a good ship, assign the extra café staff (up to the good-ship staffing level) before filling any other optional shifts.
Priority 3 (HIGH): Fill regular cruise-ship days next. Assign the cruise-day staffing level.
Priority 4 (MEDIUM): Fill remaining no-cruise days to give full-time employees their 37.5h/week. Distribute shifts across quieter days to reach contracted hours.
Priority 5 (LOW): Only after all full-time hours are allocated, use part-time employees to fill remaining gaps.
This waterfall prevents over-scheduling: the solver fills high-demand days first, then uses leftover capacity for quieter days, rather than giving everyone maximum hours simultaneously.
---
SECTION 5: ROLE PRIORITY
Employees with role_capability="both" should default to production:
- The solver should assign "both" employees to production shifts first
- Only assign a "both" employee to a café shift if there are not enough café-only employees available to cover the café minimum on that day
- Implement as a high-weight soft constraint: penalize "both" employees on café shifts, reward them on production shifts
---
SECTION 6: MAXIMUM STAFFING CAPS
Add hard constraints for maximum staff per day:
- Max 5 café staff per day normally
- Max 6 café staff per day ONLY when multiple good ships are in port on the same day
- The solver must never exceed these caps, even if it means some full-timers get fewer hours that week
- Add equivalent configurable max for production (suggest default: 4, make it editable in Settings)
---
SECTION 7: SUNDAY AND PUBLIC HOLIDAY RULES
From Norwegian labor law (AML § 10-8):
- Over a rolling 26-week period, each employee must have on average every other Sunday off
- At least every 4th week, the weekly rest day must fall on a Sunday or public holiday
- Implementation: for each employee, track Sundays worked. Over any 26-week window, Sundays worked must be ≤ 13 (half of 26). Over any 4-week window, at least one Sunday must be a day off
- Norwegian public holidays in the season (May–October): May 1, May 17, Ascension Day (moveable), Whit Monday (moveable). Calculate these dynamically based on year.
---
SECTION 8: NEVER SCHEDULE OVERTIME
The system must never plan overtime by design:
- Always schedule 7.5-hour shifts (standard templates)
- The solver must target exactly 37.5 hours/week for full-time adults, never more
- If a full-timer cannot reach 37.5h in a given week without exceeding the café cap or other constraints, that's acceptable — under-hours is better than overtime
- The genomsnittsmetoden (averaging over 8 weeks) exists as a legal safety net, not as a planning tool. The solver should never deliberately use it
- For under-18: target their specific max (35h or 40h), never exceed
---
SECTION 9: REDUCED STAFFING ON SUNDAYS AND MONDAYS
On Sundays and Mondays with no cruise ship, the solver is allowed to go below normal minimum staffing:
- Café: can drop to 1 staff (instead of normal minimum of 2)
- Production: can drop to 0 (no production on quiet Sundays/Mondays)
- This is a soft constraint — the solver should prefer reduced staffing on these days to help keep full-timer hours balanced without over-scheduling
- If a cruise ship IS in port on a Sunday or Monday, normal cruise-day minimums apply
---
SECTION 10: LLM MUST RESPECT SHIFT TEMPLATES
Critical fix: The LLM chat is currently suggesting custom time blocks (e.g., "work 1 hour" or "3-hour shift") instead of using the predefined shift templates.
Fix:
- Update the LLM system prompt to explicitly list all valid shift IDs and their times. State clearly: "You may ONLY assign shifts from this list. Never suggest custom hours or partial shifts."
- In the action executor (the code that applies LLM suggestions), validate that the shift_id exists in the shift_templates table BEFORE applying. If the LLM suggests an invalid shift, reject it and show: "Invalid shift suggested — only predefined shifts are allowed."
- Update the prompt to explain that a standard shift = 7.5h worked + 0.5h break = 8h total. The only exception is shift 6 (7h total for under-15 employees).
- Include the shift legend in the system prompt context so the LLM always has it available
---
SECTION 11: UI — SHIFT LEGEND IN SIDEBAR
Add a collapsible shift legend to the left sidebar, visible on all pages:
- Use st.sidebar.expander("📋 Shift Legend") containing the shift template table
- Show shift ID, role, start time, end time, worked hours
- Load from database so it reflects any customizations
- Collapsed by default, user clicks to expand
- Place it below the navigation, above any page-specific sidebar content
---
SECTION 12: VALIDATION AND TESTING
After implementing all changes:
1. Regenerate August 2026 with the test data
2. Verify in the output that:
- Shifts 1, 2, 3, 4, and 5 are all used (not just 5)
- No employee exceeds 37.5h/week (counted as 7.5h per shift)
- Max 5 café staff on normal days, 6 only on multi-good-ship days
- "Both" employees are assigned to production unless café needs them
- Good-ship days have higher staffing than regular cruise days
- No-cruise Sundays/Mondays have reduced staffing
- Opening hours are covered (someone starts at shift 1 or 2 when café opens)
- The shift legend appears in the sidebar on all pages
3. Test the LLM chat: ask it to reassign someone. Confirm it only suggests valid shift template IDs
4. Log solver statistics: how many variables, constraints, solution status, total hours assigned
5. Report any constraints that are infeasible and suggest which ones to relax
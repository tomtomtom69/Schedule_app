"Three improvements: cruise ship upload polish, schedule archive versioning, and schedule status flow fix. Plus a verification test.
SECTION 1: CRUISE SHIP UPLOAD — APPEND OR REPLACE
Add the same append/replace choice that the employee upload has. When uploading a cruise ship file:
- If the database already has cruise ship records, show two buttons: 'Append (add new arrivals alongside existing)' and 'Replace All (delete existing and import fresh)'
- When choosing Replace, show a confirmation: 'This will replace [N] existing cruise ship records with [M] new ones from this file. Continue?'
- The current duplicate detection message ('126 arrivals already saved') is good — keep it, but show it as an info message alongside the append/replace choice rather than blocking the upload
- When choosing Append, only add ships that don't already exist (match on ship_name + date + arrival_time to detect duplicates)
SECTION 2: SCHEDULE ARCHIVE VERSIONING
When a month already has a saved schedule and the user generates a new one, archive the old one instead of overwriting:
- Add a 'version' field to the Schedule model (integer, starting at 1)
- Add an 'archived' status to ScheduleStatus enum (draft, approved, archived)
- When generating a new schedule for a month that already has one: set the existing schedule's status to 'archived', create the new schedule as 'draft' with version = previous + 1
- Before generating, show a warning dialog: 'You already have an approved schedule for [Month Year]. Generating a new schedule will archive the current one. You can switch back to it later.' Two buttons: 'Generate New Schedule' and 'Cancel'
- Add a version selector dropdown at the top of the schedule view: '[Month Year] — Draft v3 (generated today)' / '[Month Year] — Approved v2 (March 28)' / '[Month Year] — Archived v1 (March 25)'
- Loading an archived version shows it as read-only with a banner: 'This is an archived version. To make changes, generate a new schedule or restore this version.'
- Add a 'Restore This Version' button on archived schedules that copies it as a new draft
- The cross-month dependency check (loading last 6 days of previous month) should always use the APPROVED version, not drafts or archived versions
"In the schedule editor, remove the 'Apply Changes' button — it's confusing alongside 'Save Draft'. Cell edits should apply to the in-memory grid immediately when the user makes them. The user then clicks 'Save Draft' to persist to the database or 'Approve & Finalize' to persist and lock. Three buttons total: Save Draft, Approve & Finalize, Validate. Order them left to right: Validate (check first), Save Draft (save work), Approve & Finalize (done). Use distinct colors: Validate = neutral/grey, Save Draft = blue, Approve & Finalize = green."
SECTION 3: SCHEDULE STATUS FLOW FIX
The status flow between schedule tab and editor tab is broken. After approving a schedule, editing it in the editor, and saving as draft, the schedule tab still shows it as approved. Fix the complete flow:
- An approved schedule that gets edited in the editor MUST change status to 'draft (modified)' in the database when saved. It must NOT remain 'approved'
- The schedule tab must always re-query the current status from the database when loading — never rely on cached/session state for status
- Make the button flow in the editor crystal clear:
- 'Validate' = check constraints and show violations, does NOT save anything
- 'Save Draft' = save current state as draft, status becomes 'draft'. Show confirmation: 'Saved as draft. Return to Schedule tab to approve when ready.'
- 'Approve & Finalize' = save AND set status to approved. Show confirmation: 'Schedule approved and finalized. This version will be used for cross-month calculations.'
- After saving a draft in the editor, going back to the schedule tab MUST show status as 'draft', not 'approved'
- Add a visual status badge at the top of both the schedule tab and editor tab: green badge 'APPROVED' / yellow badge 'DRAFT' / grey badge 'ARCHIVED' — always visible, always current
- If the user is on the schedule tab viewing an approved schedule, the Generate button should say 'Generate New Schedule' (not just 'Generate') to make clear it creates a new version
SECTION 4: VERIFICATION TEST
Create a test script at tests/test_schedule_flow.py that programmatically tests the complete flow:
1. Create test employees and cruise ships in the database
2. Generate a schedule for July 2026 → verify status = 'draft', version = 1
3. Approve the schedule → verify status = 'approved'
4. Modify one assignment (change an employee's shift on one day) → save as draft → verify status = 'draft (modified)', version still 1
5. Approve again → verify status = 'approved'
6. Generate a new schedule for July 2026 → verify old schedule status = 'archived', new schedule status = 'draft', version = 2
7. Load the archived version → verify it's read-only
8. Restore the archived version → verify it creates a new draft, version = 3
9. Generate August 2026 → verify it loads last 6 days from July's APPROVED version (not archived or draft)
10. Print PASS/FAIL for each step
Run the test and report results.
Write all code without requesting approval. Only ask before running system commands."
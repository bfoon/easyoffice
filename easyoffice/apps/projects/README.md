# Project Milestones, Activity Log & Permissions — Patch Summary

This patch implements:

1. **Project owner** as a separate role from project manager
2. **Milestone assignment to a project member** that auto-creates a linked task with the same deadline
3. **Task completion notes** flow into the project activity log, linked to the milestone
4. **Manual activity-log entries** can be linked to a milestone
5. **Permission tightening** so only the right people can act on milestones, risks, dates, and the project itself
6. **Project closure** prevents further changes to milestones, risks, and activity log
7. **Date validation** so milestones cannot be set beyond the project end date
8. **Completed milestones** are locked from further edits

---

## Files in this delivery

| File | Drop-in location | Action |
|---|---|---|
| `projects_models.py` | `apps/projects/models.py` | Replace |
| `projects_views.py` | `apps/projects/views.py` | Replace |
| `project_detail.html` | `templates/projects/project_detail.html` | Replace |
| `migration_add_project_owner.py` | `apps/projects/migrations/00XX_project_owner.py` | New file |
| `migration_backfill_project_owner.py` | `apps/projects/migrations/00YY_backfill_project_owner.py` | New file |

The `urls.py`, `serializers.py`, and `tasks/views.py` files are unchanged — the existing routes and the task-side `sync_milestone_from_task` hook already do the right thing once the project-side helpers are upgraded.

---

## 1. Owner vs. Project Manager

A new `Project.owner` field has been added. The two roles are now distinct:

| Role | Can do |
|---|---|
| **Owner** (new) | Edit the project itself (name, dates, budget, status, transfer ownership). Plus everything the PM can do. |
| **Project Manager** | Assign milestones, change milestone status, change milestone due date, post activity-log updates, log/clear risks, reorder milestones. |
| **Team member** | View. Comment on tasks. (No milestone or risk powers.) |
| **Superuser** | Everything. |

When a project is created, the creator becomes the owner by default. The owner can transfer ownership later via the project edit form (you'll need to add an "Owner" picker to your `project_form.html` — see the *Project form template* note at the bottom).

The existing `_is_project_owner` helper now returns `True` for *both* the owner and the PM (since both have most powers). A new `_can_edit_project` helper enforces the stricter owner-only gate.

---

## 2. Database migration

Run after pinning the dependency in each migration file:

```bash
# Look up your latest existing projects migration
python manage.py showmigrations projects

# Edit migration_add_project_owner.py and replace
#   ('projects', '0001_initial')
# with the actual most recent migration name, e.g.
#   ('projects', '0023_alter_milestone_task')

# Then edit migration_backfill_project_owner.py and replace
#   ('projects', '00XX_project_owner')
# with the filename you just placed (without the .py)

# Move both files into apps/projects/migrations/ with the next sequential numbers
mv migration_add_project_owner.py      apps/projects/migrations/0024_project_owner.py
mv migration_backfill_project_owner.py apps/projects/migrations/0025_backfill_project_owner.py

python manage.py migrate projects
```

The backfill migration sets `owner = project_manager` for every existing project, so your current PMs keep their edit powers without manual cleanup.

---

## 3. Permission matrix (final)

| Action | Owner | PM | Team Member | Project Closed |
|---|:---:|:---:|:---:|:---:|
| Edit project (name, dates, budget…) | ✅ | ❌ | ❌ | 🔒 blocked |
| Transfer ownership | ✅ | ❌ | ❌ | 🔒 blocked |
| Add milestone | ✅ | ✅ | ❌ | 🔒 blocked |
| Assign milestone to a member | ✅ | ✅ | ❌ | 🔒 blocked |
| Change milestone due date | ✅ | ✅ | ❌ | 🔒 blocked |
| Change milestone status | ✅ | ✅ | ❌ | 🔒 blocked |
| Update completed milestone | ❌ | ❌ | ❌ | — (always locked) |
| Reorder milestones | ✅ | ✅ | ❌ | 🔒 blocked |
| Post activity-log update | ✅ | ✅ | ❌ | 🔒 blocked |
| Log / resolve / re-open risk | ✅ | ✅ | ❌ | 🔒 blocked |
| View project | ✅ | ✅ | ✅ | ✅ |
| Comment on linked tasks | ✅ | ✅ | ✅ | ✅ |

Closed = `status` is `completed` or `cancelled`.

---

## 4. Milestone → task auto-creation

When a milestone is created **with an assignee** or an existing milestone is **reassigned**, `_create_or_update_milestone_task()`:

- Creates a new task (or updates the existing linked task) with the same project, the milestone deadline, and the project's priority
- Adds the assignee to the project's `team_members` if they aren't already
- Sends the assignee a `CoreNotification` linking back to the project
- On reassignment, writes a `TaskReassignment` audit row so the task's reassignment history makes sense
- Posts an activity-log entry: *"Milestone X assigned to Y. A task has been created with the same deadline."*

The link is via `Milestone.task` (a `OneToOneField` to `Task`) — already in your model.

---

## 5. Activity log ↔ milestone linking

There are three ways an activity-log entry can be tied to a milestone:

1. **Auto on milestone assignment** — when a milestone is assigned or reassigned, `_log_milestone_activity` writes a ProjectUpdate row.
2. **Auto on task completion** — when the linked task is marked done with a completion note, `sync_milestone_from_task` (already in your code, called from `TaskCommentView`) copies the completion note into a ProjectUpdate row tied to the milestone. *No change needed here.*
3. **Manual** — the "Post Update" modal now has a *Link to Milestone* dropdown. The view (`ProjectUpdateStatusView`) was already accepting this parameter; the UI just needed wiring.

Every linked activity-log entry now displays a small purple milestone tag (`<i class="bi bi-flag-fill"></i> Milestone Name`) next to the author.

---

## 6. Date validation

`_validate_milestone_due_date()` rejects:

- Empty / unparseable dates
- Dates after `project.end_date` (if set)
- Dates before `project.start_date` (if set)

The Add-Milestone modal and the Edit-Milestone modal both use HTML5 `min` / `max` on the date input as a UX hint, but the server enforces the rule regardless of what the browser sent.

The `ProjectEditView` also rejects pulling the project end date *before* an existing milestone's due date (with a clear error message identifying which milestone is the blocker).

---

## 7. UI changes in `project_detail.html`

- **Closed-project banner** at the top when status is `completed` / `cancelled`
- **Edit button** in the header now uses `can_edit_project` (owner-only) and hides when project is closed
- **Add Milestone modal** now has an "Assign To" dropdown and a date picker constrained to the project window
- **Each milestone row** now shows the assignee avatar+name + a link icon to the linked task
- **A pencil button** next to each non-completed milestone opens an *Edit Milestone* modal (deadline + reassign)
- **The status `<select>`** is removed entirely for completed milestones — they're locked, with a small lock icon next to the name
- **Post Update modal** has a new "Link to Milestone" dropdown
- **Activity log entries** display a small flag tag when linked to a milestone
- **Risk Log/Resolve/Re-open buttons** now use `can_manage_risks` instead of `is_member`

---

## 8. New context variables exposed in `ProjectDetailView`

| Name | Meaning |
|---|---|
| `is_owner` | Strict owner-only check (true for superusers and the project's `owner`). |
| `can_edit_project` | Same as `is_owner` — alias used in the template for clarity. |
| `is_manager` | Owner-OR-PM (kept for backward compat with existing template usage). |
| `can_manage_milestones` | Owner-OR-PM AND project not closed. |
| `can_post_update` | Owner-OR-PM AND project not closed. |
| `can_manage_risks` | Owner-OR-PM AND project not closed. |
| `project_closed` | Convenience: `status in ('completed', 'cancelled')`. |

---

## 9. Project form template — manual update needed

Your `templates/projects/project_form.html` was not in the upload, so I haven't modified it. To let owners transfer ownership, add this near the project-manager picker:

```html
{% if mode == 'edit' %}
<div class="eo-form-group">
  <label class="eo-form-label">Project Owner</label>
  <select name="owner" class="eo-form-select">
    {% for staff in staff_list %}
    <option value="{{ staff.id }}" {% if project.owner_id == staff.id %}selected{% endif %}>
      {{ staff.full_name }}
    </option>
    {% endfor %}
  </select>
  <p class="text-muted" style="font-size:.78rem">
    Only the current owner can edit this project. Transferring ownership is irreversible
    from the new owner's side.
  </p>
</div>
{% endif %}
```

The view (`ProjectEditView.post`) already reads and saves the `owner` POST field.

---

## 10. Things explicitly NOT changed

- `urls.py` — no route changes needed
- `serializers.py` — both files were empty in your upload; nothing to change
- `tasks/views.py` — `TaskCommentView` already calls `sync_milestone_from_task`; that's unchanged
- The Gantt PDF generator — still works against the new fields without modification
- `_milestone_due_datetime` — unchanged; still stamps milestone deadlines at 17:00 local time when creating the linked task

---

## Quick test plan

1. Create a project as user A → A is owner & PM.
2. Try to edit as user B (a team member) → should be blocked with "Only the project owner…".
3. As A, add a milestone, assign it to user C → C gets a task notification, the task is visible at `/tasks/`, the activity log shows the assignment entry.
4. As C, mark the linked task done with a completion note → milestone transitions to Completed, activity log shows the completion note linked to the milestone.
5. Try to change the milestone status afterwards → blocked with "already completed".
6. As A, try to post an activity-log update with the milestone dropdown → entry shows in the log with the flag tag.
7. Move the project to `completed` status → banner appears, all milestone/risk/update buttons disappear.
8. Try to set a milestone date after the project's `end_date` → server rejects with the date error.

# Custom Forms app (`apps.customforms`)

Drag-and-drop form builder with configurable approval flows, signatures,
letterhead-branded exports, and PDF/Word downloads (empty or filled).

## What's included

```
apps/customforms/
├── __init__.py
├── apps.py
├── admin.py
├── models.py        FormTemplate · FormSubmission · SubmissionStep
├── views.py         builder SPA, JSON APIs, fill/submit, approvals, exports
├── urls.py
└── templates/customforms/
    ├── forms_home.html        dashboard: inbox · fill · drafts · my submissions
    ├── form_builder.html      drag-and-drop builder (fields / flow / settings tabs)
    ├── form_fill.html         submission page (renders any schema)
    └── submission_detail.html timeline, approve / sign / reject, downloads
```

## Install (4 steps)

1. Copy the `customforms` folder into your `apps/` directory.

2. `settings.py`:
   ```python
   INSTALLED_APPS = [
       # ...
       'apps.customforms',
   ]
   ```

3. Project `urls.py`:
   ```python
   path('forms/', include('apps.customforms.urls')),
   ```

4. Migrate:
   ```bash
   python manage.py makemigrations customforms
   python manage.py migrate
   ```

Then visit **/forms/**. "New form" opens the builder (visible to superusers
and staff profiles with role `ceo` / `office_admin` — the same rule as
letterheads).

## Feature map

| Requirement | Where |
|---|---|
| Drag-and-drop builder | `form_builder.html` — 16 field types incl. tables, choices, yes/no, currency; drag from palette or click; reorder by drag; half/third widths; duplicate; live preview |
| Approval flow | Flow tab — ordered steps; each step = **approve** or **approve + drawn signature** |
| Approver = specific person | assignee type "A specific person" (user search) |
| Approver = role / position / unit | dropdowns auto-populated from staff profiles; anyone matching can act, and the first person to act "claims" the step |
| Approver chosen by submitter | assignee type "Let the submitter choose" — fill page shows a person picker |
| Letterhead | Settings tab: none · company default (active) · a specific letterhead — stamped onto PDF/Word exports |
| Download empty | builder & home page → blank printable PDF/Word with signature/date lines per approval step |
| Download filled | submission page → PDF/Word with values, ☑ marks, approval outcomes, embedded signatures, comments |

## Things you may want to adjust

**Profile field names.** Role/position/unit matching reads
`user.staffprofile`. If your profile model uses different attribute names,
edit one dict at the bottom of `models.py`:

```python
PROFILE_ATTRS = {
    'role':     ['role'],
    'position': ['position', 'job_title', 'title'],
    'unit':     ['unit', 'department', 'dept'],
}
```

**Notifications.** When a step becomes pending, `FormSubmitView._notify_step`
is called (currently logs only). Plug your email system in there, and in
`SubmissionActionView.post` after `sub.advance()` if you want "next approver"
emails too.

**Exports.** Conversion uses `libreoffice --headless` exactly like the
letterhead exporter, so no new server dependency. If LibreOffice is missing
the API returns HTTP 501 with a clear message.

## Behaviour notes

- The schema is **snapshotted** onto each submission, so editing or even
  archiving a template never corrupts in-flight approvals.
- Steps run strictly in order; a rejection (comment required) finalises the
  submission and skips remaining steps.
- Deleting a template that has submissions archives it instead (submissions
  are protected by `on_delete=PROTECT`).
- Reference numbers are `FRM-<year>-0001` style, reset yearly.
- Submissions are visible to the submitter, managers, and anyone who is or
  was an approver on them — nobody else.

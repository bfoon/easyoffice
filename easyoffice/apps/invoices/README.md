# EasyOffice — Invoice Generator

A complete invoice / proforma / delivery note generator integrated with the
files app. CEO, Sales, HR and Admin groups have full access.

---

## Features

- **Three document types:** Invoice, Proforma Invoice, Delivery Note — each
  with its own yearly-reset sequence (`INV-2026-0001`, `PRO-2026-0001`,
  `DN-2026-0001`).
- **Letterhead from files:** pick any PDF you can access in the files app, or
  upload a new one in the same picker.
- **Draggable layout:** every block (title, number, client, items table,
  totals, bank details, notes) is free-drag on the letterhead preview.
  Positions auto-save as %-of-page so they survive zoom/resize.
- **Reusable templates** *(new)* — save any invoice as a template that
  captures the letterhead, layout, and defaults (bank/terms/currency/tax).
  Choose Personal or Shared visibility, and optionally lock the layout so
  template users can only fill in data.
- **Live table editor:** add/remove line items, tax %, discount — totals
  recompute live.
- **Auto-saving:** metadata, items and layout all save via AJAX with a
  debounced save indicator.
- **Finalize:** allocates the number, generates the PDF (overlays invoice
  content on the letterhead, repeats letterhead on overflow pages), saves it
  as a `SharedFile` in an auto-created `Invoices` folder, and locks the
  invoice.
- **Send for Signature:** finalized invoices can be sent through the
  existing e-signature flow (CEO, Sales, etc. as signers) with one click.
- **Void, don't delete:** finalized invoices can be voided (number preserved)
  or duplicated into a new draft.
- **Dashboard:** counts per type, total value this year, filters, search,
  signature-status column.

---

## Templates workflow

### Saving a template

1. Create or open any invoice draft and set up the layout, bank details,
   currency, tax rate, and payment terms the way you want them reusable.
2. Click **Save as Template…** in the builder header.
3. Enter a name, pick visibility (Personal / Shared), optionally enable
   **Lock layout for template users** and/or **Lock document type**.
4. The template is saved. You can keep editing and finalize the current
   invoice normally.

### What gets saved vs cleared

| Saved on template | Blank on each use |
|-------------------|-------------------|
| Letterhead PDF    | Client name / address / email |
| Block positions (layout_json) | Ship-to address |
| Currency          | PO reference |
| Tax rate          | Dates (invoice date / due date) |
| Payment terms     | Line items |
| Bank details      | Discount amount |
| Notes             |  |

### Using a template

- Go to **Invoices → New Invoice**. The first tab is now **Use a Template**,
  which shows your personal templates and shared ones from other users.
- Click a template card → a new draft invoice is created with all the
  template's defaults pre-filled. You jump straight into the builder.
- If the template has **Lock layout** enabled:
  - Blocks cannot be dragged
  - The Reset Layout button is hidden
  - A banner at the top explains which template is in use
  - Server-side enforcement returns 403 on any attempt to modify layout
- If the template has **Lock document type** set, the doc-type switcher is
  replaced with a locked display of the fixed type.

### Managing templates

- **Invoices → Templates** lists all templates with edit/use/delete buttons.
- Editing a template lets you change anything (name, visibility, lock flags,
  defaults). If the template is already in use by draft invoices, an
  "Also apply to existing drafts" checkbox lets you push the updated layout
  and defaults to those drafts retroactively. **Finalized invoices are never
  modified** — they are considered permanent records.
- Deleting a template simply unlinks it from any invoices that used it.
  The invoices keep all their data; they just lose the template reference.

### Permissions

- Any user with invoice access (CEO / Sales / HR / Admin / superuser) can
  create templates and use personal or shared ones.
- Only the owner (or a superuser) can edit or delete a template.

---

## Files delivered

```
apps/invoices/
├── __init__.py
├── apps.py
├── admin.py
├── forms.py
├── models.py
├── pdf_generator.py
├── permissions.py
├── services.py
├── urls.py
├── views.py
├── migrations/
│   └── __init__.py
├── templatetags/
│   ├── __init__.py
│   └── invoice_tags.py
└── templates/invoices/
    ├── dashboard.html
    ├── new.html
    ├── builder.html
    └── detail.html

file_manager.html          (drop-in replacement for apps/files/templates/files/file_manager.html)
```

---

## Installation

### 1. Copy the app into your project

```
cp -r apps/invoices /path/to/your/project/apps/
```

### 2. Replace your existing `file_manager.html`

```
cp file_manager.html /path/to/your/project/apps/files/templates/files/file_manager.html
```

This version adds:
- `{% load invoice_tags %}` at the top
- A gated "Invoice Generator" link in the Tools sidebar that only renders for
  users in CEO / Sales / HR / Admin groups (or superusers).

### 3. Add to `INSTALLED_APPS` (in `settings.py`)

```python
INSTALLED_APPS = [
    # ... your existing apps ...
    'apps.files',
    'apps.invoices',   # ← add this
    # ...
]
```

### 4. Include the URLs (in your root `urls.py`)

```python
urlpatterns = [
    # ... existing urls ...
    path('files/',    include('apps.files.urls')),
    path('invoices/', include('apps.invoices.urls')),   # ← add this
    # ...
]
```

### 5. Ensure required packages are installed

The invoice module uses these (all already used elsewhere in EasyOffice):

```
pypdf>=3.0
reportlab>=4.0
```

If not already in `requirements.txt`:

```
pip install pypdf reportlab
```

### 6. Run migrations

```
python manage.py makemigrations invoices
python manage.py migrate invoices
```

### 7. Create the access groups (if you don't have them)

The module grants access to users in groups named `CEO`, `Sales`, `HR`, `Admin`
(case-insensitive match). Create them via Django admin or shell:

```python
from django.contrib.auth.models import Group
for name in ['CEO', 'Sales', 'HR', 'Admin']:
    Group.objects.get_or_create(name=name)
```

Then add the appropriate users to those groups in Django admin.

### 8. Restart the app

That's it. Navigate to `/files/` — if you're a superuser or in one of the
groups, you'll see **Invoice Generator** in the Tools sidebar.

---

## First use — what happens automatically

The first time any user clicks "Finalize & Save PDF", the system will:

1. Create a top-level folder called **Invoices** in the files app
2. Set it to office-wide visibility
3. Grant FULL folder-share-access to every user in CEO / Sales / HR / Admin
   groups so they can all see/manage what's inside it
4. Save the generated PDF there as `INV-2026-0001.pdf` (or whatever the
   allocated number is)

---

## User flow

1. **Files app → Tools → Invoice Generator** → you land on the dashboard
2. **New Invoice** → pick document type (Invoice / Proforma / Delivery Note)
   and letterhead PDF
3. **Editor opens:** edit client info, line items, tax rate, bank details
   etc. in the right-hand panel; drag blocks on the PDF preview to reposition
   them. Everything auto-saves.
4. **Preview PDF** at any time to see the output in a new tab
5. **Finalize & Save PDF** — allocates the real number, generates the PDF,
   saves it to the `Invoices` folder, locks the record
6. From the detail view: download, duplicate (creates a new draft), or void

---

## Architecture notes

### Numbering is thread-safe

`InvoiceCounter.allocate_next()` uses `select_for_update()` inside an
`atomic` block so concurrent finalizes on the same `(doc_type, year)` pair
can't produce duplicate numbers. The number is only allocated on finalize,
not on draft create — so abandoned drafts don't burn numbers.

### Layout coordinates

Positions are stored as `{x_pct, y_pct, w_pct}` percentages of page
dimensions (with `y_pct` measured from the **top** of the page to match how
PDF.js renders in the browser). The reportlab backend converts to
bottom-origin when drawing. Defaults live in `pdf_generator.DEFAULT_LAYOUT`
and are merged with `invoice.layout_json` — so users can customise just the
blocks they move.

### Multi-page overflow

If line items don't fit on page 1, overflow items continue on page 2. The
letterhead is repeated on every page (as requested) — `build_invoice_pdf`
creates a fresh copy of the letterhead's page 1 and merges the overlay onto
each. Totals, bank details and notes always render on the **last** page.

### Permissions

The `InvoiceAccessMixin` is applied to every view in the app. The
`can_use_invoices()` helper in `permissions.py` is the single source of
truth — change access rules there if your group structure differs.

---

## Known limitations / future work

- Line items have no per-line tax or discount — tax is applied to the whole
  subtotal. Easy to extend in `InvoiceLineItem` if needed.
- No email-to-client action from the detail view yet. Pattern is already
  established in your signature flow — could be added as a follow-up.
- No "Mark as Paid" / payment tracking — this is an invoice generator, not
  an AR ledger.
- Drag-to-reposition is on the CONTAINER of each block. The container's
  width can also be edited via `w_pct` but there's no UI handle for that
  yet (stored in layout JSON, so future work can add edge-resize handles
  without a data migration).
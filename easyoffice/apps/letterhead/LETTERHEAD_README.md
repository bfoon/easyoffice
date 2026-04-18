# Letterhead Builder — Integration Guide

A Django app that lets **superusers, CEOs, and Office Admins** create,
save, and export company letterhead designs. The builder UI runs entirely
in the browser (HTML5 Canvas); this app provides the persistence and
export backend.

---

## File structure

```
apps/letterhead/
├── __init__.py
├── apps.py
├── admin.py
├── models.py          ← LetterheadTemplate model
├── views.py           ← all HTTP views + permission gate
├── urls.py            ← URL routing
└── migrations/
    └── 0001_initial.py

templates/letterhead/
└── builder.html       ← SPA shell (mount the JS builder here)
```

---

## 1 · Add to INSTALLED_APPS

```python
# settings.py
INSTALLED_APPS = [
    ...
    'apps.letterhead',
]
```

---

## 2 · Mount the URLs

```python
# project/urls.py
from django.urls import path, include

urlpatterns = [
    ...
    path('letterhead/', include('apps.letterhead.urls')),
]
```

---

## 3 · Run the migration

```bash
python manage.py migrate letterhead
```

---

## 4 · Serve media files (logo uploads)

Logos are uploaded to `MEDIA_ROOT/letterhead/logos/`.
Make sure your settings include:

```python
# settings.py
MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
```

```python
# project/urls.py  (development only — use nginx/S3 in production)
from django.conf import settings
from django.conf.urls.static import static

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
```

---

## 5 · Wire up the role check

`views._can_manage_letterhead()` checks `user.role` by default.
Adapt the two lines to match your user/profile model:

```python
# views.py  — _can_manage_letterhead()
role = getattr(user, 'role', None)          # if role is on the user model
# — OR —
role = getattr(user.profile, 'role', None)  # if role is on a Profile model
```

Allowed role values: `'ceo'` and `'office_admin'`. Django superusers
always pass regardless of role.

---

## 6 · Optional: server-side export dependencies

The `/export/pdf/` and `/export/png/` endpoints use optional libraries.
Install them if you want server-side rendering (the JS builder also
exports directly from the canvas without hitting these endpoints):

```bash
pip install Pillow reportlab
```

---

## URL reference

| Method   | URL                                            | What it does                      |
|----------|------------------------------------------------|-----------------------------------|
| GET      | `/letterhead/`                                 | Builder SPA shell                 |
| GET      | `/letterhead/api/templates/`                   | List all saved templates (JSON)   |
| POST     | `/letterhead/api/templates/`                   | Create a new template             |
| GET      | `/letterhead/api/templates/<uuid>/`            | Get one template                  |
| PUT      | `/letterhead/api/templates/<uuid>/update/`     | Update (full replace)             |
| DELETE   | `/letterhead/api/templates/<uuid>/delete/`     | Delete (cannot delete active)     |
| POST     | `/letterhead/api/templates/<uuid>/set-active/` | Make this the company default     |
| POST     | `/letterhead/api/templates/<uuid>/logo/`       | Upload logo (multipart `logo`)    |
| GET      | `/letterhead/api/templates/<uuid>/export/pdf/` | Download PDF                      |
| GET      | `/letterhead/api/templates/<uuid>/export/png/` | Download PNG                      |

Word (`.docx`) export is handled entirely in the browser via the JS builder
canvas → Blob — no server endpoint needed.

---

## JSON payload (create / update)

```json
{
  "name":            "Main Office",
  "template_key":    "classic",
  "company_name":    "Acme Corporation Ltd",
  "tagline":         "Innovation · Excellence · Integrity",
  "address":         "14 Independence Drive\nBanjul, The Gambia",
  "contact_info":    "+220 123 4567  |  info@acme.gm  |  www.acme.gm",
  "color_primary":   "#1D9E75",
  "color_secondary": "#2C2C2A",
  "font_header":     "Georgia",
  "font_body":       "Arial"
}
```

`template_key` options: `classic`, `modern`, `bold`, `minimal`, `split`, `elegant`

---

## Hooking the JS builder to the backend

The `builder.html` template injects `window.LETTERHEAD` with:

```js
window.LETTERHEAD = {
  activeTemplate: { /* to_builder_dict() of the active row, or null */ },
  apiBase:        "/letterhead/api/templates/",
  csrfToken:      "...",
};
```

Use these in your JS builder to:

1. **Pre-populate the form** from `window.LETTERHEAD.activeTemplate` on load.
2. **Save** by `POST`-ing to `apiBase` with `X-CSRFToken` header.
3. **Load saved designs** by `GET`-ing `apiBase` and listing them in a sidebar.
4. **Upload a logo** by `POST`-ing `multipart/form-data` to
   `{apiBase}{id}/logo/`.
5. **Export** by navigating to `{apiBase}{id}/export/pdf/` or `/export/png/`.

---

## Django Admin

`LetterheadTemplate` is registered in the admin. Superusers can:
- View / edit all templates
- Use the **"Set selected template as active"** bulk action
- See colour swatches in the list view

# Sales Orders app — installation guide

A new Django app at `apps/orders/` that bridges Customer Service and Invoices.

## What it does

- **Internal UI** for CS agents to enter phone / walk-in orders
- **Public API** at `/api/orders/` for the website / external integrations
- **Auto-fulfillment** — generates the full Proforma → Invoice → Delivery Note chain via the existing `apps.invoices.services.convert_invoice` and `finalize_invoice` helpers
- **Customer matching** — incoming website orders are linked by phone, then email, with a new Customer created on miss
- **Three auth methods** for the API: Api-Key header, DRF Token, or JWT
- **Reports integration** — adds an "Orders Report" tile to the reports dashboard (supervisor / admin tier)

## Installation

### 1. Drop the files in

Copy the entire `apps/orders/` folder into your project's `apps/` directory.

### 2. Add to `INSTALLED_APPS`

```python
INSTALLED_APPS = [
    # ...existing apps...
    'apps.customer_service',
    'apps.invoices',
    'apps.orders',                       # ← add this
    'rest_framework.authtoken',          # ← if not already present (for DRF Token)
    # ...
]
```

### 3. Wire up the URLs

In your project-level `urls.py`:

```python
urlpatterns = [
    # ...
    path('orders/',     include('apps.orders.urls')),
    path('api/',        include('apps.orders.api.urls')),
    # ...
]
```

### 4. Migrate

```bash
./manage.py migrate orders
```

### 5. Issue an API key for your website

```bash
./manage.py issue_order_api_key --user website-bot@yourcompany.com --name "Website production"
```

The command prints the raw key once. Copy it into your website's environment as `ORDERS_API_KEY`. Save it — you cannot retrieve it again from the database.

## Reports integration

The companion `apps.reports` files (`views.py`, `urls.py`, `templates/reports/dashboard.html`, `templates/reports/orders_report.html`) need to be replaced/updated. They add an `OrdersReportView` and a navigation card on the reports dashboard.

## API usage

### Endpoint

```
POST /api/orders/
```

### Authentication — pick one

**Option A: Api-Key header** (recommended for the website)

```http
Authorization: Api-Key sk_xxxxxxxxxxxxxxxxxxxx
```

**Option B: DRF Token**

```http
Authorization: Token <token>
```

**Option C: JWT** (uses your existing simplejwt setup)

```http
Authorization: Bearer <jwt-access-token>
```

### Example request

```bash
curl -X POST https://yourdomain.com/api/orders/?source=website \
  -H 'Authorization: Api-Key sk_xxxxxxxxxxxxxxxxxxxx' \
  -H 'Content-Type: application/json' \
  -d '{
    "external_ref": "WEB-CHK-12345",
    "idempotency_key": "WEB-CHK-12345",
    "contact_name": "Awa Jallow",
    "contact_phone": "+220 999 1234",
    "contact_email": "awa@example.com",
    "delivery_address": "12 Atlantic Road, Banjul",
    "currency": "GMD",
    "tax_rate": "15",
    "items": [
      {"description": "Solar panel 250W", "quantity": "2", "unit_price": "8500"},
      {"description": "Installation",     "quantity": "1", "unit_price": "1500"}
    ]
  }'
```

### Response (201 Created)

```json
{
  "id": "<uuid>",
  "order_no": "SO-20260426-A1B2C3",
  "status": "new",
  "customer_id": 42,
  "customer_name": "Awa Jallow",
  "subtotal": "18500.00",
  "tax_amount": "2775.00",
  "total": "21275.00",
  "items": [...],
  "proforma_no": null,
  "invoice_no": null,
  "delivery_note_no": null
}
```

The order arrives in CS as "New". An agent reviews, optionally confirms, then clicks **Fulfill** — that generates the three PDFs and links them to the order.

### Idempotency

Pass the same `idempotency_key` (e.g. your checkout ID) on retries. If we already have an order with that key, we return the existing order with `Idempotent-Replay: true` instead of creating a duplicate.

## Permissions

| Action               | Who can do it                                           |
|----------------------|---------------------------------------------------------|
| View / create orders | Customer Service, Sales, Supervisor, Admin, CEO         |
| Fulfill order        | Same set above                                          |
| Cancel order         | Same set above                                          |
| View Orders Report   | Supervisor, Admin, CEO, superuser, `is_staff`           |

The check accepts any of: Django group membership, `StaffProfile.position.title` keyword match, `is_staff`, or `is_superuser`.

## Files

```
apps/orders/
├── __init__.py
├── apps.py
├── admin.py
├── models.py
├── permissions.py
├── services.py            # fulfill_order, resolve_or_create_customer, ...
├── views.py               # internal HTML views
├── urls.py                # internal routes
├── forms.py
├── api/
│   ├── __init__.py
│   ├── authentication.py  # ApiKeyAuthentication
│   ├── models.py          # ApiKey (hashed)
│   ├── serializers.py
│   ├── urls.py
│   └── views.py
├── management/
│   └── commands/
│       └── issue_order_api_key.py
├── migrations/
│   └── 0001_initial.py
└── templates/
    └── orders/
        ├── dashboard.html
        ├── list.html
        ├── create.html
        └── detail.html

apps/reports/                      # updates only
├── views.py                       # +OrdersReportView
├── urls.py                        # +path('orders/', ...)
└── templates/reports/
    ├── dashboard.html             # +nav card
    └── orders_report.html         # new
```

## Notes / caveats

- **Letterhead picking** is best-effort: `fulfill_order` looks for a SharedFile whose name contains "letterhead", falling back to any PDF. If you have a designated letterhead system you want to plug in, edit `_build_proforma_from_order` in `services.py`.
- **No product catalog** — line items are free-text, per project decision. If you add a Product model later, plug it in via a new optional FK on `SalesOrderItem`.
- **No notification on new website order** — agents need to refresh the dashboard or check the list to see new entries. If you want push alerts (email, Slack, in-app `CoreNotification`), bolt them into `services.create_order_from_payload`.
- **No partial fulfillment** — fulfilling builds all three documents at once. Splitting an order into multiple invoices would need a new flow.

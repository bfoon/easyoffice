# EasyOffice — `apps.inventory`

A complete inventory and asset-management module for the EasyOffice Django
project. Tracks shop products for sale, office assets used daily, and
everything in between — with full QR-code labelling, automatic stock
debiting on order fulfilment, and an event-sourced audit trail.

This module follows the same conventions you’ll find in `apps.orders` and
`apps.finance`: UUID primary keys, services-layer business logic, soft
cross-app references, two-channel notifications (in-app + email), and
class-based views gated by group/title checks.

---

## What you get

**Products & stock**

- Two-level categories, suppliers, multi-location stock
- Three product kinds: stocked, service, bundle
- Optional batch / lot tracking with expiry
- Optional serial-number tracking
- Reorder points, reorder quantities, low-stock alerts
- Reservations (for orders) separate from on-hand quantity

**Movements (the ledger)**

- One append-only `StockMovement` table records every quantity change
- Ten movement kinds: receive, sale, internal use, transfer in/out,
  return in/out, adjustment in/out, scrap
- Negative on-hand allowed (real-world shrinkage); flagged in red
- Automatic stock debit when a `SalesOrder` is fulfilled

**Assets (office property)**

- Auto-generated `EO-AST-XXXXXX` tags
- Lifecycle: in store → assigned → under repair → retired → scrapped
- Assignment history with return condition
- Maintenance log with cost, description, next-due
- Straight-line depreciation, book-value report
- Disposal (`scrap_asset`) gated to CEO / superuser only

**Stock requests**

- Internal staff request form with multi-line items
- Manager can issue stock (with partial-fulfilment) or reroute to a
  `finance.PurchaseRequest` if items aren’t in stock
- Auto-reroute fall-through when issue fails on insufficient stock

**QR everywhere**

- Every product, asset, location, and stock-item has a unique
  `eo-<22-chars>` QR token assigned at creation
- Printable labels (A6, client-side QR rendering)
- Camera-based scanner page (jsQR) — point and resolve
- One unified `/inventory/q/<token>/` route routes any token to its detail
  page

**Cycle counts**

- Pre-fills expected counts from current stock
- One-click finalise emits `ADJUST_IN` / `ADJUST_OUT` movements for
  variances

**Reports**

- Stock valuation at cost
- Asset register with book values
- Both have print buttons that hide the chrome

**Public API**

- `Api-Key` authenticated REST endpoints under `/api/inventory/`
- ViewSets for products, assets, locations, suppliers, movements
- POST `/api/inventory/qr/resolve/` — pass any token, get the resource
- `manage.py issue_inventory_api_key` for key management

---

## Installation

1. Drop the `inventory/` folder into `apps/` so the path is `apps/inventory/`.

2. Add it to `INSTALLED_APPS` in your settings:

   ```python
   INSTALLED_APPS = [
       # ...
       'apps.inventory',
   ]
   ```

3. Wire the URLs in your project `urls.py`:

   ```python
   from django.urls import include, path

   urlpatterns = [
       # ...
       path('inventory/',     include('apps.inventory.urls')),
       path('api/inventory/', include('apps.inventory.api.urls')),
   ]
   ```

4. Run migrations:

   ```bash
   ./manage.py makemigrations inventory
   ./manage.py migrate
   ```

5. Issue your first API key (optional, for the public API):

   ```bash
   ./manage.py issue_inventory_api_key --user yourname --name "Warehouse scanner"
   ```

6. Visit `/inventory/` — that’s the dashboard.

---

## Permissions

The module reuses the project’s group-and-title pattern (lower-case names):

| Capability                          | Who                                                     |
| ----------------------------------- | ------------------------------------------------------- |
| View dashboard, products, scan QR   | Any authenticated user                                  |
| View own assigned assets / requests | Any authenticated user (only their own)                 |
| Create / edit products, locations, suppliers | `inventory_team`, `inventory_manager`, `admin` |
| Receive / issue / transfer stock    | `inventory_manager`, `admin`                            |
| Approve / reroute stock requests    | `inventory_manager`, `admin`                            |
| Scrap or dispose assets             | `ceo`, `admin`, superuser                               |
| Finalise cycle counts               | `inventory_manager`, `admin`                            |

Group and title sets are defined in `apps/inventory/permissions.py` and
mirror the patterns in `apps/orders/permissions.py`.

---

## QR workflow

The QR system is two-sided.

**Generate.** Every Product, Asset, Location, and StockItem gets a
`qr_token` of the form `eo-<22 url-safe chars>` when it’s created. Tokens
are unique per object and never change.

**Print.** From any product, asset, or location detail page, click *Print
label*. The label page renders an A6 ticket with the QR (drawn
client-side via the `qrcodejs` CDN) and a human-readable caption.

**Scan.** The `/inventory/scan/` page opens the camera (`jsQR` decoder),
auto-detects the next QR in view, and routes the user to the matching
detail page. Manual entry is available as a fallback.

**Resolve programmatically.** Hit `/inventory/q/<token>/` from any link
or deep-link. The resolver checks Asset, then Product, then StockItem,
then Location, and 302s to the right detail page.

**API.** `POST /api/inventory/qr/resolve/` with body `{"token": "eo-..."}`
returns the resource as JSON plus a `redirect_url`. Useful for native or
mobile scanners.

---

## Integration with other apps

### Orders → Inventory (auto stock debit)

When a `SalesOrder` transitions to `FULFILLED`, the inventory
`signals.py` notices the edge and calls
`services.on_sales_order_fulfilled()`. That function walks the order’s
line items, attempts to map each one to a Product (by SKU prefix, exact
name match, then fuzzy contains), and issues a `SALE` movement for each
match.

> **Caveat.** The current `apps.orders.SalesOrderItem` is free-text — it
> has no FK to a Product. Mapping is best-effort. The recommended next
> step is to add `product = ForeignKey(inventory.Product, null=True)` to
> the order line and wire it through; once present, `_match_product()`
> will use it directly.

### Finance ← Inventory (purchase request reroute)

If a manager tries to issue a stock request and the goods aren’t in
stock, they can click *Reroute to Purchase*. That creates a new
`finance.PurchaseRequest` (status = `SUBMITTED`) with a description
listing the unfulfilled lines, and marks the stock request as
`REROUTED`.

### Finance → Inventory (purchase delivery)

The reverse — automatically receiving stock when a `PurchaseRequest` is
marked delivered — is **not wired**, because the project’s
`PurchaseRequest` only carries a free-text description (no structured
lines). Instead, accountants should use the *Receive Stock* form on the
inventory side, which you can pre-fill manually. The bridge function
`services.on_purchase_delivered(purchase_request, lines=[...])` is ready
for the day structured lines arrive — just call it from a finance signal.

---

## Files

```
apps/inventory/
├── __init__.py
├── apps.py                — AppConfig.ready() wires signals
├── models.py              — all models, properties, and constants
├── services.py            — business logic; views and signals only call here
├── signals.py             — bridges to orders / finance
├── notifications.py       — in-app + email notifications
├── permissions.py         — predicates and view mixins
├── forms.py               — Django forms for HTML views
├── views.py               — class-based views, all gated
├── urls.py                — internal HTML routes
├── admin.py               — Django admin registrations
├── api/
│   ├── __init__.py
│   ├── authentication.py  — Api-Key authentication
│   ├── models.py          — ApiKey (hashed)
│   ├── serializers.py     — DRF serializers
│   ├── urls.py            — DRF router + qr/resolve
│   └── views.py           — viewsets that delegate to services
├── management/commands/
│   └── issue_inventory_api_key.py
└── templates/inventory/
    ├── dashboard.html
    ├── product_list.html, product_detail.html, product_form.html
    ├── location_list.html, location_detail.html, location_form.html
    ├── supplier_list.html, supplier_form.html
    ├── category_list.html, category_form.html
    ├── asset_list.html, asset_detail.html, asset_form.html
    ├── stock_request_list.html, stock_request_form.html, stock_request_detail.html
    ├── stocktake_list.html, stocktake_form.html, stocktake_detail.html
    ├── receive_stock.html, issue_stock.html, transfer_stock.html, movement_list.html
    ├── report_stock.html, report_assets.html
    └── scan.html, qr_label.html
```

All HTML extends `base.html` and uses your project’s `eo-*` CSS
classes (`eo-card`, `eo-page-header`, `eo-btn`, `eo-form-control`, etc.)
plus Bootstrap rows/cols and Bootstrap Icons. There are no new CSS files
to register.

---

## Design decisions worth knowing

- **Soft cross-app references.** `StockMovement.source_kind` /
  `source_ref` are strings (`"sales_order:abc-123"`) rather than
  generic FKs, so the inventory app loads even when other apps are mid-
  migration.

- **Append-only ledger.** `StockMovement` is never updated. Mistakes are
  reversed with a counter-movement. `StockItem.quantity` is the
  pre-aggregated cache; the ledger is the truth.

- **Negative on-hand is allowed.** Real warehouses have shrinkage
  before they have time to count it. The UI flags negative quantities
  in red rather than refusing the operation.

- **`ready()` failsafe.** The signals import in `apps.py` is wrapped in a
  bare `try/except` so a partially-loaded sister app cannot block
  inventory from loading.

- **Hashed API keys.** Raw keys are shown once and never stored. Lookup
  is O(1) via a 12-char prefix column.

---

## Roadmap (not built yet)

- Barcode scanning in the receive/issue forms (currently scan resolves
  to detail pages only)
- Forecasting reorder quantities from movement velocity
- Multi-currency stock valuation
- Replenishment runs that batch low-stock products into one PR
- Per-location user permissions (currently global)
- Asset depreciation methods other than straight-line
- Webhook notifications when stock crosses reorder

If you build any of these, the seams are ready: the services layer is
the only place you should be touching state, and the dashboard view is
where new alerts naturally surface.

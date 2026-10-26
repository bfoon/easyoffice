"""
apps/features/builtin.py
────────────────────────
Catalogue of the EasyOffice apps that existed before the feature system.
New apps should NOT be added here — give them their own feature_manifest.py.

Prefix notes
  • Mobile API routes are mapped per feature so switching off e.g. POS also
    closes /api/mobile/v1/pos/ for the Flutter app.
  • '/api/' alone belongs to the orders API (that is how easyoffice/urls.py
    mounts it); longer prefixes such as '/api/inventory/' win over it.
  • WebSocket routes (/ws/...) are not covered by HTTP middleware.
"""
from apps.features.registry import FeatureSpec, register

M = '/api/mobile/v1/'

BUILTIN = [
    # ── Platform (locked) ─────────────────────────────────────────────────
    FeatureSpec('core', 'Core platform', 'Login, profile, notifications, dashboard.',
                'bi-house-gear', 'Platform', ('/auth/', '/dashboard/', '/api/v1/', M + 'auth/',
                                               M + 'device-tokens/', M + 'users/', M + 'presence/'),
                locked=True, order=1),
    FeatureSpec('feature_control', 'Feature control', 'This control panel.',
                'bi-toggles', 'Platform', ('/features/',), locked=True, order=2),
    FeatureSpec('user_admin', 'User administration', 'Accounts, temp passwords, resets.',
                'bi-person-gear', 'Platform', ('/users/',), locked=True, order=3),
    FeatureSpec('admin_security', 'Admin security', 'Django admin kill-switch and OTP gate.',
                'bi-shield-lock', 'Platform', ('/admin-security/', '/security/'), locked=True, order=4),

    # ── Collaboration ─────────────────────────────────────────────────────
    FeatureSpec('tasks', 'Tasks', 'Task assignment, follow-ups and on-site tracking.',
                'bi-check2-square', 'Collaboration', ('/tasks/', M + 'tasks/')),
    FeatureSpec('meetings', 'Meetings', 'Scheduling, minutes, recordings and calls.',
                'bi-calendar-event', 'Collaboration', ('/meetings/',)),
    FeatureSpec('projects', 'Projects', 'Projects, surveys, tracking sheets and maps.',
                'bi-kanban', 'Collaboration', ('/projects/',)),
    FeatureSpec('messaging', 'Chat & calls', 'Real-time chat, polls and WebRTC calls.',
                'bi-chat-dots', 'Collaboration', ('/messages/', M + 'rooms/', M + 'messages/', M + 'polls/')),
    FeatureSpec('files', 'Files & e-signature', 'File manager, Collabora editing and signatures.',
                'bi-folder2-open', 'Collaboration', ('/files/', M + 'files/', M + 'sign/')),
    FeatureSpec('customforms', 'Forms', 'Form templates, submissions and approvals.',
                'bi-ui-checks', 'Collaboration', ('/forms/',)),
    FeatureSpec('letterhead', 'Letterhead builder', 'Letterhead design and apply tool.',
                'bi-file-earmark-richtext', 'Collaboration', ('/letterhead/',), requires=('files',)),

    # ── People ────────────────────────────────────────────────────────────
    FeatureSpec('staff', 'Staff & leave', 'Staff directory and leave requests.',
                'bi-people', 'People', ('/staff/',)),
    FeatureSpec('organization', 'Organization', 'Org chart and departments.',
                'bi-diagram-3', 'People', ('/organization/',)),
    FeatureSpec('hr', 'HR', 'Appraisals, payroll, onboarding, attendance.',
                'bi-person-badge', 'People', ('/hr/',), requires=('staff',)),
    FeatureSpec('poi', 'POI portal', 'Sandboxed portal for restricted external users.',
                'bi-incognito', 'People', ('/poi/',), requires=('messaging',)),

    # ── Finance & sales ───────────────────────────────────────────────────
    FeatureSpec('finance', 'Finance', 'Financial Control Center, budgets, payments.',
                'bi-cash-coin', 'Finance & sales', ('/finance/',)),
    FeatureSpec('sales_targets', 'Sales targets & rewards', 'Monthly/quarterly targets and rewards.',
                'bi-trophy', 'Finance & sales', ('/finance/sales-targets/',), requires=('finance',)),
    FeatureSpec('invoices', 'Invoices', 'Proforma → invoice → delivery note chain.',
                'bi-receipt', 'Finance & sales', ('/invoices/',), requires=('files',)),
    FeatureSpec('orders', 'Orders', 'Order pipeline, CEO signing and document chain.',
                'bi-cart-check', 'Finance & sales', ('/orders/', '/api/'), requires=('invoices',)),
    FeatureSpec('pos', 'Point of sale', 'Checkout, mobile money, receipts.',
                'bi-upc-scan', 'Finance & sales', ('/pos/', M + 'pos/'), requires=('inventory',)),
    FeatureSpec('marketing', 'Marketing', 'Campaigns, leads, CAC/LTV analytics.',
                'bi-megaphone', 'Finance & sales', ('/marketing/',)),
    FeatureSpec('opportunities', 'Procurement opportunities', 'Tender scanning and matching.',
                'bi-binoculars', 'Finance & sales', ('/opportunities/',)),

    # ── Operations ────────────────────────────────────────────────────────
    FeatureSpec('inventory', 'Inventory', 'Products, stock, assets and licences.',
                'bi-box-seam', 'Operations', ('/inventory/', '/api/inventory/')),
    FeatureSpec('logistics', 'Logistics', 'Fleet, drivers and shipments.',
                'bi-truck', 'Operations', ('/logistics/', '/drive/')),
    FeatureSpec('customer_service', 'Customer service', 'Tickets, live chat and call desk.',
                'bi-headset', 'Operations', ('/customer-service/',)),
    FeatureSpec('customer_portal', 'Customer portal', 'Client portal, on-site reports, technician portal.',
                'bi-door-open', 'Operations', ('/portal/', '/onsite/', '/tech/'),
                requires=('customer_service',)),
    FeatureSpec('it_support', 'IT support', 'IT helpdesk tickets.',
                'bi-pc-display', 'Operations', ('/it/',)),
    FeatureSpec('reports', 'Reports', 'Cross-module reports.',
                'bi-bar-chart-line', 'Operations', ('/reports/',)),
]

for _spec in BUILTIN:
    register(_spec)

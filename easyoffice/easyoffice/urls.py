from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView   # ← NEW
from apps.marketing.views_landing import LandingView, LandingEnquiryView

urlpatterns = [
    # Admin
    path('security/', RedirectView.as_view(pattern_name='security_dashboard', permanent=False)),  # ← NEW
    path('admin-security/', include('apps.admin_security.urls')),
    path('admin/', admin.site.urls),

    # Authentication
    path('auth/', include('apps.core.urls.auth')),

    # Public landing page (signed-in users are redirected to the dashboard).
    # Must stay above the root dashboard include so it wins for '/'.
    path('', LandingView.as_view(), name='landing'),
    path('enquiry/', LandingEnquiryView.as_view(), name='landing_enquiry'),

    # Dashboard
    path('dashboard/', include('apps.dashboard.urls')),
    path('', include('apps.dashboard.urls')),

    # Organization
    path('organization/', include('apps.organization.urls')),

    # Staff / HR
    path('staff/', include('apps.staff.urls')),
    path('hr/', include('apps.hr.urls')),

    # Tasks
    path('tasks/', include('apps.tasks.urls')),

    # Meeting
    path('meetings/', include('apps.meetings.urls')),

    # Projects
    path('projects/', include('apps.projects.urls')),

    # Messaging & Chat
    path('messages/', include('apps.messaging.urls')),

    # Files
    path('files/', include('apps.files.urls')),
    path('files/collabora/', include('apps.files.collabora.urls')),

    # Finance
    path('finance/', include('apps.finance.urls')),

    # IT Support
    path('it/', include('apps.it_support.urls')),

    # Reports
    path('reports/', include('apps.reports.urls')),
    path('orders/',     include('apps.orders.urls')),
    path('api/',        include('apps.orders.api.urls')),

    path('portal/', include('apps.customer_portal.urls')),
    path('onsite/', include('apps.customer_portal.urls_onsite_report')),
    path('tech/', include('apps.customer_portal.urls_technician_portal')),

    path('logistics/', include('apps.logistics.urls')),
    path('drive/', include('apps.logistics.urls_driver_portal')),
    path("poi/", include("apps.poi.urls")),

    # API
    path('api/v1/', include('apps.core.urls.api')),
    path('letterhead/', include('apps.letterhead.urls')),
    path(
        'invoices/',
        include(('apps.invoices.urls', 'invoices'), namespace='invoices')
    ),
    path('api/mobile/v1/', include('apps.mobile_api.urls')),
    path('opportunities/', include('apps.opportunities.urls')),
    path('customer-service/', include('apps.customer_service.urls')),
    path('users/', include('apps.user_admin.urls', namespace='user_admin')),
    path('inventory/',     include('apps.inventory.urls')),
    path('api/inventory/', include('apps.inventory.api.urls')),
    path('pos/', include('apps.pos.urls')),
    path('forms/', include('apps.customforms.urls')),
    path('marketing/', include('apps.marketing.urls')),

    # Feature control (superusers) — switch modules on/off/pilot
    path('features/', include('apps.features.urls')),

    # Investments: staff management + investor portal
    path('investments/', include('apps.investments.urls_admin')),
    path('invest/', include('apps.investments.urls_portal')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Customize admin
admin.site.site_header = 'EasyOffice Administration'
admin.site.site_title = 'EasyOffice Admin'
admin.site.index_title = 'Office Management'

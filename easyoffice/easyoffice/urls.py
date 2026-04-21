from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Authentication
    path('auth/', include('apps.core.urls.auth')),

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

    # API
    path('api/v1/', include('apps.core.urls.api')),
    path('letterhead/', include('apps.letterhead.urls')),
    path('invoices/', include('apps.invoices.urls')),
    path('opportunities/', include('apps.opportunities.urls')),
    path('customer-service/', include('apps.customer_service.urls')),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Customize admin
admin.site.site_header = 'EasyOffice Administration'
admin.site.site_title = 'EasyOffice Admin'
admin.site.index_title = 'Office Management'

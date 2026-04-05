from django.urls import path, include

urlpatterns = [
    path('auth/', include('apps.core.api_urls')),
    path('staff/', include('apps.staff.api_urls')),
    path('tasks/', include('apps.tasks.api_urls')),
    path('projects/', include('apps.projects.api_urls')),
    path('messages/', include('apps.messaging.api_urls')),
    path('files/', include('apps.files.api_urls')),
    path('finance/', include('apps.finance.api_urls')),
    path('hr/', include('apps.hr.api_urls')),
    path('it/', include('apps.it_support.api_urls')),
    path('org/', include('apps.organization.api_urls')),
]

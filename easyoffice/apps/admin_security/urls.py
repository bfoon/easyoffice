"""
apps/admin_security/urls.py
───────────────────────────
Wire into your project urls.py BEFORE the admin include:

    from django.urls import include, path
    urlpatterns = [
        path('admin-security/', include('apps.admin_security.urls')),
        path('admin/', admin.site.urls),
        ...
    ]
"""
from django.urls import path
from . import views

app_name = 'admin_security'

urlpatterns = [
    path('settings/',     views.settings_view,       name='settings'),
    path('otp/',          views.otp_challenge_view,  name='otp_challenge'),
    path('otp/resend/',   views.resend_otp_view,     name='otp_resend'),
]

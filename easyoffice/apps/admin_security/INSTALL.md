# Admin Security — install & wiring

A small Django app that adds two things to your project:

1. **A kill-switch for `/admin/`.** Toggle it off and the Django admin
   becomes inaccessible to everyone except the superuser who turned it
   off (so you can never lock yourself out).
2. **Email OTP at admin login.** When enabled, every admin user enters
   their password, gets a 6-digit code emailed to them, and has to type
   it on a verification page before they can reach the admin.

Both flags are flipped from a settings page (superuser-only) at
`/admin-security/settings/`.

---

## 1. Drop the app into your project

Place the `admin_security` folder under your project's `apps/` directory
so the import path is `apps.admin_security`. (If your app folder is
named differently, adjust the `name` in `apps.py` and the dotted paths
in `MIDDLEWARE` / `INSTALLED_APPS` accordingly.)

## 2. Add to `INSTALLED_APPS`

```python
INSTALLED_APPS = [
    ...
    'apps.admin_security.apps.AdminSecurityConfig',
]
```

## 3. Add the middleware

Both classes must come **after** `AuthenticationMiddleware`:

```python
MIDDLEWARE = [
    ...
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.admin_security.middleware.AdminKillSwitchMiddleware',
    'apps.admin_security.middleware.AdminOTPGateMiddleware',
    ...
]
```

## 4. Wire the URLs

In your **project** `urls.py`, before the admin include:

```python
from django.urls import include, path
from django.contrib import admin

urlpatterns = [
    path('admin-security/', include('apps.admin_security.urls')),
    path('admin/', admin.site.urls),
    ...
]
```

## 5. Configure email

Anything that works with `django.core.mail` is fine. For development:

```python
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'no-reply@yourdomain.com'
```

For production, point at your real SMTP / SendGrid / SES backend.
**Make sure every staff user has an email address** — without one, OTP
can't be delivered and they'll see a "contact your administrator"
message instead of getting in.

## 6. Run migrations

```bash
python manage.py migrate admin_security
```

## 7. Open the settings page

As a superuser, go to:

    http://your-site/admin-security/settings/

Toggle the switches. That's it.

---

## How the lockout-prevention works

When you flip **"Admin portal enabled"** to OFF, the row also stores
your user ID as `last_changed_by`. The middleware *always* lets that
user reach the admin, even with the switch off — so you can flip it
back on. Other admins (including other superusers) get the "admin is
disabled" page.

If you somehow lose access (deleted user, etc.), you can re-enable from
the Django shell:

```python
python manage.py shell
>>> from apps.admin_security.models import AdminAccessSetting
>>> s = AdminAccessSetting.load()
>>> s.is_admin_enabled = True
>>> s.require_otp_for_admin = False
>>> s.save()
```

## Optional: custom admin URL prefix

If your admin lives at `/control-panel/` instead of `/admin/`, set in
`settings.py`:

```python
ADMIN_URL_PREFIX = '/control-panel/'
```

Both middlewares respect this.

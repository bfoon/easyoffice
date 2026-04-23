# apps.user_admin — User Administration

A self-contained Django app giving CEO / Superuser / Administration-group
members a full interface for managing company accounts.

## Features

- **Dashboard** — paginated staff roster with search + filters (status,
  group, locked-out).
- **Create user** (two modes):
  - Email invitation — recipient sets their own password.
  - Immediate creation with a temporary password shown once to the admin.
- **User actions**:
  - Email password-reset link
  - Generate a new temporary password (forces change on next login)
  - Block / unblock / change status / terminate
  - Unlock accounts stuck behind a failed-login lockout
  - Manage groups (access roles)
  - Hard-delete (superuser only)
- **Invitations panel** — list, resend, revoke.
- **Registration links** — bulk onboarding. Shareable link, N uses, M days.
- **Forced password change** — after logging in with a temp password,
  middleware routes the user to `/users/change-password/` until they set
  a new one.
- **Audit trail** — every admin action is logged via the existing
  `core.AuditLog` model; shown on each user's detail page.

## Install

1. **Drop the `user_admin` folder** into `apps/` so you have
   `apps/user_admin/`.

2. **Add to `INSTALLED_APPS`** in `settings.py`:

   ```python
   INSTALLED_APPS = [
       ...
       'apps.user_admin',
   ]
   ```

3. **Add the middleware** (AFTER `AuthenticationMiddleware`):

   ```python
   MIDDLEWARE = [
       ...
       'django.contrib.auth.middleware.AuthenticationMiddleware',
       ...
       'apps.user_admin.middleware.PasswordChangeRequiredMiddleware',
       ...
   ]
   ```

4. **Wire the URLs** in your project `urls.py`:

   ```python
   path('users/', include('apps.user_admin.urls', namespace='user_admin')),
   ```

5. **(Optional) Add the context processor** so templates can show the
   admin link conditionally:

   ```python
   TEMPLATES = [{
       ...
       'OPTIONS': {
           'context_processors': [
               ...
               'apps.user_admin.context_processors.admin_flags',
           ],
       },
   }]
   ```

   Then in your base template:

   ```html
   {% if is_user_admin %}
       <a href="{% url 'user_admin:dashboard' %}">User Administration</a>
   {% endif %}
   ```

6. **Migrate**:

   ```bash
   python manage.py makemigrations user_admin
   python manage.py migrate user_admin
   ```

7. **Create the admin groups** in Django admin (`/admin/auth/group/`):
   - `CEO`
   - `Administration`

   Add any user you want to have access to the console to one of these
   groups. Superusers always have access.

## Email configuration

Invitations and password-reset emails use Django's `send_mail`. Make sure
your `EMAIL_BACKEND`, `EMAIL_HOST`, and `DEFAULT_FROM_EMAIL` settings are
configured. If `SITE_BASE_URL` is set, invitation emails will use it to
build absolute URLs even for management commands. Otherwise the request's
host is used.

Password-reset emails assume you have Django's built-in
`django.contrib.auth.views.PasswordResetConfirmView` wired up at
`/reset/<uidb64>/<token>/`. If not, add to your root urls:

```python
path('reset/<uidb64>/<token>/',
     django.contrib.auth.views.PasswordResetConfirmView.as_view(
         success_url='/reset/done/'),
     name='password_reset_confirm'),
```

## URL summary

| Path                                         | Who              | Purpose                          |
|----------------------------------------------|------------------|----------------------------------|
| `/users/`                                    | admins           | Dashboard                        |
| `/users/create/`                             | admins           | New user form                    |
| `/users/u/<uuid>/`                           | admins           | User detail + actions            |
| `/users/u/<uuid>/action/`                    | admins (POST)    | Action endpoint                  |
| `/users/invitations/`                        | admins           | List invitations                 |
| `/users/invitations/<uuid>/action/`          | admins (POST)    | Resend/revoke                    |
| `/users/links/`                              | admins           | Registration links               |
| `/users/links/create/`                       | admins (POST)    | Create registration link         |
| `/users/links/<uuid>/action/`                | admins (POST)    | Revoke/extend link               |
| `/users/invite/<token>/`                     | anyone (token)   | Accept an invitation             |
| `/users/register/<token>/`                   | anyone (token)   | Self-register via shared link    |
| `/users/change-password/`                    | logged-in user   | Forced temp-pw change            |

## Data retention note

Temporary passwords are never stored in plaintext server-side after the
admin has seen them once. What we persist is a `TempPassword` row with
only the last 4 chars as a hint, purely for audit-trail display. When the
user changes their password, all outstanding `TempPassword` rows for
them flip `is_consumed=True`.

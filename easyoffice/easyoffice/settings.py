"""
EasyOffice Django Settings
"""
import os
from pathlib import Path
from decouple import config, Csv
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================
# CORE
# =============================================================
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-this-key')
DEBUG = config('DEBUG', default=True, cast=bool)

# 🔒 'web' is REQUIRED in production: the Collabora container calls the
# WOPI endpoints with Host: web:8000 over the docker network. Without it,
# every WOPI callback 400s and the editor shows "Failed to load document".
# Set via .env:  ALLOWED_HOSTS=easyoffice.gm,www.easyoffice.gm,web,localhost
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

# 🔒 Required for POSTs (login, chat sends, CSRF checks) when the site is
# served over HTTPS on its real domain. Without this, Django 4+ rejects
# cross-origin-looking POSTs from https://easyoffice.gm with a CSRF error.
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://easyoffice.gm',
    cast=Csv(),
)

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
]

THIRD_PARTY_APPS = [
    'channels',
    'rest_framework',
    'rest_framework_simplejwt',
    'django_filters',
    'crispy_forms',
    'crispy_bootstrap5',
    'widget_tweaks',
    'django_celery_beat',
    'django_celery_results',
    'django_extensions',
]

LOCAL_APPS = [
    'apps.core',
    'apps.organization',
    'apps.staff',
    'apps.tasks',
    'apps.projects',
    'apps.messaging',
    'apps.files',
    'apps.finance',
    'apps.hr',
    'apps.it_support',
    'apps.dashboard',
    'apps.reports',
    'apps.meetings',
    'apps.files.collabora',
    'apps.letterhead',
    'apps.invoices',
    'apps.opportunities',
    'apps.customer_service',
    'apps.user_admin',
    'apps.orders',
    'apps.inventory',
    'apps.customer_portal',
    'apps.logistics',
    'apps.poi',
    'apps.pos',
    'apps.customforms',
    'apps.marketing',
    'apps.admin_security.apps.AdminSecurityConfig',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.admin_security.middleware.AdminKillSwitchMiddleware',
    'apps.admin_security.middleware.AdminOTPGateMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.ActiveUserMiddleware',
    'apps.core.middleware.OfficeBrandingMiddleware',
    'apps.user_admin.middleware.PasswordChangeRequiredMiddleware',
    'apps.poi.middleware.POIConfinementMiddleware',
]

ROOT_URLCONF = 'easyoffice.urls'
ASGI_APPLICATION = 'easyoffice.asgi.application'
WSGI_APPLICATION = 'easyoffice.wsgi.application'

AUTH_USER_MODEL = 'core.User'
LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/auth/login/'

# =============================================================
# TEMPLATES
# =============================================================
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.office_settings',
                'apps.core.context_processors.unread_notifications',
                'apps.messaging.context_processors.unread_messages',
                'apps.user_admin.context_processors.admin_flags',
            ],
        },
    },
]

# =============================================================
# DATABASE
# =============================================================
DATABASES = {
    'default': {
        'ENGINE': config('DB_ENGINE', default='django.db.backends.postgresql'),
        'NAME': config('DB_NAME', default='easyoffice'),
        'USER': config('DB_USER', default='easyoffice'),
        'PASSWORD': config('DB_PASSWORD', default='easyoffice_secret'),
        'HOST': config('DB_HOST', default='db'),
        'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

# =============================================================
# CACHE & SESSIONS
# =============================================================
REDIS_URL = config('REDIS_URL', default='redis://redis:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'TIMEOUT': 300,
    }
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'

# =============================================================
# CHANNELS (WebSocket)
# =============================================================
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_URL],
        },
    },
}

# =============================================================
# CELERY
# =============================================================
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://redis:6379/1')
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://redis:6379/2')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = config('TIME_ZONE', default='UTC')
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_TASK_ROUTES = {
    'apps.messaging.tasks.*': {'queue': 'notifications'},
    'apps.reports.tasks.*': {'queue': 'reports'},
}

# =============================================================
# STATIC & MEDIA
# =============================================================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = config('MEDIA_URL', default='/media/')
MEDIA_ROOT = config('MEDIA_ROOT', default=str(BASE_DIR / 'media'))

MAX_UPLOAD_SIZE = config('MAX_UPLOAD_SIZE', default=50, cast=int) * 1024 * 1024

# 🔒 Enforce the same ceiling at the request-parsing layer so oversized
# bodies are rejected before views ever run (memory-exhaustion guard).
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE
FILE_UPLOAD_MAX_MEMORY_SIZE = min(MAX_UPLOAD_SIZE, 10 * 1024 * 1024)

# 🔒 PROTECTED MEDIA (chat attachments, in-call presentations)
# These folders under MEDIA_ROOT contain private conversation content and
# are marked `internal` in nginx — they can only be served via an
# X-Accel-Redirect issued by an authenticated Django view (which
# re-checks room membership on every request). Direct URL access 404s.
# The helper lives in apps/core/xaccel.py.
PROTECTED_MEDIA_PREFIXES = ['chat_uploads/', 'presentations/']

# True in production (nginx does the file transfer, Django only
# authorizes). False in local dev where there is no nginx — Django then
# streams the file itself via FileResponse.
MEDIA_USE_XACCEL = config('MEDIA_USE_XACCEL', default=not DEBUG, cast=bool)

# =============================================================
# EMAIL
# =============================================================
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='EasyOffice <noreply@easyoffice.com>')


# Adjust to your project. Format is 'app_label.ModelName'.
CUSTOMER_PORTAL_CONTRACT_MODEL = 'finance.Contract'   # or 'finance.Contract'
CUSTOMER_PORTAL_TASK_MODEL = 'tasks.Task'
CUSTOMER_PORTAL_TICKET_MODEL = 'customer_service.ServiceTicket'
CUSTOMER_PORTAL_ASSIGNMENT_MODEL = 'customer_service.ServiceTicketAssignment'
CUSTOMER_PORTAL_CUSTOMER_MODEL = 'customer_service.Customer'

# Used by email templates
ORGANISATION_NAME    = 'Easy Solutions'
ORGANISATION_ADDRESS = 'Kairaba Avenue, Serrekunda, The Gambia'
ORGANISATION_PHONE   = '+220 …'
SITE_URL = 'https://easyoffice.gm'
SUPPORT_EMAIL = 'support@easysolutions.gm'

# =============================================================
# AUTH & SECURITY
# =============================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

SESSION_COOKIE_AGE = 86400 * 7  # 7 days
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=False, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=False, cast=bool)

# 🔒 Explicit hardening (HTTPONLY is Django's default for sessions, but
# stating it makes the intent auditable; the referrer policy stops chat
# URLs — which contain room UUIDs — leaking to external sites via links).
SESSION_COOKIE_HTTPONLY = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CONTENT_TYPE_NOSNIFF = True

# =============================================================
# HTTPS / HSTS  (set to True in production via env vars)
# =============================================================
SECURE_SSL_REDIRECT            = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SECURE_HSTS_SECONDS            = config('SECURE_HSTS_SECONDS', default=0, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False, cast=bool)
SECURE_HSTS_PRELOAD            = config('SECURE_HSTS_PRELOAD', default=False, cast=bool)
SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')

# =============================================================
# MESSAGING ENCRYPTION (at-rest, Fernet = AES-128-CBC + HMAC-SHA256)
# =============================================================
# NOTE: the old comment here said "AES-256" — Fernet is AES-128-CBC with
# an HMAC-SHA256 authenticity tag. Still strong; just labelled correctly.
#
# Generate a key once and store it as an environment variable:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Never commit the key value to source control.
MESSAGING_ENCRYPTION_KEY = config('MESSAGING_ENCRYPTION_KEY', default='')

# 🔒 Fail-closed: if encryption ever fails (missing/typo'd key on a
# worker), REJECT the message save instead of silently storing plaintext.
# Only flip to True as a temporary, deliberate emergency measure.
MESSAGING_ENCRYPTION_FAIL_OPEN = config(
    'MESSAGING_ENCRYPTION_FAIL_OPEN', default=False, cast=bool
)

# 🔒 Push/email previews: False (default) sends a generic "New message"
# instead of copying plaintext message content through Firebase and mail
# servers — which would defeat the at-rest encryption above. Set True
# only if you consciously accept that trade-off for nicer notifications.
MESSAGING_NOTIFY_INCLUDE_PREVIEW = config(
    'MESSAGING_NOTIFY_INCLUDE_PREVIEW', default=False, cast=bool
)

# 🔒 WebSocket auth: allow the DEPRECATED ?token= query-string fallback
# while mobile clients migrate to the "Authorization: Bearer" header
# (query strings leak JWTs into nginx/proxy access logs). Set to False
# once all clients are updated.
MESSAGING_WS_ALLOW_QUERY_TOKEN = config(
    'MESSAGING_WS_ALLOW_QUERY_TOKEN', default=True, cast=bool
)

# =============================================================
# COLLABORA ONLINE (collaborative Word/Excel/PowerPoint editor)
# =============================================================
# Three DISTINCT URLs — do not conflate them:
#
#   COLLABORA_SERVER_URL  Django → Collabora   (internal docker network)
#   COLLABORA_PUBLIC_URL  Browser → nginx → Collabora  (the iframe host)
#   COLLABORA_WOPI_HOST   Collabora → Django   (internal docker network)

# INTERNAL: used only by Django to fetch /hosting/discovery. Plain HTTP
# over the docker network is correct here — the browser never sees it.
COLLABORA_SERVER_URL = config('COLLABORA_SERVER_URL', default='http://collabora:9980')

# PUBLIC: origin the browser loads the editor iframe from. With Collabora
# mounted at root paths behind nginx (/browser/, /cool/) this is simply
# the site itself.
COLLABORA_PUBLIC_URL = config('COLLABORA_PUBLIC_URL', default=SITE_URL)

# INTERNAL: base URL Collabora uses to call the WOPI endpoints back.
# Must match the Collabora container's aliasgroup1 exactly, and its host
# ('web') must be present in ALLOWED_HOSTS.
COLLABORA_WOPI_HOST = config('COLLABORA_WOPI_HOST', default='http://web:8000')

# WOPI token lifetime in seconds. 10 hours is generous enough for long
# editing stints; tokens are narrowly scoped (one file, one user, one
# permission level) so a long TTL is safe.
COLLABORA_TOKEN_TTL_SECONDS = config('COLLABORA_TOKEN_TTL_SECONDS', default=36000, cast=int)

# Dedicated HMAC secret for WOPI tokens. Optional — falls back to
# SECRET_KEY if unset. Setting a dedicated secret lets you rotate it
# (invalidating all outstanding editor sessions) without logging
# everyone out of the rest of the site.
# Generate with:  python -c "import secrets; print(secrets.token_urlsafe(48))"
COLLABORA_JWT_SECRET = config('COLLABORA_JWT_SECRET', default='')

# NOTE: COLLABORA_EDITOR_PATH was removed — the editor entry path is now
# resolved at runtime from /hosting/discovery (it contains a version hash
# that changes on every Collabora image upgrade, so it must never be
# hardcoded).

# =============================================================
# REST FRAMEWORK
# =============================================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': config('DEFAULT_PAGE_SIZE', default=25, cast=int),
    # 🔒 The browsable HTML API is a great dev tool but hands production
    # visitors a point-and-click explorer of every endpoint. JSON-only in
    # production; browsable only when DEBUG.
    'DEFAULT_RENDERER_CLASSES': (
        [
            'rest_framework.renderers.JSONRenderer',
            'rest_framework.renderers.BrowsableAPIRenderer',
        ] if DEBUG else [
            'rest_framework.renderers.JSONRenderer',
        ]
    ),
}

# =============================================================
# CRISPY FORMS
# =============================================================
CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap5'
CRISPY_TEMPLATE_PACK = 'bootstrap5'

# =============================================================
# SIMPLE HISTORY
# =============================================================
SIMPLE_HISTORY_REVERT_DISABLED = False

# =============================================================
# NOTIFICATIONS
# =============================================================
DJANGO_NOTIFICATIONS_CONFIG = {
    'USE_JSONFIELD': True,
    'SOFT_DELETE': True,
}

# =============================================================
# OFFICE CONFIGURATION (configurable per deployment)
# =============================================================
OFFICE_CONFIG = {
    'NAME': config('OFFICE_NAME', default='EasyOffice'),
    'TAGLINE': config('OFFICE_TAGLINE', default='Your Virtual Office Management Platform'),
    'LOGO_URL': config('OFFICE_LOGO_URL', default=''),
    'PRIMARY_COLOR': config('OFFICE_PRIMARY_COLOR', default='#1e3a5f'),
    'ACCENT_COLOR': config('OFFICE_ACCENT_COLOR', default='#2196f3'),
    'FISCAL_YEAR_START_MONTH': config('FISCAL_YEAR_START_MONTH', default=1, cast=int),
    'APPRAISAL_CYCLE_MONTHS': config('APPRAISAL_CYCLE_MONTHS', default=12, cast=int),
    'WORKING_HOURS_START': config('WORKING_HOURS_START', default='08:00'),
    'WORKING_HOURS_END': config('WORKING_HOURS_END', default='17:00'),
    'CURRENCY': config('CURRENCY', default='USD'),
    'CURRENCY_SYMBOL': config('CURRENCY_SYMBOL', default='$'),
    'DATE_FORMAT': config('DATE_FORMAT', default='%Y-%m-%d'),
    'MAX_TASK_COLLABORATORS': config('MAX_TASK_COLLABORATORS', default=10, cast=int),
    'ENABLE_CHAT': config('ENABLE_CHAT', default=True, cast=bool),
    'ENABLE_VIDEO_CALLS': config('ENABLE_VIDEO_CALLS', default=False, cast=bool),
    'ENABLE_PAYROLL': config('ENABLE_PAYROLL', default=True, cast=bool),
    'FILE_STORAGE_QUOTA_GB': config('FILE_STORAGE_QUOTA_GB', default=10, cast=int),
}

# =============================================================
# INTERNATIONALIZATION
# =============================================================
LANGUAGE_CODE = config('LANGUAGE_CODE', default='en-us')
# ⏰ NOTE for the chat date dividers ("Today"/"Yesterday"): days are
# grouped using this TIME_ZONE. Keep it set to your office's local zone
# (e.g. 'Africa/Banjul') via the TIME_ZONE env var — with the 'UTC'
# default, a message sent at 00:30 local could group under the wrong day.
TIME_ZONE = config('TIME_ZONE', default='UTC')
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
X_FRAME_OPTIONS = 'SAMEORIGIN'
# =============================================================
# LOGGING
# =============================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps': {
            # 🔒 DEBUG-level app logs can capture request bodies and message
            # metadata. DEBUG while developing, INFO in production (override
            # with the APPS_LOG_LEVEL env var if needed).
            'handlers': ['console'],
            'level': config('APPS_LOG_LEVEL', default='DEBUG' if DEBUG else 'INFO'),
            'propagate': False,
        },
    },
}
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# settings.py — only set the ones whose defaults don't match your models
POI_TRUSTED_DEVICE_MODEL  = "core.TrustedDevice"  # app_label.ModelName
POI_TD_USER_FIELD         = "user"                # FK to User on that model
POI_TD_FINGERPRINT_FIELD  = "device_id"           # the fingerprint column
POI_TD_TRUSTED_UNTIL_FIELD= None                  # expiry column, or None

POI_NOTIFICATION_MODEL    = "core.Notification"
POI_NOTIF_RECIPIENT_FIELD = "user"
POI_NOTIF_READ_FIELD      = "is_read"
POI_NOTIF_CREATED_FIELD   = "created_at"

POS_INVENTORY_MODEL = 'orders.InventoryItem'   # app_label.ModelName
POS_FIELD_NAME      = 'name'         # product display name
POS_FIELD_SKU       = 'sku'          # SKU/product code   ('' if you have none)
POS_FIELD_BARCODE   = 'barcode'      # barcode/QR value   ('' if you have none)
POS_FIELD_PRICE     = 'unit_price'   # selling price
POS_FIELD_STOCK     = 'quantity'     # on-hand stock      ('' = no stock tracking)
POS_ALLOW_OVERSELL  = False          # True lets stock go negative
POS_REQUIRE_CASH_SESSION = True
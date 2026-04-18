"""
EasyOffice Django Settings
"""
import os
from pathlib import Path
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================
# CORE
# =============================================================
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-this-key')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

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
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.ActiveUserMiddleware',
    'apps.core.middleware.OfficeBrandingMiddleware',
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

# =============================================================
# HTTPS / HSTS  (set to True in production via env vars)
# =============================================================
SECURE_SSL_REDIRECT            = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SECURE_HSTS_SECONDS            = config('SECURE_HSTS_SECONDS', default=0, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False, cast=bool)
SECURE_HSTS_PRELOAD            = config('SECURE_HSTS_PRELOAD', default=False, cast=bool)
SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')

# =============================================================
# MESSAGING ENCRYPTION (at-rest, AES-256 via Fernet)
# =============================================================
# Generate a key once and store it as an environment variable:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Never commit the key value to source control.
MESSAGING_ENCRYPTION_KEY = config('MESSAGING_ENCRYPTION_KEY', default='')

# =============================================================
# COLLABORA ONLINE (collaborative Word/Excel/PowerPoint editor)
# =============================================================
# Base URL of the Collabora Online server, reachable by the BROWSER.
# e.g. https://cool.easyoffice.example.org  (no trailing slash)
# Must be HTTPS in production — Collabora refuses to load over plain HTTP.
COLLABORA_SERVER_URL = config('COLLABORA_SERVER_URL', default='')

# URL where Django is reachable FROM the Collabora container.
# - If Collabora reaches Django over the public internet: leave blank and
#   the current request's scheme+host will be used (this works for most
#   single-domain deployments).
# - If Collabora runs on the same Docker network and can reach Django via
#   an internal DNS name, set it here to skip a public round-trip:
#   e.g. 'http://web:8000'  (where 'web' is the Django service name).
COLLABORA_WOPI_HOST = config('COLLABORA_WOPI_HOST', default='')

# Path to the Collabora editor entry point. Default works for the standard
# collabora/code Docker image. Override only if your Collabora deployment
# uses a non-standard entry path.
COLLABORA_EDITOR_PATH = config('COLLABORA_EDITOR_PATH', default='/browser/dist/cool.html')

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
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
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
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}
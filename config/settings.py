"""
Django settings for QualityLab project.

Migrated from Node.js + FastAPI to Django + DRF.
"""
import os
import sys
from pathlib import Path
from datetime import timedelta
from email.utils import formataddr

import environ

# ──────────────────────────────────────────────
# Base directory & environment
# ──────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    PRODUCTION=(bool, False),
)
environ.Env.read_env(BASE_DIR / '.env')


def _normalize_throttle_rate(raw_value: str, default: str) -> str:
    """Return a DRF-compatible throttle rate like '10/hour'."""
    candidate = str(raw_value or '').split('#', 1)[0].strip()
    if not candidate:
        return default

    if '/' not in candidate:
        if candidate.isdigit():
            # Bare integers are treated as per-minute limits for convenience.
            return f'{int(candidate)}/minute'
        return default

    requests_count, period = [part.strip() for part in candidate.split('/', 1)]
    if not requests_count.isdigit() or not period:
        return default

    if period[0].lower() not in {'s', 'm', 'h', 'd'}:
        return default

    return f'{int(requests_count)}/{period.lower()}'

SECRET_KEY = env('SECRET_KEY', default='QualityLabSecretKey123!')
DEBUG = env('DEBUG')
PRODUCTION = env('PRODUCTION')
JOB_CONNECTION_STRING_ENCRYPTION_KEY = env('JOB_CONNECTION_STRING_ENCRYPTION_KEY', default='')
ANALYZE_POSTGRES_RATE_LIMIT = _normalize_throttle_rate(
    env('ANALYZE_POSTGRES_RATE_LIMIT', default='10/hour'),
    default='10/hour',
)
EXECUTE_FIX_RATE_LIMIT = _normalize_throttle_rate(
    env('EXECUTE_FIX_RATE_LIMIT', default='200/hour'),
    default='200/hour',
)
USER_RATE_LIMIT = _normalize_throttle_rate(
    env('USER_RATE_LIMIT', default='1200/hour'),
    default='1200/hour',
)
ANON_RATE_LIMIT = _normalize_throttle_rate(
    env('ANON_RATE_LIMIT', default='120/hour'),
    default='120/hour',
)
ACCESS_TOKEN_LIFETIME_MINUTES = max(5, env.int('ACCESS_TOKEN_LIFETIME_MINUTES', default=60))
REFRESH_TOKEN_LIFETIME_DAYS = max(1, env.int('REFRESH_TOKEN_LIFETIME_DAYS', default=7))
ROTATE_REFRESH_TOKENS = env.bool('ROTATE_REFRESH_TOKENS', default=True)

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])
if DEBUG:
    ALLOWED_HOSTS = sorted(set(ALLOWED_HOSTS + ['localhost', '127.0.0.1']))

# ──────────────────────────────────────────────
# Installed apps
# ──────────────────────────────────────────────

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',

    # Third-party
    'rest_framework',
    'corsheaders',
    'rest_framework_simplejwt',
    'dj_rest_auth',
    'dj_rest_auth.registration',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.github',

    # Bonus auth
    'oauth2_provider',
    'social_django',
    'drf_social_oauth2',
    'drfpasswordless',

    # Project apps
    'users',
    'api',

    # WebSocket support
    'channels',
]

# ──────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'social_django.context_processors.backends',
                'social_django.context_processors.login_redirect',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ──────────────────────────────────────────────
# Databases
# ──────────────────────────────────────────────

_RUNNING_TESTS = any(
    token in str(arg).lower()
    for arg in sys.argv
    for token in ('test', 'unittest', 'pytest')
)
USE_SQLITE_TEST_DB = env.bool('USE_SQLITE_TEST_DB', default=_RUNNING_TESTS)

if USE_SQLITE_TEST_DB:
    # Default test runs to a local SQLite database so `python -m unittest`
    # remains self-contained in development and CI environments where a live
    # PostgreSQL server is not available.
    test_db_dir = BASE_DIR / 'tmp'
    test_db_dir.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(test_db_dir / 'test_db.sqlite3'),
        }
    }
else:
    # PostgreSQL (Django ORM) — all data: users, jobs, results, chat, guidelines
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': env('PG_DB', default='qualitylab'),
            'USER': env('PG_USER', default='quality_user'),
            'PASSWORD': env('PG_PASSWORD', default='strongpassword'),
            'HOST': env('PG_HOST', default='localhost'),
            'PORT': env('PG_PORT', default='5432'),
        }
    }

# ──────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
]

AUTHENTICATION_BACKENDS = [
    # Social OAuth2
    'drf_social_oauth2.backends.DjangoOAuth2',
    # Allauth
    'allauth.account.auth_backends.AuthenticationBackend',
    # Default Django
    'django.contrib.auth.backends.ModelBackend',
]

# ──────────────────────────────────────────────
# Django REST Framework
# ──────────────────────────────────────────────

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'users.backends.CookieJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': ANON_RATE_LIMIT,
        'user': USER_RATE_LIMIT,
        # Scoped in api.views via UserRateThrottle subclasses.
        'analyze_postgres': ANALYZE_POSTGRES_RATE_LIMIT,
        'execute_fix': EXECUTE_FIX_RATE_LIMIT,
    },
    'EXCEPTION_HANDLER': 'rest_framework.views.exception_handler',
}

# ──────────────────────────────────────────────
# Simple JWT
# ──────────────────────────────────────────────

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS),
    'ROTATE_REFRESH_TOKENS': ROTATE_REFRESH_TOKENS,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'userId',
    # We use custom cookie auth, not header-based
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
}

# Cookie settings — match Node.js httpOnly cookie behavior
JWT_COOKIE_NAME = 'token'
JWT_COOKIE_MAX_AGE = ACCESS_TOKEN_LIFETIME_MINUTES * 60
JWT_COOKIE_HTTPONLY = True
JWT_COOKIE_SAMESITE = 'Lax'
JWT_COOKIE_SECURE = PRODUCTION
JWT_COOKIE_PATH = '/'

# Refresh cookie settings — used by /api/token/refresh for silent session renewal
JWT_REFRESH_COOKIE_NAME = env('JWT_REFRESH_COOKIE_NAME', default='refresh_token')
JWT_REFRESH_COOKIE_MAX_AGE = REFRESH_TOKEN_LIFETIME_DAYS * 24 * 60 * 60
JWT_REFRESH_COOKIE_HTTPONLY = True
JWT_REFRESH_COOKIE_SAMESITE = JWT_COOKIE_SAMESITE
JWT_REFRESH_COOKIE_SECURE = JWT_COOKIE_SECURE
JWT_REFRESH_COOKIE_PATH = '/api/token/refresh'

# dj-rest-auth settings — keep cookie-based JWT
REST_AUTH = {
    'USE_JWT': True,
    'JWT_AUTH_COOKIE': JWT_COOKIE_NAME,
    'JWT_AUTH_REFRESH_COOKIE': JWT_REFRESH_COOKIE_NAME,
    'JWT_AUTH_SAMESITE': JWT_COOKIE_SAMESITE,
    'JWT_AUTH_SECURE': JWT_COOKIE_SECURE,
    'JWT_AUTH_HTTPONLY': JWT_COOKIE_HTTPONLY,
    'TOKEN_MODEL': None,
}

REST_AUTH_SERIALIZERS = {
    'USER_DETAILS_SERIALIZER': 'users.dj_rest_auth_serializers.CustomUserDetailsSerializer',
    'REGISTER_SERIALIZER': 'users.dj_rest_auth_serializers.CustomRegisterSerializer',
}

REST_AUTH_REGISTER_SERIALIZERS = {
    'REGISTER_SERIALIZER': 'users.dj_rest_auth_serializers.CustomRegisterSerializer',
}

# ──────────────────────────────────────────────
# CORS — match Node.js CORS settings
# ──────────────────────────────────────────────

CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:4200',
    'http://127.0.0.1:3000',
    'http://127.0.0.1:4200',
]
CORS_ALLOW_CREDENTIALS = True  # Required for httpOnly cookies

# ──────────────────────────────────────────────
# CSRF — exempt API endpoints (we use JWT cookies, not CSRF tokens)
# ──────────────────────────────────────────────

CSRF_TRUSTED_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:4200',
    'http://127.0.0.1:3000',
    'http://127.0.0.1:4200',
]

# ──────────────────────────────────────────────
# Celery
# ──────────────────────────────────────────────

CELERY_BROKER_URL = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'

# ──────────────────────────────────────────────
# Django Channels (WebSocket)
# ──────────────────────────────────────────────

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [env('REDIS_URL', default='redis://localhost:6379/0')],
        },
    },
}

# ──────────────────────────────────────────────
# File uploads
# ──────────────────────────────────────────────

UPLOAD_DIR = BASE_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)

REPORTS_DIR = BASE_DIR / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)

# Max upload size: 500 MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 500 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 500 * 1024 * 1024

MEDIA_ROOT = UPLOAD_DIR
MEDIA_URL = '/uploads/'

# ──────────────────────────────────────────────
# Passwordless auth (bonus)
# ──────────────────────────────────────────────

PASSWORDLESS_AUTH = {
    'PASSWORDLESS_AUTH_TYPES': ['EMAIL'],
    'PASSWORDLESS_EMAIL_NOREPLY_ADDRESS': 'noreply@qualitylab.dev',
    'PASSWORDLESS_TOKEN_EXPIRE_TIME': 15 * 60,  # 15 minutes
}

# ──────────────────────────────────────────────
# Social auth
# ──────────────────────────────────────────────

SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = env('GOOGLE_OAUTH2_KEY', default='')
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = env('GOOGLE_OAUTH2_SECRET', default='')
SOCIAL_AUTH_GITHUB_KEY = env('GITHUB_KEY', default='')
SOCIAL_AUTH_GITHUB_SECRET = env('GITHUB_SECRET', default='')

# Frontend URL (used for GitHub OAuth callback redirect)
FRONTEND_URL = env('FRONTEND_URL', default='http://localhost:3000')
GITHUB_CALLBACK_URL = env('GITHUB_CALLBACK_URL', default='http://localhost:8000/api/github/callback')
FRONTEND_VERIFY_EMAIL_URL = env('FRONTEND_VERIFY_EMAIL_URL', default=f'{FRONTEND_URL}/#/verify-email')

EMAIL_VERIFICATION_CODE_TTL_MINUTES = env.int('EMAIL_VERIFICATION_CODE_TTL_MINUTES', default=15)
EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = env.int('EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS', default=60)
EMAIL_VERIFICATION_MAX_ATTEMPTS = env.int('EMAIL_VERIFICATION_MAX_ATTEMPTS', default=5)

# django-allauth
SITE_ID = 1
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = True
ACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'APP': {
            'client_id': SOCIAL_AUTH_GOOGLE_OAUTH2_KEY,
            'secret': SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET,
            'key': '',
        },
    },
    'github': {
        'SCOPE': ['user:email'],
        'APP': {
            'client_id': SOCIAL_AUTH_GITHUB_KEY,
            'secret': SOCIAL_AUTH_GITHUB_SECRET,
            'key': '',
        },
    },
}
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_ADAPTER = 'users.allauth_adapter.SocialAccountAdapter'

# ──────────────────────────────────────────────
# Internationalization & Static
# ──────────────────────────────────────────────

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Brevo SMTP email transport
BREVO_SMTP_HOST = env('BREVO_SMTP_HOST', default='smtp-relay.brevo.com')
BREVO_SMTP_PORT = env.int('BREVO_SMTP_PORT', default=587)
BREVO_SMTP_LOGIN = env('BREVO_SMTP_LOGIN', default='')
BREVO_SMTP_KEY = env('BREVO_SMTP_KEY', default='')

EMAIL_FROM_NAME = env('EMAIL_FROM_NAME', default='QualityLab')
EMAIL_FROM_ADDRESS = env('EMAIL_FROM_ADDRESS', default='noreply@qualitylab.com')

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = BREVO_SMTP_HOST
EMAIL_PORT = BREVO_SMTP_PORT
EMAIL_HOST_USER = BREVO_SMTP_LOGIN
EMAIL_HOST_PASSWORD = BREVO_SMTP_KEY
EMAIL_USE_SSL = env.bool('BREVO_SMTP_USE_SSL', default=BREVO_SMTP_PORT == 465)
EMAIL_USE_TLS = env.bool('BREVO_SMTP_USE_TLS', default=not EMAIL_USE_SSL)
if EMAIL_USE_SSL:
    EMAIL_USE_TLS = False

DEFAULT_FROM_EMAIL = formataddr((EMAIL_FROM_NAME, EMAIL_FROM_ADDRESS))
SERVER_EMAIL = DEFAULT_FROM_EMAIL
ADMIN_EMAIL = env('ADMIN_EMAIL', default='')

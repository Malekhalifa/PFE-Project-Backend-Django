"""
Django settings for QualityLab project.

Migrated from Node.js + FastAPI to Django + DRF.
"""
import os
from pathlib import Path
from datetime import timedelta

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

SECRET_KEY = env('SECRET_KEY', default='change-me-to-a-strong-random-secret')
DEBUG = env('DEBUG')
PRODUCTION = env('PRODUCTION')

ALLOWED_HOSTS = ['*']

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

# PostgreSQL (Django ORM) — users, audit logs
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

# MongoDB (pymongo) — jobs, analysis results, cleaning results
MONGO_URI = env('MONGO_URI', default='mongodb://localhost:27017/qualitylab')

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
    'EXCEPTION_HANDLER': 'rest_framework.views.exception_handler',
}

# ──────────────────────────────────────────────
# Simple JWT
# ──────────────────────────────────────────────

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=14),
    'ROTATE_REFRESH_TOKENS': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'userId',
    # We use custom cookie auth, not header-based
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
}

# Cookie settings — match Node.js httpOnly cookie behavior
JWT_COOKIE_NAME = 'token'
JWT_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days
JWT_COOKIE_HTTPONLY = True
JWT_COOKIE_SAMESITE = 'Lax'
JWT_COOKIE_SECURE = PRODUCTION
JWT_COOKIE_PATH = '/'

# dj-rest-auth settings — keep cookie-based JWT
REST_AUTH = {
    'USE_JWT': True,
    'JWT_AUTH_COOKIE': JWT_COOKIE_NAME,
    'JWT_AUTH_REFRESH_COOKIE': None,
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
# File uploads
# ──────────────────────────────────────────────

UPLOAD_DIR = BASE_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)

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

# Email (password reset) — console backend until SMTP is configured
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

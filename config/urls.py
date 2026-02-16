"""
QualityLab URL Configuration.

All API endpoints are mounted under /api/ to match the existing Node.js backend.
Bonus auth endpoints live under /auth/.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.db import connection


def health_check(request):
    """Health check endpoint matching Node.js GET /health."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


urlpatterns = [
    # Django admin
    path('admin/', admin.site.urls),

    # Health check
    path('health', health_check),

    # Core API — auth endpoints
    path('api/', include('users.urls')),

    # Core API — jobs, uploads, analysis, cleaning
    path('api/', include('api.urls')),

    # Bonus: Social OAuth2
    path('auth/social/', include('drf_social_oauth2.urls', namespace='drf')),

    # Bonus: Passwordless login
    path('auth/passwordless/', include('drfpasswordless.urls')),

    # Allauth (required for socialaccount signup flow)
    path('accounts/', include('allauth.urls')),
]

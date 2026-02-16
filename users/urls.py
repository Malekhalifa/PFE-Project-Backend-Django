"""
Auth URL routes — mounted at /api/ in config/urls.py.
"""
from django.urls import path
from dj_rest_auth.views import PasswordResetView, PasswordResetConfirmView, PasswordChangeView
from . import dj_rest_auth_views as views

urlpatterns = [
    path('register', views.CustomRegisterView.as_view(), name='register'),
    path('login', views.CustomLoginView.as_view(), name='login'),
    path('logout', views.CustomLogoutView.as_view(), name='logout'),
    path('me', views.CustomUserDetailsView.as_view(), name='me'),

    # Social login
    path('social-login', views.SocialLoginProxyView.as_view(), name='social-login'),
    path('github/login', views.GitHubLoginView.as_view(), name='github-login'),
    path('github/callback', views.GitHubCallbackView.as_view(), name='github-callback'),

    # Password reset/change
    path('password/reset', PasswordResetView.as_view(), name='password_reset'),
    path('password/reset/confirm', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password/change', PasswordChangeView.as_view(), name='password_change'),
]

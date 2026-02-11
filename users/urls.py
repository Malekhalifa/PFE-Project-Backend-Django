"""
Auth URL routes — mounted at /api/ in config/urls.py.
"""
from django.urls import path
from . import views

urlpatterns = [
    path('register', views.RegisterView.as_view(), name='register'),
    path('login', views.LoginView.as_view(), name='login'),
    path('logout', views.LogoutView.as_view(), name='logout'),
    path('me', views.MeView.as_view(), name='me'),

    # Social login
    path('social-login', views.SocialLoginView.as_view(), name='social-login'),
    path('github/login', views.GitHubLoginView.as_view(), name='github-login'),
    path('github/callback', views.GitHubCallbackView.as_view(), name='github-callback'),
]

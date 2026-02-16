"""
Auth views using dj-rest-auth while preserving existing API shapes.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.middleware import MessageMiddleware
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import RegisterView, SocialLoginView
from dj_rest_auth.views import LoginView, LogoutView, UserDetailsView

from .serializers import UserSerializer
from .dj_rest_auth_serializers import TokenSocialLoginSerializer


class CustomRegisterView(RegisterView):
    """POST /api/register — create account, set JWT cookie."""
    permission_classes = [AllowAny]

    def get_response_data(self, user):
        return {'user': UserSerializer(user).data}


class CustomLoginView(LoginView):
    """POST /api/login — verify credentials, set JWT cookie."""
    permission_classes = [AllowAny]

    def get_response(self):
        response = super().get_response()
        response.data = {'user': UserSerializer(self.user).data}
        return response


class CustomLogoutView(LogoutView):
    """POST /api/logout — clear JWT cookie."""
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        response.data = {'message': 'Logged out'}
        return response


class CustomUserDetailsView(UserDetailsView):
    """GET/PUT/PATCH /api/me — return or update current user."""
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        response.data = {'user': response.data}
        return response

    def put(self, request, *args, **kwargs):
        response = super().put(request, *args, **kwargs)
        response.data = {'user': response.data}
        return response

    def patch(self, request, *args, **kwargs):
        response = super().patch(request, *args, **kwargs)
        response.data = {'user': response.data}
        return response


class GoogleSocialLoginView(SocialLoginView):
    """POST /api/social-login — Google social login."""
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    serializer_class = TokenSocialLoginSerializer
    permission_classes = [AllowAny]


class GitHubSocialLoginView(SocialLoginView):
    """Internal social login view for GitHub OAuth."""
    adapter_class = GitHubOAuth2Adapter
    client_class = OAuth2Client
    permission_classes = [AllowAny]


class SocialLoginProxyView(APIView):
    """
    Legacy endpoint:
    POST /api/social-login — expects { provider, token } for Google.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        provider = request.data.get('provider')
        if provider != 'google':
            return Response(
                {'error': 'Unsupported provider. Use google.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = dict(request.data)
        factory = APIRequestFactory()
        post_request = factory.post('/api/social-login', payload)
        SessionMiddleware(lambda req: None).process_request(post_request)
        post_request.session.save()
        MessageMiddleware(lambda req: None).process_request(post_request)
        return GoogleSocialLoginView.as_view()(post_request)


class GitHubLoginView(APIView):
    """
    GET /api/github/login — redirect user to GitHub OAuth authorize page.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        github_url = (
            'https://github.com/login/oauth/authorize'
            f'?client_id={settings.SOCIAL_AUTH_GITHUB_KEY}'
            f'&redirect_uri={settings.GITHUB_CALLBACK_URL}'
            '&scope=user:email'
        )
        return redirect(github_url)


class GitHubCallbackView(APIView):
    """
    GET /api/github/callback?code=... — GitHub OAuth callback.

    Exchanges code via dj-rest-auth social login to set JWT cookie,
    then redirects to the frontend.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get('code')
        error = request.query_params.get('error')
        frontend = settings.FRONTEND_URL

        if error or not code:
            return redirect(f'{frontend}/#/login?error=github_denied')

        factory = APIRequestFactory()
        post_request = factory.post(
            '/api/github/callback',
            {'code': code, 'redirect_uri': settings.GITHUB_CALLBACK_URL},
        )
        SessionMiddleware(lambda req: None).process_request(post_request)
        post_request.session.save()
        MessageMiddleware(lambda req: None).process_request(post_request)
        login_response = GitHubSocialLoginView.as_view()(post_request)

        if login_response.status_code >= 400:
            detail = None
            if hasattr(login_response, 'data'):
                detail = login_response.data
            elif hasattr(login_response, 'content'):
                try:
                    detail = login_response.content.decode('utf-8')
                except Exception:
                    detail = 'github_failed'
            return Response(
                {'error': 'github_failed', 'detail': detail},
                status=login_response.status_code,
            )

        response = redirect(f'{frontend}/#/')
        for key, morsel in login_response.cookies.items():
            response.set_cookie(
                key=key,
                value=morsel.value,
                max_age=morsel['max-age'] or None,
                expires=morsel['expires'] or None,
                path=morsel['path'] or '/',
                domain=morsel['domain'] or None,
                secure=bool(morsel['secure']),
                httponly=bool(morsel['httponly']),
                samesite=morsel['samesite'] or None,
            )
        return response

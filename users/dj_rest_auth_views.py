"""
Auth views using dj-rest-auth while preserving existing API shapes.
"""
import logging

from django.conf import settings
from django.db import IntegrityError
from django.shortcuts import redirect
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.middleware import MessageMiddleware
import requests as http_requests
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import RegisterView, SocialLoginView
from dj_rest_auth.views import LoginView, LogoutView, UserDetailsView

from .serializers import UserSerializer
from .dj_rest_auth_serializers import CustomRegisterSerializer, TokenSocialLoginSerializer, CustomUserDetailsSerializer
from .models import User


logger = logging.getLogger(__name__)


def _set_jwt_cookie(response, user):
    """Create a JWT access token and set it as an httpOnly cookie."""
    token = AccessToken.for_user(user)
    token['role'] = user.role

    response.set_cookie(
        key=settings.JWT_COOKIE_NAME,
        value=str(token),
        httponly=settings.JWT_COOKIE_HTTPONLY,
        secure=settings.JWT_COOKIE_SECURE,
        samesite=settings.JWT_COOKIE_SAMESITE,
        max_age=settings.JWT_COOKIE_MAX_AGE,
        path=settings.JWT_COOKIE_PATH,
    )
    return response


def _get_or_create_social_user(email):
    """Find existing user by email or create a new one for social login."""
    normalized_email = (email or '').strip().lower()
    try:
        user, _ = User.objects.get_or_create(
            email=normalized_email,
            defaults={'username': normalized_email, 'role': 'user'},
        )
        return user
    except IntegrityError:
        # Race-safe fallback when two social callbacks attempt the same email.
        return User.objects.get(email__iexact=normalized_email)


def _exchange_github_code(code):
    """Exchange a GitHub OAuth code for an access token and return verified email."""
    try:
        token_resp = http_requests.post(
            'https://github.com/login/oauth/access_token',
            json={
                'client_id': settings.SOCIAL_AUTH_GITHUB_KEY,
                'client_secret': settings.SOCIAL_AUTH_GITHUB_SECRET,
                'code': code,
            },
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        if token_resp.status_code != 200:
            return None

        access_token = token_resp.json().get('access_token')
        if not access_token:
            return None

        emails_resp = http_requests.get(
            'https://api.github.com/user/emails',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=10,
        )
        if emails_resp.status_code != 200:
            return None

        emails = emails_resp.json()
        for entry in emails:
            if entry.get('primary') and entry.get('verified'):
                return entry['email']
        for entry in emails:
            if entry.get('verified'):
                return entry['email']

        return None
    except http_requests.RequestException:
        return None


class CustomRegisterView(RegisterView):
    """POST /api/register — create account, set JWT cookie.
    GET redirects to frontend login so opening the API URL in a browser is friendly."""
    serializer_class = CustomRegisterSerializer
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required; avoid 401 when no cookie is sent

    def get_permissions(self):
        return [AllowAny()]

    def get(self, request, *args, **kwargs):
        """Redirect GET to frontend login; registration is POST-only."""
        return redirect(f'{settings.FRONTEND_URL}/#/login')

    def get_response_data(self, user):
        return {'user': UserSerializer(user).data}

    @staticmethod
    def _build_integrity_error_response(exc):
        text = str(exc)
        if 'users_email_key' in text or 'Key (email)=' in text:
            message = 'This email is already registered. Welcome back! Please sign in instead.'
            return Response(
                {
                    'email': [message],
                    'error': message,
                    'code': 'email_already_registered',
                },
                status=status.HTTP_409_CONFLICT,
            )

        if 'users_username_key' in text or 'Key (username)=' in text:
            message = 'That username is already taken. Please choose another one.'
            return Response(
                {
                    'username': [message],
                    'error': message,
                    'code': 'username_already_registered',
                },
                status=status.HTTP_409_CONFLICT,
            )

        logger.warning('Unhandled registration integrity error: %s', text)
        return Response(
            {
                'error': 'We could not create your account due to a data conflict. Please try again.',
                'code': 'registration_conflict',
            },
            status=status.HTTP_409_CONFLICT,
        )

    def create(self, request, *args, **kwargs):
        """
        Mirror dj-rest-auth register flow but ensure successful signup sets
        the same JWT cookie used by login/social flows.
        """
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            user = self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            data = self.get_response_data(user)

            if data:
                response = Response(
                    data,
                    status=status.HTTP_201_CREATED,
                    headers=headers,
                )
                return _set_jwt_cookie(response, user)

            return Response(status=status.HTTP_204_NO_CONTENT, headers=headers)
        except ValidationError:
            raise
        except IntegrityError as exc:
            return self._build_integrity_error_response(exc)
        except Exception:
            logger.exception('Unexpected server error while registering user')
            return Response(
                {
                    'error': 'Unexpected server error while creating your account. Please try again shortly.',
                    'code': 'registration_server_error',
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomLoginView(LoginView):
    """POST /api/login — verify credentials, set JWT cookie."""
    permission_classes = [AllowAny]
    authentication_classes = []  # Allow unauthenticated requests (no cookie yet)

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
    serializer_class = CustomUserDetailsSerializer  # explicitly override so REST_AUTH_SERIALIZERS is not needed

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

        email = _exchange_github_code(code)
        if not email:
            return redirect(f'{frontend}/#/login?error=github_failed')

        user = _get_or_create_social_user(email)
        response = redirect(f'{frontend}/#/')
        return _set_jwt_cookie(response, user)

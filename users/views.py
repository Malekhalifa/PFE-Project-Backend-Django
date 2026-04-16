"""
Auth views — matches the 4 Node.js auth endpoints exactly,
plus social login (Google, GitHub).

POST /api/register        -> create user, set JWT cookie, return user data
POST /api/login           -> verify credentials, set JWT cookie, return user data
POST /api/logout          -> clear JWT cookie
GET  /api/me              -> return current authenticated user
POST /api/social-login    -> Google ID-token verification, set JWT cookie
GET  /api/github/login    -> redirect to GitHub OAuth
GET  /api/github/callback -> exchange code, set JWT cookie, redirect to frontend
"""
import logging

import requests as http_requests

from django.conf import settings
from django.contrib.auth import authenticate
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from .models import User
from .serializers import RegisterSerializer, LoginSerializer, UserSerializer


logger = logging.getLogger(__name__)


def _set_jwt_cookie(response, user):
    """Create a JWT access token and set it as an httpOnly cookie."""
    token = AccessToken.for_user(user)
    # Add role claim to match Node.js JWT payload { userId, role }
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


class RegisterView(APIView):
    """POST /api/register — create account, set JWT cookie."""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if not serializer.is_valid():
            # Match Node.js error format
            errors = serializer.errors
            if 'email' in errors:
                msg = errors['email'][0]
                code = status.HTTP_409_CONFLICT if 'already' in str(msg) else status.HTTP_400_BAD_REQUEST
                return Response({'error': str(msg)}, status=code)
            if 'password' in errors:
                return Response({'error': str(errors['password'][0])}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Invalid input'}, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.save()
        user_data = UserSerializer(user).data
        response = Response({'user': user_data}, status=status.HTTP_201_CREATED)
        return _set_jwt_cookie(response, user)


class LoginView(APIView):
    """POST /api/login — verify credentials, set JWT cookie."""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            # Return the first meaningful error, matching Node.js format
            if 'email' in errors:
                return Response({'error': str(errors['email'][0])}, status=status.HTTP_400_BAD_REQUEST)
            if 'password' in errors:
                return Response({'error': str(errors['password'][0])}, status=status.HTTP_400_BAD_REQUEST)
            return Response(
                {'error': 'Email and password are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        user = authenticate(request, username=email, password=password)
        if user is None:
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user_data = UserSerializer(user).data
        response = Response({'user': user_data})
        return _set_jwt_cookie(response, user)


class LogoutView(APIView):
    """POST /api/logout — clear JWT cookie."""
    permission_classes = [AllowAny]

    def post(self, request):
        response = Response({'message': 'Logged out'})
        response.delete_cookie(
            key=settings.JWT_COOKIE_NAME,
            path=settings.JWT_COOKIE_PATH,
        )
        return response


class MeView(APIView):
    """GET /api/me — return current authenticated user."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_data = UserSerializer(request.user).data
        return Response({'user': user_data})


# ──────────────────────────────────────────────
# Social Login helpers
# ──────────────────────────────────────────────

def _get_or_create_social_user(email):
    """Find existing user by email or create a new one for social login."""
    user, _ = User.objects.get_or_create(
        email=email,
        defaults={'username': email, 'role': 'user'},
    )
    return user


def _verify_google_token(id_token):
    """Verify a Google ID token and return the user's email, or None."""
    try:
        resp = http_requests.get(
            'https://oauth2.googleapis.com/tokeninfo',
            params={'id_token': id_token},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        email = data.get('email')
        verified = str(data.get('email_verified', '')).lower() == 'true'

        if not email or not verified:
            return None
        return email
    except http_requests.RequestException:
        return None


def _exchange_github_code(code):
    """Exchange a GitHub OAuth code for an access token and return the user's email, or None."""
    try:
        logger.debug(
            'Starting GitHub OAuth code exchange (code_present=%s, key_set=%s, secret_set=%s)',
            bool(code),
            bool(settings.SOCIAL_AUTH_GITHUB_KEY),
            bool(settings.SOCIAL_AUTH_GITHUB_SECRET),
        )
        # Exchange code for access token
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
            logger.warning('GitHub token exchange failed with status=%s', token_resp.status_code)
            return None

        token_payload = token_resp.json()
        access_token = token_payload.get('access_token')
        if not access_token:
            logger.warning('GitHub token exchange did not return access_token')
            return None

        # Fetch user emails from GitHub
        emails_resp = http_requests.get(
            'https://api.github.com/user/emails',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=10,
        )
        if emails_resp.status_code != 200:
            logger.warning('GitHub email lookup failed with status=%s', emails_resp.status_code)
            return None

        # Find primary verified email
        emails = emails_resp.json()
        for entry in emails:
            if entry.get('primary') and entry.get('verified'):
                return entry['email']
        # Fallback: first verified email
        for entry in emails:
            if entry.get('verified'):
                return entry['email']
        logger.info('GitHub user has no verified email addresses')
        return None
    except Exception as exc:
        logger.exception('GitHub OAuth code exchange failed: %s', exc)
        return None


# ──────────────────────────────────────────────
# Social Login views
# ──────────────────────────────────────────────

class SocialLoginView(APIView):
    """
    POST /api/social-login — authenticate via Google ID token.

    Body: { "provider": "google", "token": "<google-id-token>" }
    Returns: { user } + sets JWT cookie.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        provider = request.data.get('provider')
        token = request.data.get('token')

        if not provider or not token:
            return Response(
                {'error': 'Provider and token are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if provider == 'google':
            email = _verify_google_token(token)
        else:
            return Response(
                {'error': 'Unsupported provider. Use google.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not email:
            return Response(
                {'error': 'Social authentication failed'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = _get_or_create_social_user(email)
        user_data = UserSerializer(user).data
        response = Response({'user': user_data})
        return _set_jwt_cookie(response, user)


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

    Exchanges code for access token, fetches user email,
    creates/gets user, sets JWT cookie, and redirects to the frontend.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get('code')
        error = request.query_params.get('error')
        frontend = settings.FRONTEND_URL

        logger.debug(
            'GitHub callback received (has_code=%s, has_error=%s)',
            bool(code),
            bool(error),
        )

        if error or not code:
            return redirect(f'{frontend}/#/login?error=github_denied')

        email = _exchange_github_code(code)
        if not email:
            return redirect(f'{frontend}/#/login?error=github_failed')

        user = _get_or_create_social_user(email)
        response = redirect(f'{frontend}/#/')
        response = _set_jwt_cookie(response, user)
        return response

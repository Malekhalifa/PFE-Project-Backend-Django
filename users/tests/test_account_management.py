import os

import django
from django.conf import settings
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import AccessToken

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from users.backends import CookieJWTAuthentication
from users.dj_rest_auth_serializers import CustomUserDetailsSerializer
from users.dj_rest_auth_views import CustomUserDetailsView, LogoutAllDevicesView, _set_jwt_cookie
from users.models import User


class AccountManagementTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = self._build_user('john_doe', 'john@example.com')
        self.other_user = self._build_user('jane_doe', 'jane@example.com')

    def _build_user(self, username: str, email: str) -> User:
        user = User(username=username, email=email)
        user.set_password('StrongPass123!')
        user.save()
        return user

    def test_custom_user_details_serializer_rejects_duplicate_username(self):
        serializer = CustomUserDetailsSerializer(
            instance=self.user,
            data={'username': self.other_user.username},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('username', serializer.errors)

    def test_custom_user_details_serializer_rejects_invalid_accent_color(self):
        serializer = CustomUserDetailsSerializer(
            instance=self.user,
            data={'accent_color': 'blue'},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('accent_color', serializer.errors)

    def test_delete_me_requires_matching_username(self):
        view = CustomUserDetailsView.as_view()

        mismatch_request = self.factory.delete('/api/me', {'username': 'wrong_name'}, format='json')
        force_authenticate(mismatch_request, user=self.user)
        mismatch_response = view(mismatch_request)

        self.assertEqual(mismatch_response.status_code, 400)
        self.assertEqual(mismatch_response.data.get('code'), 'username_mismatch')

    def test_delete_me_deletes_account_when_username_matches(self):
        view = CustomUserDetailsView.as_view()

        request = self.factory.delete('/api/me', {'username': self.user.username}, format='json')
        force_authenticate(request, user=self.user)
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('code'), 'account_deleted')
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_logout_all_devices_increments_session_version_and_clears_cookie(self):
        self.user.session_version = 5
        self.user.save(update_fields=['session_version'])

        view = LogoutAllDevicesView.as_view()
        request = self.factory.post('/api/logout-all', {}, format='json')
        force_authenticate(request, user=self.user)
        response = view(request)

        self.user.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('code'), 'sessions_revoked')
        self.assertEqual(self.user.session_version, 6)
        self.assertIn(settings.JWT_COOKIE_NAME, response.cookies)
        self.assertEqual(response.cookies[settings.JWT_COOKIE_NAME]['max-age'], 0)

    def test_set_jwt_cookie_includes_session_version_claim(self):
        self.user.session_version = 3
        self.user.save(update_fields=['session_version'])

        response = Response({'ok': True})
        response = _set_jwt_cookie(response, self.user)

        token_value = response.cookies[settings.JWT_COOKIE_NAME].value
        decoded = AccessToken(token_value)

        self.assertEqual(decoded.get('sv'), 3)

    def test_cookie_authentication_rejects_stale_session_token(self):
        self.user.session_version = 4
        self.user.save(update_fields=['session_version'])

        stale_token = AccessToken.for_user(self.user)
        stale_token['role'] = self.user.role
        stale_token['sv'] = 3

        request = self.factory.get('/api/me')
        request.COOKIES[settings.JWT_COOKIE_NAME] = str(stale_token)

        auth = CookieJWTAuthentication()
        auth_result = auth.authenticate(request)

        self.assertIsNone(auth_result)

    def test_cookie_authentication_accepts_matching_session_token(self):
        self.user.session_version = 2
        self.user.save(update_fields=['session_version'])

        valid_token = AccessToken.for_user(self.user)
        valid_token['role'] = self.user.role
        valid_token['sv'] = 2

        request = self.factory.get('/api/me')
        request.COOKIES[settings.JWT_COOKIE_NAME] = str(valid_token)

        auth = CookieJWTAuthentication()
        auth_result = auth.authenticate(request)

        self.assertIsNotNone(auth_result)
        auth_user, _ = auth_result
        self.assertEqual(auth_user.pk, self.user.pk)

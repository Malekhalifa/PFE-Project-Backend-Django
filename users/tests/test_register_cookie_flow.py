import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
from django.db import IntegrityError
from rest_framework import status

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from users import dj_rest_auth_views
from users.dj_rest_auth_views import CustomRegisterView


class CustomRegisterViewCookieTests(unittest.TestCase):
    @patch("users.dj_rest_auth_views._set_jwt_cookie")
    def test_create_sets_cookie_when_signup_succeeds(self, mock_set_jwt_cookie):
        view = CustomRegisterView()

        request = SimpleNamespace(data={"email": "new.user@example.com"})

        serializer = MagicMock()
        serializer.data = {"email": "new.user@example.com"}
        serializer.is_valid.return_value = True

        user = SimpleNamespace(id="3f8b3e7f-a0eb-4f82-8d7f-8d08f56c4f55", role="user")

        view.get_serializer = MagicMock(return_value=serializer)
        view.perform_create = MagicMock(return_value=user)
        view.get_success_headers = MagicMock(return_value={"X-Test": "1"})
        view.get_response_data = MagicMock(return_value={"user": {"id": str(user.id), "email": "new.user@example.com", "role": "user"}})

        mock_set_jwt_cookie.side_effect = lambda response, _: response

        response = view.create(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["user"]["email"], "new.user@example.com")

        view.get_serializer.assert_called_once_with(data=request.data)
        serializer.is_valid.assert_called_once_with(raise_exception=True)
        view.perform_create.assert_called_once_with(serializer)
        mock_set_jwt_cookie.assert_called_once()

        called_response, called_user = mock_set_jwt_cookie.call_args[0]
        self.assertEqual(called_response.status_code, status.HTTP_201_CREATED)
        self.assertIs(called_user, user)

    @patch("users.dj_rest_auth_views._set_jwt_cookie")
    def test_create_skips_cookie_when_register_returns_no_data(self, mock_set_jwt_cookie):
        view = CustomRegisterView()

        request = SimpleNamespace(data={"email": "new.user@example.com"})

        serializer = MagicMock()
        serializer.data = {"email": "new.user@example.com"}
        serializer.is_valid.return_value = True

        user = SimpleNamespace(id="3f8b3e7f-a0eb-4f82-8d7f-8d08f56c4f55", role="user")

        view.get_serializer = MagicMock(return_value=serializer)
        view.perform_create = MagicMock(return_value=user)
        view.get_success_headers = MagicMock(return_value={})
        view.get_response_data = MagicMock(return_value=None)

        response = view.create(request)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_set_jwt_cookie.assert_not_called()

    @patch("users.dj_rest_auth_views._set_jwt_cookie")
    def test_create_returns_409_for_duplicate_email_integrity_error(self, mock_set_jwt_cookie):
        view = CustomRegisterView()

        request = SimpleNamespace(data={"email": "existing@example.com"})

        serializer = MagicMock()
        serializer.data = {"email": "existing@example.com"}
        serializer.is_valid.return_value = True

        view.get_serializer = MagicMock(return_value=serializer)
        view.perform_create = MagicMock(
            side_effect=IntegrityError(
                'duplicate key value violates unique constraint "users_email_key" DETAIL: Key (email)=(existing@example.com) already exists.'
            )
        )

        response = view.create(request)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data.get("code"), "email_already_registered")
        self.assertIn("email", response.data)
        mock_set_jwt_cookie.assert_not_called()

    @patch("users.dj_rest_auth_views._set_jwt_cookie")
    @patch("users.dj_rest_auth_views.logger.exception")
    def test_create_returns_500_json_for_unexpected_error(self, mock_logger_exception, mock_set_jwt_cookie):
        view = CustomRegisterView()

        request = SimpleNamespace(data={"email": "new.user@example.com"})

        serializer = MagicMock()
        serializer.data = {"email": "new.user@example.com"}
        serializer.is_valid.return_value = True

        view.get_serializer = MagicMock(return_value=serializer)
        view.perform_create = MagicMock(side_effect=RuntimeError("boom"))

        response = view.create(request)

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.data.get("code"), "registration_server_error")
        self.assertIn("error", response.data)
        mock_logger_exception.assert_called_once()
        mock_set_jwt_cookie.assert_not_called()


class SocialUserRaceSafetyTests(unittest.TestCase):
    @patch("users.dj_rest_auth_views.User.objects.get")
    @patch("users.dj_rest_auth_views.User.objects.get_or_create")
    def test_get_or_create_social_user_falls_back_after_integrity_error(self, mock_get_or_create, mock_get):
        expected_user = SimpleNamespace(id="user-1", email="race@example.com", role="user")
        mock_get_or_create.side_effect = IntegrityError('duplicate key value violates unique constraint "users_email_key"')
        mock_get.return_value = expected_user

        user = dj_rest_auth_views._get_or_create_social_user("Race@Example.com")

        self.assertIs(user, expected_user)
        mock_get_or_create.assert_called_once()
        mock_get.assert_called_once_with(email__iexact="race@example.com")


if __name__ == "__main__":
    unittest.main()

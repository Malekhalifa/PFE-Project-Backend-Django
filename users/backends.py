"""
Custom JWT authentication backend that reads the token from an httpOnly cookie.

This matches the current Node.js behavior where the JWT is stored in a cookie
named 'token' and the Angular frontend sends it via withCredentials: true.
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed


class CookieJWTAuthentication(JWTAuthentication):
    """
    Read JWT from the 'token' httpOnly cookie instead of the Authorization header.
    """

    def authenticate(self, request):
        # 1) Keep default SimpleJWT behavior for Authorization: Bearer <token>
        header_auth = super().authenticate(request)
        if header_auth is not None:
            return header_auth

        # 2) Fallback to httpOnly cookie auth used by the frontend
        raw_token = request.COOKIES.get('token')
        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
            return self.get_user(validated_token), validated_token
        except (InvalidToken, AuthenticationFailed):
            # Token is invalid, expired, or references a deleted/recreated user.
            # Return None so DRF treats the request as anonymous (not a hard 401).
            return None

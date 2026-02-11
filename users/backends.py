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
        raw_token = request.COOKIES.get('token')
        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
        except (InvalidToken, AuthenticationFailed):
            return None

        return self.get_user(validated_token), validated_token

"""
WebSocket JWT authentication middleware for Django Channels.

Reads the `token` cookie (same name as JWT_COOKIE_NAME in settings),
decodes it with SimpleJWT, and populates scope["user"] so consumers
can call user.is_authenticated without a Django session.
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware


@database_sync_to_async
def _get_user(user_id):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import AnonymousUser
    User = get_user_model()
    try:
        return User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return AnonymousUser()


def _parse_cookie_header(headers) -> dict:
    for name, value in headers:
        if name == b"cookie":
            result = {}
            for part in value.decode("latin-1").split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    result[k.strip()] = v.strip()
            return result
    return {}


class JwtAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            from django.contrib.auth.models import AnonymousUser
            from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
            from rest_framework_simplejwt.tokens import AccessToken

            cookies = _parse_cookie_header(scope.get("headers", []))
            token_str = cookies.get("token")

            if not token_str:
                qs = parse_qs(scope.get("query_string", b"").decode())
                token_str = (qs.get("token") or [None])[0]

            if token_str:
                try:
                    token = AccessToken(token_str)
                    uid = token.get("userId") or token.get("user_id")
                    scope["user"] = await _get_user(uid)
                except (InvalidToken, TokenError):
                    scope["user"] = AnonymousUser()
            else:
                scope["user"] = AnonymousUser()

        return await super().__call__(scope, receive, send)


def JwtAuthMiddlewareStack(inner):
    return JwtAuthMiddleware(inner)

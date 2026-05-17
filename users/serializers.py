"""
Serializers for user authentication.

Only UserSerializer is live; register/login flows use dj_rest_auth_serializers.
"""
from rest_framework import serializers


class UserSerializer(serializers.Serializer):
    """Read-only serializer matching Node.js { id, email, role } shape."""
    id = serializers.UUIDField()
    email = serializers.EmailField()
    username = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    role = serializers.CharField()
    is_staff = serializers.BooleanField()
    email_verified = serializers.BooleanField()
    accent_color = serializers.CharField()
    bio = serializers.CharField()
    preferred_language = serializers.CharField()
    notifications_enabled = serializers.BooleanField()
    email_notifications_enabled = serializers.BooleanField()
    theme_preference = serializers.CharField()
    last_login = serializers.DateTimeField(allow_null=True)

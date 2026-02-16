"""
dj-rest-auth custom serializers to keep existing API shapes and user fields.
"""
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.registration.serializers import SocialLoginSerializer
from dj_rest_auth.serializers import UserDetailsSerializer
from rest_framework import serializers

from .models import User


class CustomRegisterSerializer(RegisterSerializer):
    """Create users with a generated username and default role."""

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        email = data.get('email')
        if email and not data.get('username'):
            base = email.split('@')[0]
            username = base
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f'{base}{counter}'
                counter += 1
            data['username'] = username
        return data

    def save(self, request):
        user = super().save(request)
        if getattr(user, 'role', None) != 'user':
            user.role = 'user'
            user.save(update_fields=['role'])
        return user


class CustomUserDetailsSerializer(UserDetailsSerializer):
    """Expose id/email/role for frontend user details."""
    role = serializers.CharField(read_only=True)

    class Meta(UserDetailsSerializer.Meta):
        model = User
        fields = ('id', 'email', 'role')


class TokenSocialLoginSerializer(SocialLoginSerializer):
    """
    Accept legacy payloads: { provider: "google", token: "<id_token>" }.
    Map token -> access_token/id_token for dj-rest-auth/allauth.
    """

    def validate(self, attrs):
        token = self.initial_data.get('token')
        if token:
            attrs = {**attrs}
            attrs.setdefault('access_token', token)
            attrs.setdefault('id_token', token)
        return super().validate(attrs)

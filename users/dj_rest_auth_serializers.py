"""
dj-rest-auth custom serializers to keep existing API shapes and user fields.
"""
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.registration.serializers import SocialLoginSerializer
from dj_rest_auth.serializers import UserDetailsSerializer
from rest_framework import serializers

from .models import User


class CustomRegisterSerializer(RegisterSerializer):
    """Create users with optional username, first_name, last_name and default role."""

    username = serializers.CharField(required=False, allow_blank=True, max_length=150)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)

    def validate_username(self, value):
        if not value or not value.strip():
            return value
        if User.objects.filter(username=value.strip()).exists():
            raise serializers.ValidationError('A user with that username already exists.')
        return value.strip()

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        email = data.get('email')
        username = (self.validated_data.get('username') or '').strip()
        if not username and email:
            base = email.split('@')[0]
            username = base
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f'{base}{counter}'
                counter += 1
        if username:
            data['username'] = username
        data['first_name'] = (self.validated_data.get('first_name') or '').strip()
        data['last_name'] = (self.validated_data.get('last_name') or '').strip()
        return data

    def save(self, request):
        user = super().save(request)
        update_fields = []
        if getattr(user, 'role', None) != 'user':
            user.role = 'user'
            update_fields.append('role')
        first_name = (self.validated_data.get('first_name') or '').strip()
        last_name = (self.validated_data.get('last_name') or '').strip()
        if first_name != (user.first_name or ''):
            user.first_name = first_name
            update_fields.append('first_name')
        if last_name != (user.last_name or ''):
            user.last_name = last_name
            update_fields.append('last_name')
        if update_fields:
            user.save(update_fields=update_fields)
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

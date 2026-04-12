"""
Serializers for user authentication.

Matches the Node.js request/response shapes:
  - Register: { email, password } -> { user: { id, email, role } }
  - Login:    { email, password } -> { user: { id, email, role } }
  - Me:       -> { user: { id, email, role } }
"""
from rest_framework import serializers
from .models import User


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    password = serializers.CharField(min_length=8, write_only=True)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already registered')
        return value

    def create(self, validated_data):
        # Generate a username from email (required by AbstractUser)
        email = validated_data['email']
        username = email.split('@')[0]

        # Ensure unique username
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f'{base_username}{counter}'
            counter += 1

        user = User.objects.create_user(
            username=username,
            email=email,
            phone_number=(validated_data.get('phone_number') or '').strip(),
            password=validated_data['password'],
            role='user',  # hard-coded — no admin via register
        )
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.CharField()   # Don't validate format on login — just pass to authenticate()
    password = serializers.CharField()


class UserSerializer(serializers.Serializer):
    """Read-only serializer matching Node.js { id, email, role } shape."""
    id = serializers.UUIDField()
    email = serializers.EmailField()
    phone_number = serializers.CharField(allow_blank=True, required=False)
    role = serializers.CharField()

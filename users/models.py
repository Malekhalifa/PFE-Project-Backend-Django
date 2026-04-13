"""
Custom User model — stored in PostgreSQL via Django ORM.

Mirrors the Node.js/Mongoose User schema:
  - UUID primary key
  - email as the login identifier (USERNAME_FIELD)
  - role field: 'user' or 'admin'
  - password hashed via Django's built-in auth (bcrypt-compatible)
"""
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    LANGUAGE_CHOICES = [
        ('en', 'English'),
        ('fr', 'French'),
        ('ar', 'Arabic'),
    ]

    THEME_CHOICES = [
        ('system', 'System'),
        ('light', 'Light'),
        ('dark', 'Dark'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=10,
        choices=[('user', 'user'), ('admin', 'admin')],
        default='user',
    )
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    accent_color = models.CharField(max_length=7, default='#0ea5e9')
    bio = models.CharField(max_length=160, blank=True, default='')
    preferred_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default='en')
    notifications_enabled = models.BooleanField(default=True)
    theme_preference = models.CharField(max_length=10, choices=THEME_CHOICES, default='system')
    session_version = models.PositiveIntegerField(default=0)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        db_table = 'users'

    def __str__(self):
        return self.email


class EmailVerificationChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='email_verification_challenges',
    )
    code_hash = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'email_verification_challenges'
        indexes = [
            models.Index(fields=['user', 'expires_at']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f'EmailVerificationChallenge<{self.user_id}>'

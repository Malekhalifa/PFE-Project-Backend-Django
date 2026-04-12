from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, EmailVerificationChallenge


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('email', 'role', 'email_verified', 'is_active', 'date_joined')
    list_filter = ('role', 'email_verified', 'is_active')
    search_fields = ('email',)
    ordering = ('-date_joined',)


@admin.register(EmailVerificationChallenge)
class EmailVerificationChallengeAdmin(admin.ModelAdmin):
    list_display = ('user', 'expires_at', 'consumed_at', 'attempt_count', 'created_at')
    list_filter = ('consumed_at', 'expires_at')
    search_fields = ('user__email',)
    ordering = ('-created_at',)

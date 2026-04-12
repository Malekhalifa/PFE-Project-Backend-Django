"""
Allauth adapter to allow social login for existing users by email.
"""
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from rest_framework import status
from rest_framework.response import Response

from .models import User


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    If a social login comes in with an email that already exists,
    connect it to that user instead of raising a signup error.
    """

    def pre_social_login(self, request, sociallogin):
        if sociallogin.is_existing:
            return

        email = None
        if sociallogin.user and sociallogin.user.email:
            email = sociallogin.user.email

        if not email:
            return

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return

        sociallogin.connect(request, user)
        raise ImmediateHttpResponse(
            Response({'user': {'id': str(user.id), 'email': user.email, 'role': user.role}})
        )

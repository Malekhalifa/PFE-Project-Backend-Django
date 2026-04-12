import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import EmailVerificationChallenge, User


def _hash_secret(raw_value: str) -> str:
    payload = f'{settings.SECRET_KEY}:{raw_value}'
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _active_challenge_for_user(user: User) -> EmailVerificationChallenge | None:
    now = timezone.now()
    return (
        EmailVerificationChallenge.objects
        .filter(user=user, consumed_at__isnull=True, expires_at__gt=now)
        .order_by('-created_at')
        .first()
    )


def issue_email_verification_challenge(
    user: User,
) -> tuple[EmailVerificationChallenge | None, str | None, str | None, int]:
    if user.email_verified:
        return None, None, None, 0

    now = timezone.now()
    cooldown_seconds = max(0, int(getattr(settings, 'EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS', 60)))

    latest = (
        EmailVerificationChallenge.objects
        .filter(user=user)
        .order_by('-created_at')
        .first()
    )
    if latest and cooldown_seconds > 0:
        elapsed = int((now - latest.created_at).total_seconds())
        if elapsed < cooldown_seconds:
            return None, None, None, cooldown_seconds - elapsed

    # Keep only one active challenge at a time.
    EmailVerificationChallenge.objects.filter(
        user=user,
        consumed_at__isnull=True,
    ).update(consumed_at=now)

    raw_code = f'{secrets.randbelow(1_000_000):06d}'
    raw_token = secrets.token_urlsafe(32)
    ttl_minutes = max(1, int(getattr(settings, 'EMAIL_VERIFICATION_CODE_TTL_MINUTES', 15)))

    challenge = EmailVerificationChallenge.objects.create(
        user=user,
        code_hash=_hash_secret(raw_code),
        token_hash=_hash_secret(raw_token),
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    return challenge, raw_code, raw_token, 0


def mark_user_email_verified(user: User) -> None:
    now = timezone.now()

    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = now
        user.save(update_fields=['email_verified', 'email_verified_at'])

    EmailVerificationChallenge.objects.filter(
        user=user,
        consumed_at__isnull=True,
    ).update(consumed_at=now)


def verify_email_with_code(user: User, raw_code: str) -> str:
    if user.email_verified:
        return 'already_verified'

    normalized_code = ''.join(ch for ch in str(raw_code or '') if ch.isdigit())
    if len(normalized_code) != 6:
        return 'invalid_code'

    challenge = _active_challenge_for_user(user)
    if not challenge:
        return 'challenge_missing'

    max_attempts = max(1, int(getattr(settings, 'EMAIL_VERIFICATION_MAX_ATTEMPTS', 5)))
    now = timezone.now()

    expected_hash = challenge.code_hash
    provided_hash = _hash_secret(normalized_code)
    if not secrets.compare_digest(expected_hash, provided_hash):
        challenge.attempt_count += 1
        if challenge.attempt_count >= max_attempts:
            challenge.consumed_at = now
        challenge.save(update_fields=['attempt_count', 'consumed_at'])
        return 'too_many_attempts' if challenge.attempt_count >= max_attempts else 'invalid_code'

    challenge.consumed_at = now
    challenge.save(update_fields=['consumed_at'])
    mark_user_email_verified(user)
    return 'verified'


def verify_email_with_token(raw_token: str) -> tuple[User | None, str]:
    token = (raw_token or '').strip()
    if not token:
        return None, 'invalid_token'

    token_hash = _hash_secret(token)
    challenge = (
        EmailVerificationChallenge.objects
        .select_related('user')
        .filter(token_hash=token_hash)
        .first()
    )
    if not challenge:
        return None, 'invalid_token'

    user = challenge.user
    if user.email_verified:
        return user, 'already_verified'

    now = timezone.now()
    if challenge.consumed_at is not None:
        return None, 'token_consumed'

    if challenge.expires_at <= now:
        challenge.consumed_at = now
        challenge.save(update_fields=['consumed_at'])
        return None, 'token_expired'

    challenge.consumed_at = now
    challenge.save(update_fields=['consumed_at'])
    mark_user_email_verified(user)
    return user, 'verified'

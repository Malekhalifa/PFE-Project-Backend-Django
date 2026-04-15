import logging
import json
import time
import hmac
import hashlib
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
import requests as http_requests

from api.models import Job

logger = logging.getLogger(__name__)


def _resolve_webhook_target(job_id: str) -> tuple[str | None, str | None, str | None]:
    try:
        job = (
            Job.objects
            .select_related('user')
            .filter(id=job_id)
            .only(
                'id',
                'webhook_url_override',
                'user__id',
                'user__webhook_url',
                'user__webhook_secret_encrypted',
                'user__notifications_enabled',
            )
            .first()
        )
    except Exception:
        logger.exception('Failed to resolve webhook target for job=%s', job_id)
        return None, None, 'lookup_failed'

    if job is None:
        return None, None, 'job_not_found'

    user = getattr(job, 'user', None)
    if user is None:
        return None, None, 'user_missing'

    if not bool(getattr(user, 'notifications_enabled', True)):
        return None, None, 'notifications_disabled'

    url = ((job.webhook_url_override or '').strip() or (getattr(user, 'webhook_url', '') or '').strip())
    if not url:
        return None, None, 'webhook_url_missing'

    try:
        secret = (user.get_webhook_secret() or '').strip()
    except Exception:
        logger.exception('Failed to decrypt webhook secret for user=%s', user.id)
        return None, None, 'webhook_secret_invalid'

    if not secret:
        return None, None, 'webhook_secret_missing'

    return url, secret, None


def _build_webhook_signature(secret: str, timestamp: str, payload_bytes: bytes) -> str:
    signed = hmac.new(
        key=secret.encode('utf-8'),
        msg=timestamp.encode('utf-8') + b'.' + payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f'sha256={signed}'


def notify_job_webhook_event(
    *,
    job_id: str,
    event_type: str,
    status: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Send a signed callback to the configured webhook URL for the job owner."""
    url, secret, skip_reason = _resolve_webhook_target(job_id)
    if skip_reason:
        return {
            'ok': False,
            'skipped': True,
            'reason': skip_reason,
            'event': event_type,
        }

    body = {
        'event': event_type,
        'job_id': str(job_id),
        'status': status,
        'timestamp': int(time.time()),
        'payload': payload or {},
    }
    if error:
        body['error'] = error

    payload_bytes = json.dumps(body, separators=(',', ':'), sort_keys=True).encode('utf-8')
    header_timestamp = str(int(time.time()))
    signature = _build_webhook_signature(secret, header_timestamp, payload_bytes)
    timeout_seconds = max(1, int(getattr(settings, 'WEBHOOK_CALLBACK_TIMEOUT_SECONDS', 5)))

    headers = {
        'Content-Type': 'application/json',
        'X-QualityLab-Event': event_type,
        'X-QualityLab-Timestamp': header_timestamp,
        'X-QualityLab-Signature': signature,
    }

    try:
        response = http_requests.post(
            url,
            data=payload_bytes,
            headers=headers,
            timeout=timeout_seconds,
        )
        return {
            'ok': 200 <= response.status_code < 300,
            'skipped': False,
            'event': event_type,
            'status_code': response.status_code,
        }
    except http_requests.RequestException as exc:
        logger.warning('Webhook callback failed for job=%s event=%s: %s', job_id, event_type, exc)
        return {
            'ok': False,
            'skipped': False,
            'event': event_type,
            'error': str(exc),
        }


def _build_html_email(title: str, message_html: str, cta_label: str | None = None, cta_href: str | None = None) -> str:
    cta_block = ''
    if cta_label and cta_href:
        cta_block = f"""
            <div style=\"margin-top:24px;\">
              <a href=\"{cta_href}\" style=\"display:inline-block;padding:12px 18px;background:#0f172a;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;\">{cta_label}</a>
            </div>
        """

    return f"""
<!DOCTYPE html>
<html>
  <body style=\"margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;\">
    <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"background:#f3f4f6;padding:24px 0;\">
      <tr>
        <td align=\"center\">
          <table role=\"presentation\" width=\"640\" cellspacing=\"0\" cellpadding=\"0\" style=\"max-width:640px;width:100%;\">
            <tr>
              <td style=\"background:#111827;color:#ffffff;padding:16px 24px;font-size:22px;font-weight:700;border-radius:10px 10px 0 0;\">QualityLab</td>
            </tr>
            <tr>
              <td style=\"background:#ffffff;padding:28px 24px;border-radius:0 0 10px 10px;box-shadow:0 8px 24px rgba(15,23,42,0.08);\">
                <h2 style=\"margin:0 0 14px 0;font-size:24px;color:#111827;\">{title}</h2>
                <div style=\"font-size:16px;line-height:1.6;color:#374151;\">{message_html}</div>
                {cta_block}
                <p style=\"margin-top:26px;font-size:12px;color:#9ca3af;\">This is an automated message from QualityLab</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def _send_html_email(*, to_email: str, subject: str, title: str, message_html: str, cta_label: str | None = None, cta_href: str | None = None) -> int:
    if not to_email:
        logger.info('Skipping email send because recipient is empty. subject=%s', subject)
        return 0

    plain_text = f"{title}\n\nPlease log in to QualityLab to view details."
    html_message = _build_html_email(
        title=title,
        message_html=message_html,
        cta_label=cta_label,
        cta_href=cta_href,
    )
    return send_mail(
        subject=subject,
        message=plain_text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
        html_message=html_message,
    )


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------

def notify_user_analysis_started(user_email: str, job_id: str) -> int:
    subject = '⏳ QualityLab — PostgreSQL analysis started'
    message_html = (
        f'<p>Your PostgreSQL analysis has started for job <strong>#{job_id}</strong>.</p>'
        '<p>We will notify you again as soon as the analysis results are ready for review.</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title='PostgreSQL analysis started',
        message_html=message_html,
        cta_label='Open Dashboard',
        cta_href='/',
    )


def notify_user_analysis_results_ready(user_email: str, job_id: str) -> int:
    subject = '✅ QualityLab — Analysis results are ready'
    message_html = (
        f'<p>Your PostgreSQL analysis pipeline has completed successfully for job <strong>#{job_id}</strong>.</p>'
        '<p>Your reports and AI insights are now available and ready to be reviewed.</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title='Analysis results are ready',
        message_html=message_html,
        cta_label='Open AI Chat Results',
        cta_href=f'/AI-chat/{job_id}',
    )

def notify_user_job_completed(user_email: str, job_id: str) -> int:
    # Backward-compatible wrapper kept for existing call sites.
    return notify_user_analysis_results_ready(user_email, job_id)


def notify_user_cleaning_done(user_email: str, job_id: str) -> int:
    subject = '✅ QualityLab — CSV Cleaning Completed'
    message_html = (
        f'<p>Your CSV cleaning job <strong>#{job_id}</strong> has finished successfully.</p>'
        '<p>You can now review the cleaned output and summary metrics.</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title='CSV cleaning completed',
        message_html=message_html,
        cta_label='View Cleaning Results',
        cta_href=f'/results/{job_id}',
    )


def notify_user_fix_executed(user_email: str, job_id: str, status: str) -> int:
    normalized = str(status or '').strip().lower()
    success = normalized in {'success', 'applied', 'committed'}

    if success:
        subject = '🔧 QualityLab — Fix Applied'
        message_html = (
            f'<p>Your requested fix for job <strong>#{job_id}</strong> was executed successfully.</p>'
            '<p>The transaction was committed and changes are now available in your database.</p>'
        )
        title = 'Fix applied successfully'
    else:
        subject = '🔧 QualityLab — Fix Failed'
        message_html = (
            f'<p>The requested fix for job <strong>#{job_id}</strong> did not complete.</p>'
            '<p>The transaction was rolled back. You can review the error details and retry safely.</p>'
        )
        title = 'Fix execution failed'

    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title=title,
        message_html=message_html,
        cta_label='Open Job Details',
        cta_href=f'/results/{job_id}',
    )


def notify_user_email_notifications_enabled(user_email: str) -> int:
    subject = '📩 QualityLab — Email notifications enabled'
    message_html = (
        '<p>Your email notifications have been successfully enabled.</p>'
        '<p>You will now receive operational updates for analysis lifecycle events, '
        'cleaning completion, and fix execution outcomes.</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title='Email notifications are now enabled',
        message_html=message_html,
        cta_label='Review Notification Settings',
        cta_href='/settings',
    )


def notify_user_email_notifications_disabled(user_email: str) -> int:
    subject = '🔕 QualityLab — Email notifications disabled'
    message_html = (
        '<p>Your email notifications have been turned off successfully.</p>'
        '<p>QualityLab will stop sending non-security operational emails. '
        'Security-related messages required for account protection may still be sent.</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title='Email notifications are now disabled',
        message_html=message_html,
        cta_label='Manage Preferences',
        cta_href='/settings',
    )


def send_email_verification_challenge(
    *,
    user_email: str,
    verification_code: str,
    verify_link: str,
    is_resend: bool = False,
) -> int:
    ttl_minutes = int(getattr(settings, 'EMAIL_VERIFICATION_CODE_TTL_MINUTES', 15))
    subject = '🔐 QualityLab — Verify your email'
    title = 'Verify your email before analysis'
    intro = (
        '<p>We sent this reminder because your account still needs verification.</p>'
        if is_resend
        else '<p>Welcome to QualityLab. Please verify your email before starting analysis.</p>'
    )
    message_html = (
        f'{intro}'
        '<p>You can verify in one click with the button below, or enter this 6-digit code in the app:</p>'
        f'<p style="font-size:24px;font-weight:700;letter-spacing:4px;color:#111827;">{verification_code}</p>'
        f'<p>This code expires in {ttl_minutes} minute(s).</p>'
    )
    return _send_html_email(
        to_email=user_email,
        subject=subject,
        title=title,
        message_html=message_html,
        cta_label='Verify Email',
        cta_href=verify_link,
    )


def notify_admin_job_failed(job_id: str, failed_at_step: str, error_message: str) -> int:
    subject = '🔴 QualityLab ALERT — Pipeline Job Failed'
    message_html = (
        f'<p>A pipeline job has failed.</p>'
        f'<p><strong>Job ID:</strong> {job_id}<br>'
        f'<strong>Failed Step:</strong> {failed_at_step}</p>'
        f'<p><strong>Error Message:</strong><br><code style="white-space:pre-wrap;">{error_message}</code></p>'
    )
    return _send_html_email(
        to_email=settings.ADMIN_EMAIL,
        subject=subject,
        title='Pipeline job failure detected',
        message_html=message_html,
    )


def notify_admin_job_stuck(job_id: str, stuck_at_step: str, duration_minutes: int) -> int:
    subject = '⚠️ QualityLab ALERT — Job Stuck in Pipeline'
    message_html = (
        f'<p>A pipeline job appears to be stuck.</p>'
        f'<p><strong>Job ID:</strong> {job_id}<br>'
        f'<strong>Current Step:</strong> {stuck_at_step}<br>'
        f'<strong>Duration:</strong> {duration_minutes} minute(s)</p>'
    )
    return _send_html_email(
        to_email=settings.ADMIN_EMAIL,
        subject=subject,
        title='Stuck pipeline job detected',
        message_html=message_html,
    )

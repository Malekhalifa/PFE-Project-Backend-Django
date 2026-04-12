import logging
from typing import Any

import httpx

try:
    import brevo_python as brevo_sdk
    _BREVO_LEGACY_SMS_API = True
except ModuleNotFoundError:
    try:
        import brevo as brevo_sdk
        _BREVO_LEGACY_SMS_API = False
    except ModuleNotFoundError:
        brevo_sdk = None
        _BREVO_LEGACY_SMS_API = False

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


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


def _get_sms_api_instance() -> Any | None:
    if brevo_sdk is None:
        logger.warning('Skipping SMS send because Brevo SDK is not installed.')
        return None

    api_key = getattr(settings, 'BREVO_API_KEY', '')
    if not api_key:
        api_key = (getattr(settings, 'ANYMAIL', {}) or {}).get('BREVO_API_KEY', '')
    if not api_key:
        logger.warning('Skipping SMS send because BREVO_API_KEY is not configured.')
        return None

    if _BREVO_LEGACY_SMS_API:
        # Preferred compatibility path requested for brevo-python SDK.
        configuration = brevo_sdk.Configuration()
        configuration.api_key['api-key'] = api_key
        return brevo_sdk.TransactionalSMSApi(
            brevo_sdk.ApiClient(configuration)
        )

    # Fallback path for newer `brevo` SDK layout.
    from brevo.transactional_sms.client import SyncClientWrapper, TransactionalSmsClient

    wrapper = SyncClientWrapper(
        api_key=api_key,
        base_url='https://api.brevo.com',
        timeout=10.0,
        httpx_client=httpx.Client(timeout=10.0),
    )
    return TransactionalSmsClient(client_wrapper=wrapper)


def _send_sms(phone_number: str, content: str) -> Any | None:
    if not phone_number:
        return None

    api_instance = _get_sms_api_instance()
    if not api_instance:
        return None

    payload = brevo_sdk.SendTransacSms(
        sender='QualityLab',
        recipient=phone_number,
        content=content,
        type='transactional',
    )

    if hasattr(api_instance, 'send_transac_sms'):
        return api_instance.send_transac_sms(payload)
    return api_instance.sendTransacSms(payload)


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


# ---------------------------------------------------------------------------
# SMS notifications
# ---------------------------------------------------------------------------

def notify_user_job_completed_sms(phone_number: str | None, job_id: str) -> Any | None:
    if not phone_number:
        return None

    message = f'QualityLab: Your analysis job #{job_id} is ready. Log in to view results.'
    try:
        return _send_sms(phone_number, message)
    except Exception:
        logger.exception('Failed to send user completed SMS for job %s', job_id)
        return None


def notify_admin_job_failed_sms(job_id: str, failed_at_step: str) -> Any | None:
    if not settings.ADMIN_PHONE:
        logger.info('Skipping admin failed SMS because ADMIN_PHONE is not configured.')
        return None

    message = (
        f'QualityLab ALERT: Job #{job_id} failed at step {failed_at_step}. '
        'Check dashboard.'
    )
    try:
        return _send_sms(settings.ADMIN_PHONE, message)
    except Exception:
        logger.exception('Failed to send admin failed SMS for job %s', job_id)
        return None


def notify_admin_job_stuck_sms(job_id: str, stuck_at_step: str, duration_minutes: int) -> Any | None:
    if not settings.ADMIN_PHONE:
        logger.info('Skipping admin stuck SMS because ADMIN_PHONE is not configured.')
        return None

    message = (
        f'QualityLab ALERT: Job #{job_id} is stuck at step {stuck_at_step} '
        f'for {duration_minutes} min. Check dashboard.'
    )
    try:
        return _send_sms(settings.ADMIN_PHONE, message)
    except Exception:
        logger.exception('Failed to send admin stuck SMS for job %s', job_id)
        return None

import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send a test email notification using the configured SMTP backend.'

    def add_arguments(self, parser):
        parser.add_argument('--to', dest='to_email', default='', help='Recipient email. Defaults to ADMIN_EMAIL.')
        parser.add_argument(
            '--subject',
            default='QualityLab SMTP Test Notification',
            help='Subject for the test notification.',
        )
        parser.add_argument(
            '--message',
            default='This is a test notification from QualityLab using Brevo SMTP transport.',
            help='Plain text body for the test notification.',
        )

    def handle(self, *args, **options):
        to_email = (options['to_email'] or settings.ADMIN_EMAIL or '').strip()
        if not to_email:
            raise CommandError('No recipient provided. Use --to or set ADMIN_EMAIL in environment.')

        if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
            raise CommandError(
                'Missing SMTP credentials. Set BREVO_SMTP_LOGIN and BREVO_SMTP_KEY in environment.'
            )

        subject = str(options['subject']).strip() or 'QualityLab SMTP Test Notification'
        plain_message = str(options['message']).strip() or 'SMTP test notification.'
        html_message = f'<p>{plain_message}</p>'

        logger.info(
            'SMTP test: host=%s port=%s tls=%s ssl=%s from=%s to=%s',
            settings.EMAIL_HOST,
            settings.EMAIL_PORT,
            settings.EMAIL_USE_TLS,
            settings.EMAIL_USE_SSL,
            settings.DEFAULT_FROM_EMAIL,
            to_email,
        )

        connection = get_connection(fail_silently=False)
        message = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
            connection=connection,
        )
        message.attach_alternative(html_message, 'text/html')

        try:
            connection.open()
            self.stdout.write(
                self.style.NOTICE(
                    f'SMTP connection opened to {settings.EMAIL_HOST}:{settings.EMAIL_PORT} '
                    f'(TLS={settings.EMAIL_USE_TLS}, SSL={settings.EMAIL_USE_SSL})'
                )
            )

            sent_count = connection.send_messages([message])
            if sent_count != 1:
                raise CommandError(
                    f'Email not sent as expected (sent_count={sent_count}). Check SMTP settings/logs.'
                )

            logger.info('SMTP test email sent successfully to %s', to_email)
            self.stdout.write(self.style.SUCCESS(f'Test notification sent to {to_email}.'))

        except smtplib.SMTPResponseException as exc:
            smtp_error = exc.smtp_error.decode('utf-8', errors='ignore') if isinstance(exc.smtp_error, (bytes, bytearray)) else str(exc.smtp_error)
            logger.exception('SMTP response error while sending test notification: code=%s error=%s', exc.smtp_code, smtp_error)
            raise CommandError(f'SMTP response error (code={exc.smtp_code}): {smtp_error}') from exc
        except smtplib.SMTPException as exc:
            logger.exception('SMTP protocol error while sending test notification: %s', exc)
            raise CommandError(f'SMTP error: {exc}') from exc
        except Exception as exc:
            logger.exception('Unexpected error while sending test notification: %s', exc)
            raise CommandError(f'Unexpected send failure: {exc}') from exc
        finally:
            try:
                connection.close()
            except Exception:
                logger.debug('Ignoring SMTP close error', exc_info=True)

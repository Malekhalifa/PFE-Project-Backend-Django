import os

import django
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.models import Job
from api.views import ChatHistoryView, JobChatView
from users.models import User


class JobChatViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username='chat-user',
            email='chat-user@example.com',
            password='not-used',
            role='user',
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            username='chat-other',
            email='chat-other@example.com',
            password='not-used',
            role='user',
            email_verified=True,
        )
        self.job = Job.objects.create(
            user=self.user,
            file_name='PostgreSQL Connection',
            file_path='postgres://<hidden>',
            status='completed',
            mode='agentic_postgres',
        )

    def test_post_and_get_job_chat_messages(self):
        post_request = self.factory.post(
            f'/api/jobs/{self.job.id}/chat/',
            {'role': 'agent', 'content': 'Analysis summary is ready.'},
            format='json',
        )
        force_authenticate(post_request, user=self.user)

        post_response = JobChatView.as_view()(post_request, job_id=str(self.job.id))
        self.assertEqual(post_response.status_code, 200)
        self.assertTrue(post_response.data.get('ok'))

        get_request = self.factory.get(f'/api/jobs/{self.job.id}/chat/')
        force_authenticate(get_request, user=self.user)

        get_response = JobChatView.as_view()(get_request, job_id=str(self.job.id))
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(len(get_response.data.get('messages', [])), 1)
        self.assertEqual(get_response.data['messages'][0]['role'], 'agent')

    def test_legacy_chat_history_alias_works(self):
        post_request = self.factory.post(
            f'/api/chat-history/{self.job.id}',
            {'role': 'user', 'content': 'Legacy route still works.'},
            format='json',
        )
        force_authenticate(post_request, user=self.user)

        post_response = ChatHistoryView.as_view()(post_request, jobId=str(self.job.id))
        self.assertEqual(post_response.status_code, 200)

        get_request = self.factory.get(f'/api/chat-history/{self.job.id}')
        force_authenticate(get_request, user=self.user)

        get_response = ChatHistoryView.as_view()(get_request, jobId=str(self.job.id))
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(len(get_response.data.get('messages', [])), 1)

    def test_post_rejects_invalid_role(self):
        request = self.factory.post(
            f'/api/jobs/{self.job.id}/chat/',
            {'role': 'system', 'content': 'Not allowed'},
            format='json',
        )
        force_authenticate(request, user=self.user)

        response = JobChatView.as_view()(request, job_id=str(self.job.id))

        self.assertEqual(response.status_code, 400)
        self.assertIn('role', response.data)

    def test_get_denies_non_owner(self):
        request = self.factory.get(f'/api/jobs/{self.job.id}/chat/')
        force_authenticate(request, user=self.other_user)

        response = JobChatView.as_view()(request, job_id=str(self.job.id))

        self.assertEqual(response.status_code, 404)

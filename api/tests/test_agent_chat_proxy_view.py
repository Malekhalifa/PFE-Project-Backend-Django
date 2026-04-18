import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
from rest_framework import status

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from api.views import AgentChatProxyView


class AgentChatProxyViewTests(unittest.TestCase):
    def _request(self, data: dict, user_id: str = 'user-1'):
        return SimpleNamespace(
            data=data,
            user=SimpleNamespace(id=user_id, role='user'),
        )

    @patch('api.views.http_requests.post')
    def test_forwards_frontend_llm_model_to_agentos(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {'content': 'ok'}
        mock_post.return_value = mock_response

        request = self._request(
            {
                'message': 'hello',
                'session_id': 'session-1',
                'llm_model': 'gemini-2.5-pro',
            }
        )

        response = AgentChatProxyView().post(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded_data = mock_post.call_args.kwargs['data']
        self.assertEqual(forwarded_data.get('llm_model'), 'gemini-2.5-pro')
        self.assertEqual(response.data.get('format'), 'markdown')
        self.assertIn('## Response Update', response.data.get('content', ''))

    @patch('api.views.save_chat_message')
    @patch('api.views.get_analysis_result', return_value=None)
    @patch('api.views.get_job', return_value={'llm_model': 'gemini-1.5-pro'})
    @patch('api.views.http_requests.post')
    def test_falls_back_to_job_llm_model_when_request_missing(
        self,
        mock_post,
        _mock_get_job,
        _mock_get_analysis_result,
        mock_save_chat_message,
    ):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {'content': 'ok'}
        mock_post.return_value = mock_response

        request = self._request(
            {
                'message': 'follow-up question',
                'session_id': 'session-2',
                'job_id': 'job-1',
            }
        )

        response = AgentChatProxyView().post(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded_data = mock_post.call_args.kwargs['data']
        self.assertEqual(forwarded_data.get('llm_model'), 'gemini-1.5-pro')
        self.assertEqual(mock_save_chat_message.call_count, 2)

    @patch('api.views.http_requests.post')
    def test_forces_markdown_prompt_policy_and_keeps_markdown_response(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            'content': '## Findings\n\n- **Problem:** Missing PK\n- **Impact:** Duplicate rows\n- **Recommendation:** Add constraint'
        }
        mock_post.return_value = mock_response

        request = self._request(
            {
                'message': 'what are the top issues?',
                'session_id': 'session-3',
            }
        )

        response = AgentChatProxyView().post(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        forwarded_data = mock_post.call_args.kwargs['data']
        self.assertIn('[RESPONSE STYLE]', forwarded_data.get('message', ''))
        self.assertIn('[USER QUESTION]', forwarded_data.get('message', ''))
        self.assertEqual(response.data.get('format'), 'markdown')
        self.assertTrue(response.data.get('content', '').startswith('## Findings'))


if __name__ == '__main__':
    unittest.main()

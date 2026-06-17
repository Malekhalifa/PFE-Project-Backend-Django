import json

from channels.generic.websocket import AsyncWebsocketConsumer


class JobStatusConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.job_id = self.scope["url_route"]["kwargs"]["job_id"]
        self.group_name = f"job_{self.job_id}"

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def job_status(self, event):
        """Standard status update pushed by _set_job_status in Celery tasks."""
        await self.send(text_data=json.dumps({"status": event["status"]}))

    async def job_stuck(self, event):
        """Stuck-job notification pushed by the check_stuck_jobs beat task.

        Expected keys in ``event``:
            status              — the running_* status the job is stuck at
            stuck_duration_ms   — how long the step has been stuck (int)
            user_message        — human-readable message for the frontend
            notification_type   — "stuck_warning" (soft) or "stuck_alert" (hard)
        """
        payload = {
            "type": "stuck",
            "status": event.get("status", ""),
            "stuck_duration_ms": event.get("stuck_duration_ms", 0),
            "user_message": event.get("user_message", ""),
            "notification_type": event.get("notification_type", "stuck_alert"),
        }
        await self.send(text_data=json.dumps(payload))

    async def job_error(self, event):
        """Structured error detail pushed on pipeline failure.

        Expected keys in ``event``:
            error_type    — one of GeminiErrorCode values (str)
            error_step    — the running_* status where the failure occurred
            user_message  — human-readable message
            is_retryable  — bool
            status        — "failed" (or other terminal status)
        """
        payload = {
            "type": "error",
            "error_type": event.get("error_type", "unknown"),
            "error_step": event.get("error_step", ""),
            "user_message": event.get(
                "user_message", "An unexpected error occurred."
            ),
            "is_retryable": event.get("is_retryable", False),
            "status": event.get("status", "failed"),
        }
        await self.send(text_data=json.dumps(payload))

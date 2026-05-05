from django.urls import re_path
from api.consumers import JobStatusConsumer

websocket_urlpatterns = [
    re_path(r"ws/jobs/(?P<job_id>[^/]+)/status/$", JobStatusConsumer.as_asgi()),
]

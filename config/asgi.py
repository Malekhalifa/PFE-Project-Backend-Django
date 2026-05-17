"""
ASGI config for QualityLab project.
"""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# get_asgi_application() must be called first — it runs django.setup()
# which initialises the app registry. Any import of Django models
# (including our JWT middleware) must come after this line.
from django.core.asgi import get_asgi_application
_django_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from api.routing import websocket_urlpatterns
from api.middleware import JwtAuthMiddlewareStack

application = ProtocolTypeRouter({
    "http": _django_app,
    "websocket": JwtAuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})
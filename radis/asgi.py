"""
ASGI config for radis project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/3.0/howto/deployment/asgi/

Adapted to use Channels, see
https://channels.readthedocs.io/en/latest/deploying.html#run-protocol-servers
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "radis.settings.development")

# Initialize OpenTelemetry before Django loads to ensure all requests are traced
from adit_radis_shared.telemetry import setup_opentelemetry  # noqa: E402

setup_opentelemetry()

from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter  # noqa: E402

application = ProtocolTypeRouter({"http": django_asgi_app})

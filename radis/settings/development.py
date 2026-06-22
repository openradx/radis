from .base import *  # noqa: F403
from .base import env

DEBUG = True

ENVIRONMENT = "development"

# Developer evaluation harness is on in development by default. Production
# and test environments inherit the False default from base. See
# LABELS_EVAL_ENABLED in base.py for the gating semantics. Tests run
# against this settings module (pyproject.toml: DJANGO_SETTINGS_MODULE =
# "radis.settings.development") so they pick up True automatically.
LABELS_EVAL_ENABLED = True

INTERNAL_IPS = env.list("DJANGO_INTERNAL_IPS")

REMOTE_DEBUGGING_ENABLED = env.bool("REMOTE_DEBUGGING_ENABLED")
REMOTE_DEBUGGING_PORT = env.int("REMOTE_DEBUGGING_PORT")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

INSTALLED_APPS += [  # noqa: F405
    "debug_toolbar",
    "debug_permissions",
    "django_browser_reload",
]

MIDDLEWARE += [  # noqa: F405
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django_browser_reload.middleware.BrowserReloadMiddleware",
]

if env.bool("FORCE_DEBUG_TOOLBAR"):
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda _: True}

LOGGING["loggers"]["radis"]["level"] = "DEBUG"  # noqa: F405

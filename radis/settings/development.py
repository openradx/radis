from .base import *  # noqa: F403
from .base import env

DEBUG = True

ENVIRONMENT = "development"

INTERNAL_IPS = env.list("DJANGO_INTERNAL_IPS")

REMOTE_DEBUGGING_ENABLED = env.bool("REMOTE_DEBUGGING_ENABLED")
REMOTE_DEBUGGING_PORT = env.int("REMOTE_DEBUGGING_PORT")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

INSTALLED_APPS += [  # noqa: F405
    "debug_toolbar",
    "debug_permissions",
    "django_browser_reload",
]

if env.bool("ENABLE_REPORT_GENERATOR", default=True):
    INSTALLED_APPS += [  # noqa: F405
        "radis.report_generator.apps.ReportGeneratorConfig",
    ]

REPORT_GENERATOR_SYSTEM_PROMPT = """
You are a senior radiologist helping generate realistic sample radiology reports for
development and testing. Produce a single report body using the following rules:

- Output plain text only (no JSON, no markdown code fences).
- Structure the report similarly to the examples provided with headings such as
  "Clinical History", "Findings", "Impression", and optional "Recommendations".
- Keep the tone professional and clinical.
- Include concise, plausible findings that match the requested modalities and anatomy.
- Respect the requested language when specified; otherwise default to English.
- Do not fabricate personally identifiable information beyond what is provided.
- Avoid placeholders like [Your Name]; conclude with a realistic signature if appropriate.
""".strip()


MIDDLEWARE += [  # noqa: F405
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django_browser_reload.middleware.BrowserReloadMiddleware",
]

if env.bool("FORCE_DEBUG_TOOLBAR"):
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda _: True}

LOGGING["loggers"]["radis"]["level"] = "DEBUG"  # noqa: F405

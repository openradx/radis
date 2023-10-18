"""
Django settings for radis project.

Generated by 'django-admin startproject' using Django 3.0.7.

For more information on this file, see
https://docs.djangoproject.com/en/3.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.0/ref/settings/
"""

from pathlib import Path

import environ
import toml

env = environ.Env()

# The base directory of the project (the root of the repository)
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent

# Read pyproject.toml file
pyproject = toml.load(BASE_DIR / "pyproject.toml")

RADIS_VERSION = pyproject["tool"]["poetry"]["version"]

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)  # type: ignore
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

BASE_URL = env.str("BASE_URL", default="http://localhost")  # type: ignore

SITE_ID = 1

# Used by our custom migration radis.core.migrations.0002_UPDATE_SITE_NAME
# to set the domain and name of the sites framework
RADIS_SITE_DOMAIN = env.str("RADIS_SITE_DOMAIN", default="radis.org")  # type: ignore
RADIS_SITE_NAME = env.str("RADIS_SITE_NAME", default="radis.org")  # type: ignore

INSTALLED_APPS = [
    "daphne",
    "whitenoise.runserver_nostatic",
    "registration",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.sites",
    "django.contrib.postgres",
    "dbbackup",
    "revproxy",
    "loginas",
    "crispy_forms",
    "crispy_bootstrap5",
    "django_htmx",
    "django_tables2",
    "rest_framework",
    "adrf",
    "radis.core.apps.CoreConfig",
    "radis.accounts.apps.AccountsConfig",
    "radis.token_authentication.apps.TokenAuthenticationConfig",
    "radis.api.apps.ApiConfig",
    "radis.reports.apps.ReportsConfig",
    "radis.collections.apps.CollectionsConfig",
    "channels",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sites.middleware.CurrentSiteMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "radis.core.middlewares.MaintenanceMiddleware",
    "radis.core.middlewares.TimezoneMiddleware",
]

ROOT_URLCONF = "radis.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "radis" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "radis.core.site.base_context_processor",
            ],
        },
    },
]

WSGI_APPLICATION = "radis.wsgi.application"

# env.db() loads the DB setup from the DATABASE_URL environment variable using
# Django-environ.
# The sqlite database is still used for pytest tests.
DATABASES = {"default": env.db(default="sqlite:///radis-sqlite.db")}  # type: ignore

# Django 3.2 switched to BigAutoField for primary keys. It must be set explicitly
# and requires a migration.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Password validation
# https://docs.djangoproject.com/en/3.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# See following examples:
# https://github.com/django/django/blob/master/django/utils/log.py
# https://cheat.readthedocs.io/en/latest/django/logging.html
# https://stackoverflow.com/a/7045981/166229
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
        "require_debug_true": {
            "()": "django.utils.log.RequireDebugTrue",
        },
    },
    "formatters": {
        "simple": {
            "format": "[%(asctime)s] %(name)-12s %(levelname)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S %Z",
        },
        "verbose": {
            "format": "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S %Z",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "mail_admins": {
            "level": "CRITICAL",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
    },
    "loggers": {
        "radis": {
            "handlers": ["console", "mail_admins"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "mail_admins"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": "ERROR"},
}

# Internationalization
# https://docs.djangoproject.com/en/3.0/topics/i18n/

LANGUAGE_CODE = "de-de"

# We don't want to have German translations, but everything in English
USE_I18N = False

# But we still want to have dates and times localized
USE_L10N = True

USE_TZ = True

TIME_ZONE = "UTC"

# All REST API requests must come from authenticated clients
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "radis.token_authentication.auth.RestTokenAuthentication",
    ],
}


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/

STATICFILES_DIRS = (BASE_DIR / "radis" / "static",)

STATIC_URL = "/static/"

STATIC_ROOT = env.str("DJANGO_STATIC_ROOT", default=(BASE_DIR / "staticfiles"))  # type: ignore

# Custom user model
AUTH_USER_MODEL = "accounts.User"

# Where to redirect to after login
LOGIN_REDIRECT_URL = "home"

# django-dbbackup
DBBACKUP_STORAGE = "django.core.files.storage.FileSystemStorage"
DBBACKUP_STORAGE_OPTIONS = {
    "location": env.str("BACKUP_DIR", default=(BASE_DIR / "backups")),  # type: ignore
}
DBBACKUP_CLEANUP_KEEP = 30

# For crispy forms
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# django-templates2
DJANGO_TABLES2_TEMPLATE = "django_tables2/bootstrap5.html"

# This seems to be important for development on Gitpod as CookieStorage
# and FallbackStorage does not work there.
# Seems to be the same problem with Cloud9 https://stackoverflow.com/a/34828308/166229
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

EMAIL_SUBJECT_PREFIX = "[RADIS] "

# An Email address used by the RADIS server to notify about finished jobs and
# management notifications.
SERVER_EMAIL = env.str("DJANGO_SERVER_EMAIL", default="support@radis.test")  # type: ignore
DEFAULT_FROM_EMAIL = SERVER_EMAIL

# A support Email address that is presented to the users where
# they can get support.
SUPPORT_EMAIL = env.str("SUPPORT_EMAIL", default=SERVER_EMAIL)  # type: ignore

# Also used by django-registration-redux to send account approval emails
admin_first_name = env.str("ADMIN_FIRST_NAME", default="RADIS")  # type: ignore
admin_last_name = env.str("ADMIN_LAST_NAME", default="Admin")  # type: ignore
admin_full_name = admin_first_name + " " + admin_last_name
ADMINS = [
    (
        admin_full_name,
        env.str("ADMIN_EMAIL", default="admin@radis.test"),  # type: ignore
    )
]

# Settings for django-registration-redux
REGISTRATION_FORM = "radis.accounts.forms.RegistrationForm"
ACCOUNT_ACTIVATION_DAYS = 14
REGISTRATION_OPEN = True

# Channels
ASGI_APPLICATION = "radis.asgi.application"

# RabbitMQ is used as Celery message broker
RABBITMQ_URL = env.str("RABBITMQ_URL", default="amqp://localhost")  # type: ignore

# Rabbit Management console is integrated in RADIS by using an reverse
# proxy (django-revproxy).This allows to use the authentication of RADIS.
# But as RabbitMQ authentication can't be disabled we have to login
# there with "guest" as username and password again.
RABBIT_MANAGEMENT_HOST = env.str("RABBIT_MANAGEMENT_HOST", default="localhost")  # type: ignore
RABBIT_MANAGEMENT_PORT = env.int("RABBIT_MANAGEMENT_PORT", default=15672)  # type: ignore

# Celery
# see https://github.com/celery/celery/issues/5026 for how to name configs
if USE_TZ:
    CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_URL = RABBITMQ_URL
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_TASK_DEFAULT_QUEUE = "default_queue"
CELERY_TASK_ROUTES = {}
CELERY_BEAT_SCHEDULE = {}

# No need for a result backend as we don't use Celery Canvas.
CELERY_IGNORE_RESULT = True

# Settings for priority queues, see also apply_async calls in the models.
# Requires RabbitMQ as the message broker!
CELERY_TASK_QUEUE_MAX_PRIORITY = 10
CELERY_TASK_DEFAULT_PRIORITY = 5

# Only non prefetched tasks can be sorted by their priority. So we only
# prefetch only one task at a time.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Not sure if this is really necessary for priorities to work, but saw this mentioned
# https://medium.com/better-programming/python-celery-best-practices-ae182730bb81
# https://stackoverflow.com/a/47980598/166229
CELERY_TASK_ACKS_LATE = True

# Flower is integrated in RADIS by using a reverse proxy (django-revproxy).
# This allows to use the authentication of RADIS.
FLOWER_HOST = env.str("FLOWER_HOST", default="localhost")  # type: ignore
FLOWER_PORT = env.int("FLOWER_PORT", default=5555)  # type: ignore

# Redis is used for distributed locks (sherlock).
REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")  # type: ignore

# Used by django-filter
FILTERS_EMPTY_CHOICE_LABEL = "Show All"

# Vespa
VESPA_HOST = env.str("VESPA_HOST", default="localhost")  # type: ignore
VESPA_CONFIG_PORT = env.int("VESPA_CONFIG_PORT", default=19071)  # type: ignore
VESPA_DATA_PORT = env.int("VESPA_DATA_PORT", default=8080)  # type: ignore


# A timezone that is used for users of the web interface.
USER_TIME_ZONE = env.str("USER_TIME_ZONE", default="Europe/Berlin")  # type: ignore

# The salt that is used for hashing new tokens in the token authentication app.
# Cave, changing the salt after some tokens were already generated makes them all invalid!
TOKEN_AUTHENTICATION_SALT = env.str(
    "TOKEN_AUTHENTICATION_SALT",
    default="Rn4YNfgAar5dYbPu",  # type: ignore
)

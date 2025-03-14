"""
Django settings for radis project.

Generated by 'django-admin startproject' using Django 3.0.7.

For more information on this file, see
https://docs.djangoproject.com/en/3.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.0/ref/settings/
"""

from pathlib import Path

from environs import env

# During development and calling `manage.py` from the host we have to load the .env file manually.
# Some env variables will still need a default value, as those are only set in the compose file.
if not env.bool("IS_DOCKER_CONTAINER", default=False):
    env.read_env()

# The base directory of the project (the root of the repository)
BASE_PATH = Path(__file__).resolve(strict=True).parent.parent.parent

# The source paths of the project
SOURCE_PATHS = [BASE_PATH / "radis"]

# Fetch version from the environment which is passed through from the git version tag
PROJECT_VERSION = env.str("PROJECT_VERSION", default="v0.0.0")

# The project URL used in the navbar and footer
PROJECT_URL = "https://github.com/openradx/radis"

# Needed by sites framework
SITE_ID = 1

# The following settings are synced to the Site model of the sites framework on startup
# (see common/apps.py in adit-radis-shared).
SITE_DOMAIN = env.str("SITE_DOMAIN")
SITE_NAME = env.str("SITE_NAME")

SECRET_KEY = env.str("DJANGO_SECRET_KEY")

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "daphne",
    "whitenoise.runserver_nostatic",
    "adit_radis_shared.common.apps.CommonConfig",
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
    "django_extensions",
    "procrastinate.contrib.django",
    "dbbackup",
    "revproxy",
    "loginas",
    "django_cotton.apps.SimpleAppConfig",
    "block_fragments.apps.SimpleAppConfig",
    "crispy_forms",
    "crispy_bootstrap5",
    "django_htmx",
    "django_tables2",
    "formtools",
    "rest_framework",
    "adrf",
    "markdownify",
    "radis.core.apps.CoreConfig",
    "adit_radis_shared.accounts.apps.AccountsConfig",
    "adit_radis_shared.token_authentication.apps.TokenAuthenticationConfig",
    "radis.reports.apps.ReportsConfig",
    "radis.search.apps.SearchConfig",
    "radis.extractions.apps.ExtractionsConfig",
    "radis.subscriptions.apps.SubscriptionsConfig",
    "radis.collections.apps.CollectionsConfig",
    "radis.notes.apps.NotesConfig",
    "radis.chats.apps.ChatsConfig",
    "radis.pgsearch.apps.PgSearchConfig",
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
    "adit_radis_shared.accounts.middlewares.ActiveGroupMiddleware",
    "adit_radis_shared.common.middlewares.MaintenanceMiddleware",
    "adit_radis_shared.common.middlewares.TimezoneMiddleware",
]

ROOT_URLCONF = "radis.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "OPTIONS": {
            "loaders": [
                (
                    "block_fragments.loader.Loader",
                    [
                        (
                            "django.template.loaders.cached.Loader",
                            [
                                "django_cotton.cotton_loader.Loader",
                                "django.template.loaders.filesystem.Loader",
                                "django.template.loaders.app_directories.Loader",
                            ],
                        )
                    ],
                )
            ],
            "builtins": [
                "django_cotton.templatetags.cotton",
            ],
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "adit_radis_shared.common.site.base_context_processor",
                "radis.reports.site.base_context_processor",
            ],
        },
    },
]

WSGI_APPLICATION = "radis.wsgi.application"

ASGI_APPLICATION = "radis.asgi.application"

# This seems to be important for Cloud IDEs as CookieStorage does not work there.
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# DATABASE_URL is only set in the compose file. For local development on the host
# we use the port from the .env file directly.
postgres_dev_port = env.int("POSTGRES_DEV_PORT", default=5432)
database_url = f"postgres://postgres:postgres@localhost:{postgres_dev_port}/postgres"
DATABASES = {"default": env.dj_db_url("DATABASE_URL", default=database_url)}

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom user model
AUTH_USER_MODEL = "accounts.User"

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

# A custom authentication backend that supports a single currently active group.
AUTHENTICATION_BACKENDS = ["adit_radis_shared.accounts.backends.ActiveGroupModelBackend"]

# Where to redirect to after login
LOGIN_REDIRECT_URL = "home"

# Settings for django-registration-redux
REGISTRATION_FORM = "adit_radis_shared.accounts.forms.RegistrationForm"
ACCOUNT_ACTIVATION_DAYS = 14
REGISTRATION_OPEN = True

EMAIL_SUBJECT_PREFIX = "[RADIS] "

# An Email address used by the RADIS server to notify about finished jobs and
# management notifications.
SERVER_EMAIL = env.str("DJANGO_SERVER_EMAIL")
DEFAULT_FROM_EMAIL = SERVER_EMAIL

# A support Email address that is presented to the users where
# they can get support.
SUPPORT_EMAIL = env.str("SUPPORT_EMAIL")

# Also used by django-registration-redux to send account approval emails
ADMINS = [(env.str("DJANGO_ADMIN_FULL_NAME"), env.str("DJANGO_ADMIN_EMAIL"))]

# All REST API requests must come from authenticated clients
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "adit_radis_shared.token_authentication.auth.RestTokenAuthentication",
    ],
}

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

TIME_ZONE = "UTC"

# We don't want to have German translations, but everything in English
USE_I18N = False

USE_TZ = True

# A timezone that is presented to the users of the web interface.
USER_TIME_ZONE = env.str("USER_TIME_ZONE")

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/
STATIC_URL = "/static/"

# Additional (project wide) static files
STATICFILES_DIRS = (BASE_PATH / "radis" / "static",)

# For crispy forms
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# django-templates2
DJANGO_TABLES2_TEMPLATE = "common/_django_tables2.html"

# The salt that is used for hashing new tokens in the token authentication app.
# Cave, changing the salt after some tokens were already generated makes them all invalid!
TOKEN_AUTHENTICATION_SALT = env.str("TOKEN_AUTHENTICATION_SALT")

# django-dbbackup
DBBACKUP_STORAGE = "django.core.files.storage.FileSystemStorage"
DBBACKUP_STORAGE_OPTIONS = {
    "location": env.str("DBBACKUP_STORAGE_LOCATION", default="/tmp/backups-radis")
}
DBBACKUP_CLEANUP_KEEP = 30

# Used by django-filter
FILTERS_EMPTY_CHOICE_LABEL = "Show All"

# LLM configuration
LLM_MODEL_NAME = env.str("LLM_MODEL_NAME", default="unused")
EXTERNAL_LLM_PROVIDER_URL = env.str("EXTERNAL_LLM_PROVIDER_URL", default="")
EXTERNAL_LLM_PROVIDER_API_KEY = env.str("EXTERNAL_LLM_PROVIDER_API_KEY", default="")
LLM_SERVICE_DEV_PORT = env.int("LLM_SERVICE_DEV_PORT", default=8080)
LLM_SERVICE_URL = env.str("LLM_SERVICE_URL", default=f"http://localhost:{LLM_SERVICE_DEV_PORT}/v1")

# Chat
CHAT_GENERATE_TITLE_SYSTEM_PROMPT = """
Summarize the following conversation in $num_words words or less and in the same language as
the conversation. Be concise and don't hallucinate.

User:
$user_prompt

Assistant:
$assistant_response
"""

CHAT_GENERAL_SYSTEM_PROMPT = """
You are an AI medical assistant with extensive knowledge in radiology and general medicine.
You have been trained on a wide range of medical literature, including the latest research
and guidelines in radiological practices.
You wil discuss and answer various questions about radiology and general medicine.
Provide concise, well-structured answers in the same language used in the question. Do use 
appropriate medical terminology. Use headers to organize information when necessary. Include
relevant anatomical details, imaging modalities, and diagnostic considerations where applicable.
Base your responses on current, peer-reviewed medical literature and established radiological
guidelines. If there are conflicting views or ongoing debates in the field, acknowledge them
briefly.
"""

CHAT_REPORT_SYSTEM_PROMPT = """
You are an AI medical assistant with extensive knowledge in radiology and general medicine.
You have been trained on a wide range of medical literature, including the latest research
and guidelines in radiological practices.
You will be asked questions about a radiology report that you have to answer. The report and
question can be given in any language.
Provide short, concise and well-structured answers in the same language used in the question.
Do use appropriate medical terminology. Use headers to organize information when necessary.
Include relevant anatomical details, imaging modalities, and diagnostic considerations where
applicable. Base your responses on current, peer-reviewed medical literature and established
radiological guidelines. If there are conflicting views or ongoing debates in the field,
acknowledge them briefly. Don't make up new data that is not mentioned in the report and
don't hallucinate.

Report:
$report
"""

# Subscription
QUESTIONS_SYSTEM_PROMPT = """
You are an AI medical assistant with extensive knowledge in radiology and general medicine.
You have been trained on a wide range of medical literature, including the latest research
and guidelines in radiological practices.
Answer the following questions from the given radiology report. The report and questions can
be given in any language.
Base your answers only on the information provided in the report. Don't hallucinate.
Return the answer in JSON format. Answer with 'true' for 'yes' and 'false' for 'no'.

Radiology Report:
$report

Questions:
$questions
"""

# Extraction
OUTPUT_FIELDS_SYSTEM_PROMPT = """
You are an AI medical assistant with extensive knowledge in radiology and general medicine.
You have been trained on a wide range of medical literature, including the latest research
and guidelines in radiological practices. 
Extract the following information from the given radiology report by using the below field
definitions. The report and fields can be given in any language.
Only provide data that is really found in the report. Don't make up new data and don't hallucinate.
Return the extracted information in JSON format.

Radiology Report:
$report

Fields to extract:
$fields
"""

# The maximum number of reports that can be extracted by one extraction job.
EXTRACTION_MAXIMUM_REPORTS_COUNT = 25000

# The default and urgent priorities for extraction tasks.
EXTRACTION_DEFAULT_PRIORITY = 2
EXTRACTION_URGENT_PRIORITY = 3

# The number of extraction instances that are processed within one extraction task.
EXTRACTION_TASK_BATCH_SIZE = 100

# The number of parallel requests the LLM can handle. This limit is enforced within each task. When
# having multiple workers that uses the LLM, the total number of parallel requests is
# EXTRACTION_LLM_CONCURRENCY_LIMIT * number of workers. Either the number of HTTP Threads and
# number of parallel computing slots of the llama.cpp should be set to match this number or the
# continuous batching capability of the LLM or a combination of both should be used.
EXTRACTION_LLM_CONCURRENCY_LIMIT = 6

START_EXTRACTION_JOB_UNVERIFIED = False


# Subscription
SUBSCRIPTION_DEFAULT_PRIORITY = 3
SUBSCRIPTION_URGENT_PRIORITY = 4
SUBSCRIPTION_CRON = "* * * * *"
SUBSCRIPTION_REFRESH_TASK_BATCH_SIZE = 100

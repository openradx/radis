from .base import *  # noqa: F403

DEBUG = False

# We must force our background worker that is started while testing
# in a subprocess to use the test database.
if not DATABASES["default"]["NAME"].startswith("test_"):  # noqa: F405
    test_database = "test_" + DATABASES["default"]["NAME"]  # noqa: F405
    DATABASES["default"]["NAME"] = test_database  # noqa: F405
    DATABASES["default"]["TEST"] = {"NAME": test_database}  # noqa: F405

DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}

# Tests must not hit a live embedding service. Blanking the URL makes
# AsyncEmbeddingClient.__init__ raise EmbeddingClientError, which
# embed_reports_inline catches and logs as a WARNING; reports save fine,
# embedding stays NULL. Tests that exercise the embedding path explicitly
# patch the client (see test_provider_hybrid.py).
EMBEDDING_PROVIDER_URL = ""

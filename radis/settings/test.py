from .base import *  # noqa: F403

DEBUG = False

# We must force our background worker that is started while testing
# in a subprocess to use the test database.
if not DATABASES["default"]["NAME"].startswith("test_"):  # noqa: F405
    test_database = "test_" + DATABASES["default"]["NAME"]  # noqa: F405
    DATABASES["default"]["NAME"] = test_database  # noqa: F405
    DATABASES["default"]["TEST"] = {"NAME": test_database}  # noqa: F405

DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}

# Tests must not hit a live embedding service. Embedding work is deferred via
# a Procrastinate task; tests do not run a worker by default. Blanking the URL
# means any incidental construction of EmbeddingClient/AsyncEmbeddingClient
# fast-fails into EmbeddingClientError rather than touching the network. Tests
# that exercise the embedding path explicitly patch the client.
EMBEDDING_PROVIDER_URL = ""

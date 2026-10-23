from .base import *  # noqa: F403

DEBUG = False

# We must force our background worker that is started while testing
# in a subprocess to use the test database.
if not DATABASES["default"]["NAME"].startswith("test_"):  # noqa: F405
    test_database = "test_" + DATABASES["default"]["NAME"]  # noqa: F405
    DATABASES["default"]["NAME"] = test_database  # noqa: F405
    DATABASES["default"]["TEST"] = {"NAME": test_database}  # noqa: F405

DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}

# Tests must not hit a live embedding service. Embedding work is deferred via a
# Procrastinate task and tests do not run a worker by default, but leaving the model
# unset also means any incidental EmbeddingClient construction fast-fails into
# EmbeddingClientError rather than touching the network. Tests that exercise the
# embedding path set EMBEDDINGS_MODEL explicitly and patch the client.
EMBEDDINGS_MODEL = None

# The suite's numeric assertions assume the shipped defaults of the search knobs, and
# base.py reads .env outside Docker, so a developer's overrides must not reach the tests.
# SEARCH_HNSW_EF_SEARCH is not pinned: base.py wires it into the database connection
# options at import, where a pin here could not follow.
SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 300
SEARCH_SEMANTIC_ITERATIVE_SCAN = True
SEARCH_SEMANTIC_MIN_SIMILARITY = None
EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS = 900
EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS = 10.0

# No real backups as a side effect of tests that run the worker (the periodic
# backup_db task would fire when a test run crosses its cron time).
BACKUP_ENABLED = False

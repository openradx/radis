import nest_asyncio
import pytest

pytest_plugins = [
    "adit_radis_shared.pytest_fixtures",
    "radis.chats.utils.testing_helpers",
]


def pytest_configure():
    # pytest-asyncio doesn't play well with pytest-playwright as
    # pytest-playwright creates an event loop for the whole test suite and
    # pytest-asyncio can't create an additional one then.
    # nest_syncio works around this this by allowing to create nested loops.
    # https://github.com/pytest-dev/pytest-asyncio/issues/543
    # https://github.com/microsoft/playwright-pytest/issues/167
    nest_asyncio.apply()


@pytest.fixture(scope="session", autouse=True)
def _terminate_orphan_connections_before_db_drop(django_db_setup, django_db_blocker):
    """Terminate lingering connections to the test DB before pytest-django drops it.

    Procrastinate runs sync task functions in ``sync_to_async`` worker threads
    (see https://github.com/procrastinate-org/procrastinate/issues/1106). Each
    thread opens its own Django DB connection; Django doesn't auto-close
    per-thread connections, so connections linger after the task returns.
    Periodic tasks like ``subscription_launcher`` fire whenever
    ``run_worker_once`` runs in tests and leak a connection this way.

    Pytest-django's session-level finalizer drops the test DB, which fails
    with ``database "test_postgres" is being accessed by other users`` when
    such connections are alive.

    This fixture's finalizer runs *before* django_db_setup's (fixtures tear
    down LIFO; we depend on django_db_setup so we're set up later and torn
    down earlier). It uses ``django_db_blocker.unblock()`` to bypass
    pytest-django's session-level DB-access guard, then terminates every
    other connection to the current database so the subsequent DROP DATABASE
    succeeds cleanly.

    The proper per-task fix is calling ``db.close_old_connections()`` at the
    end of every task that uses Django ORM (see ADIT's tasks for the pattern).
    This session safety net catches anything that slips through.
    """
    yield
    from django.db import connection, connections

    with django_db_blocker.unblock():
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "  AND pid <> pg_backend_pid()"
                )
        finally:
            connections.close_all()

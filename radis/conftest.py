import stamina

pytest_plugins = ["adit_radis_shared.pytest_fixtures"]


def pytest_configure():
    # Disable stamina retries for tests by default; transient-blip retry
    # behaviour is exercised explicitly where needed via `stamina.set_active`.
    stamina.set_active(False)

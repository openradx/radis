import pytest


@pytest.fixture(autouse=True)
def media_root(tmp_path, settings):
    """Store uploaded files in a per-test temp directory and auto-clean up."""
    settings.MEDIA_ROOT = tmp_path
    yield

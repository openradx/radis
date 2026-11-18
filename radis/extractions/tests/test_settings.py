import importlib
import os

import pytest
from django.conf import settings as dj_settings

from radis.settings import base as settings_base


def test_extraction_maximum_reports_count_defaults_to_25000():
    # Only the fallback is under test; a value configured for the environment the
    # tests run in is a deployment choice (see radis/core/tests/test_llm_settings.py).
    if os.environ.get("EXTRACTION_MAXIMUM_REPORTS_COUNT", "").strip():
        pytest.skip("EXTRACTION_MAXIMUM_REPORTS_COUNT is set in the environment")

    assert dj_settings.EXTRACTION_MAXIMUM_REPORTS_COUNT == 25000


@pytest.fixture
def _restore_base_settings_after_reload():
    """Reloading the settings module is the only way to watch a module-level
    assignment re-read the environment; see the fixture of the same name in
    radis/core/tests/test_embeddings_settings.py for why this is safe and why it
    must be requested before ``monkeypatch``."""
    yield
    importlib.reload(settings_base)


def test_extraction_maximum_reports_count_is_read_from_the_environment(
    _restore_base_settings_after_reload, monkeypatch
):
    monkeypatch.setenv("EXTRACTION_MAXIMUM_REPORTS_COUNT", "100")

    reloaded = importlib.reload(settings_base)

    assert reloaded.EXTRACTION_MAXIMUM_REPORTS_COUNT == 100

from django.conf import settings


def test_search_settings_defaults():
    assert settings.SEARCH_FTS_MAX_RESULTS == 10_000
    assert settings.SEARCH_HNSW_EF_SEARCH == 500
    assert settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS == 300
    assert settings.SEARCH_RERANK_WINDOW == 1000
    assert settings.SEARCH_RERANK_SEMANTIC_WEIGHT == 0.7
    assert settings.SEARCH_SEMANTIC_ITERATIVE_SCAN is True
    assert settings.SEARCH_SEMANTIC_MIN_SIMILARITY is None
    assert settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS == 10.0
    assert settings.SEARCH_DESCRIPTION_TIMEOUT == 10
    assert settings.SEARCH_DESCRIPTION_MAX_TOKENS == 200
    assert settings.SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS == 3600
    assert "radiology reports" in settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT


def test_the_beam_width_rides_on_the_connection_options():
    options = settings.DATABASES["default"]["OPTIONS"]["options"]
    assert options == f"-c hnsw.ef_search={settings.SEARCH_HNSW_EF_SEARCH}"


def test_no_hybrid_setting_remains():
    # The names are assembled so that a repository search for the old prefix only finds
    # settings that were left behind, not this list of the ones that must be gone.
    prefix = "HYBRID"
    for suffix in ("FTS_MAX_RESULTS", "RRF_K", "HNSW_EF_SEARCH", "FUSED_CACHE_TIMEOUT_SECONDS"):
        name = f"{prefix}_{suffix}"
        assert not hasattr(settings, name), name

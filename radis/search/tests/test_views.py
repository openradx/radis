from html.parser import HTMLParser
from unittest.mock import Mock, patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.reports.factories import LanguageFactory, ModalityFactory, ReportFactory
from radis.search.site import (
    FullTextSearch,
    ReportDocument,
    Search,
    SearchProvider,
    SearchResult,
    SemanticSearch,
)
from radis.search.utils.query_parser import QueryParser


def create_test_user_with_active_group():
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()
    return user


def create_test_search_provider():
    # Create actual Report objects in database so template can resolve full_report.id
    report1 = ReportFactory.create(
        document_id="TEST_DOC_1",
        language=LanguageFactory.create(code="en"),
        pacs_name="Test PACS",
        pacs_link="http://test.pacs.com",
        patient_age=45,
        patient_sex="M",
        study_description="Test Study 1",
    )
    report2 = ReportFactory.create(
        document_id="TEST_DOC_2",
        language=LanguageFactory.create(code="en"),
        pacs_name="Test PACS",
        pacs_link="http://test.pacs.com",
        patient_age=32,
        patient_sex="F",
        study_description="Test Study 2",
    )

    def mock_search(search):
        documents = [
            ReportDocument(
                relevance=0.9,
                document_id=report1.document_id,
                pacs_name=report1.pacs_name,
                pacs_link=report1.pacs_link,
                patient_age=report1.patient_age,
                patient_sex=report1.patient_sex,
                study_description=report1.study_description,
                modalities=["CT"],
                summary="Test summary 1",
            ),
            ReportDocument(
                relevance=0.8,
                document_id=report2.document_id,
                pacs_name=report2.pacs_name,
                pacs_link=report2.pacs_link,
                patient_age=report2.patient_age,
                patient_sex=report2.patient_sex,
                study_description=report2.study_description,
                modalities=["MR"],
                summary="Test summary 2",
            ),
        ]
        return SearchResult(total_count=2, total_relation="exact", documents=documents)

    return SearchProvider(name="Test Provider", search=mock_search, max_results=1000)


@pytest.mark.django_db
def test_search_view_get_no_params(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    response = client.get("/search/")
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
def test_search_view_unauthenticated(client: Client):
    response = client.get("/search/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_search_view_no_active_group(client: Client):
    user = UserFactory.create(is_active=True)

    client.force_login(user)

    response = client.get("/search/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_search_view_valid_query(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    language = LanguageFactory.create(code="en")
    modality = ModalityFactory.create(code="CT", filterable=True)

    search_params = {
        "query": "krüşk",
        "language": language.code,
        "modalities": [modality.code],
        "study_description": "test study",
        "patient_sex": "M",
        "age_from": 30,
        "age_till": 60,
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context
        assert "documents" in response.context
        assert len(response.context["documents"]) == 2
        assert response.context["total_count"] == 2
        assert response.context["total_relation"] == "exact"


@pytest.mark.django_db
def test_search_view_pagination(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "page": "1",
        "per_page": "10",
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context
        assert "documents" in response.context
        assert len(response.context["documents"]) == 2
        assert response.context["total_count"] == 2


@pytest.mark.django_db
def test_search_view_invalid_page(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "page": "1000",  # Very high page number
        "per_page": "25",
    }

    test_provider = SearchProvider(
        name="Test Provider",
        search=lambda search: SearchResult(total_count=10, total_relation="exact", documents=[]),
        max_results=100,
    )

    with patch("radis.search.views.search_provider", test_provider):
        response = client.get("/search/", search_params)

        assert response.status_code == 404


@pytest.mark.django_db
def test_search_view_with_filters(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    language = LanguageFactory.create(code="en")
    modality = ModalityFactory.create(code="MR", filterable=True)

    search_params = {
        "query": "test query",
        "language": language.code,
        "modalities": [modality.code],
        "study_date_from": "2023-01-01",
        "study_date_till": "2023-12-31",
        "study_description": "brain scan",
        "patient_sex": "F",
        "age_from": 20,
        "age_till": 80,
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context
        assert "documents" in response.context
        assert len(response.context["documents"]) == 2
        assert response.context["total_count"] == 2


@pytest.mark.django_db
def test_search_view_empty_query(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "",
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200


@pytest.mark.django_db
def test_search_view_query_with_fixes(client: Client):
    """Test search view when query parser applies fixes."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    test_provider = SearchProvider(
        name="Test Provider",
        search=lambda search: SearchResult(total_count=0, total_relation="exact", documents=[]),
        max_results=1000,
    )

    with patch("radis.search.views.search_provider", test_provider):
        with patch("radis.search.views.QueryParser") as mock_parser:
            mock_parser_instance = Mock()
            mock_parser.return_value = mock_parser_instance

            mock_query_node = Mock()
            mock_parser_instance.parse.return_value = (
                mock_query_node,
                ["Fixed typo: 'teh' -> 'the'"],
            )
            mock_parser.unparse.return_value = "the brain"

            search_params = {
                "query": "teh brain",
            }

            response = client.get("/search/", search_params)
            assert response.status_code == 200
            assert "form" in response.context

            assert "fixed_query" in response.context
            assert response.context["fixed_query"] == "the brain"

            # Verify search was executed and returned results
            assert "documents" in response.context
            assert (
                response.context["total_count"] == 0
            )  # This test uses a provider that returns 0 results

            # Verify that QueryParser was called with the original query
            mock_parser_instance.parse.assert_called_once_with("teh brain")

            # Verify that unparse was called to generate the fixed query
            mock_parser.unparse.assert_called_once_with(mock_query_node)


@pytest.mark.django_db
def test_search_view_with_modalities_filter(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    # Create test modalities
    ct_modality = ModalityFactory.create(code="CT", filterable=True)
    mr_modality = ModalityFactory.create(code="MR", filterable=True)

    search_params = {
        "query": "test query",
        "modalities": [ct_modality.code, mr_modality.code],
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context
        assert "documents" in response.context
        assert len(response.context["documents"]) == 2
        assert response.context["total_count"] == 2


@pytest.mark.django_db
def test_search_view_boundary_ages(client: Client):
    """Test search view with boundary age values."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "age_from": 0,  # Minimum age
        "age_till": 120,  # Maximum age
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "documents" in response.context
        assert len(response.context["documents"]) == 2
        assert response.context["total_count"] == 2


@pytest.mark.django_db
def test_unauthenticated_access_redirects_to_login(client: Client):
    """Test that unauthenticated access redirects to login."""
    response = client.get("/search/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_search_view_form_validation_errors(client: Client):
    """Test search view with form validation errors."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "age_from": 80,  # Valid integer but greater than age_till
        "age_till": 30,  # Valid integer but less than age_from
    }

    with patch("radis.search.views.search_provider", create_test_search_provider()):
        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context


@pytest.mark.django_db
def test_the_plain_page_renders_without_the_capability(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=False)):
        response = client.get("/search/", {"query": "pneumothorax"})
    assert response.status_code == 200
    assert "mode" not in response.context["form"].fields


@pytest.mark.django_db
def test_search_view_threads_selected_labels_into_filters(client: Client) -> None:
    """Selected labels from the form are passed through to SearchFilters."""
    from radis.labels.factories import LabelFactory

    LabelFactory.create(name="edema", active=True)
    LabelFactory.create(name="pneumonia", active=True)

    user = create_test_user_with_active_group()
    client.force_login(user)

    captured: dict[str, Search] = {}

    def capturing_search(search: Search) -> SearchResult:
        captured["search"] = search
        return SearchResult(total_count=0, total_relation="exact", documents=[])

    provider = SearchProvider(name="Capturing", search=capturing_search, max_results=1000)

    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"query": "chest", "labels": ["edema", "pneumonia"]})

    assert response.status_code == 200
    assert captured["search"].filters.labels == ["edema", "pneumonia"]


@pytest.mark.django_db
def test_summaries_are_autoescaped_unless_the_provider_marks_them_safe(client: Client):
    from django.utils.safestring import mark_safe

    user = create_test_user_with_active_group()
    client.force_login(user)
    report = ReportFactory.create(document_id="ESC_1", language=LanguageFactory.create(code="en"))

    def document(summary):
        return ReportDocument(
            relevance=0.5,
            document_id=report.document_id,
            pacs_name=report.pacs_name,
            pacs_link=report.pacs_link,
            patient_age=report.patient_age,
            patient_sex=report.patient_sex,
            study_description=report.study_description,
            modalities=["CT"],
            summary=summary,
        )

    def mock_search(request):
        documents = [document("<b>plain</b>"), document(mark_safe("<em>safe</em>"))]
        return SearchResult(total_count=2, total_relation="exact", documents=documents)

    provider = SearchProvider(name="Escaping", search=mock_search, max_results=1000)
    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"query": "chest"})

    content = response.content.decode()
    assert "&lt;b&gt;plain&lt;/b&gt;" in content
    assert "<em>safe</em>" in content


def _document(
    report, *, relevance: float | None = 0.5, similarity=None, score=0.0, summary="Summary"
):
    return ReportDocument(
        relevance=relevance,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=["CT"],
        summary=summary,
    )._replace(similarity=similarity, score=score)


def _text(response) -> str:
    """The rendered page with whitespace collapsed, so phrases split across template
    lines can be asserted as one string."""
    return " ".join(response.content.decode().split())


class _StartTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.append((tag, dict(attrs)))


def _element(response, tag: str, **attrs: str) -> dict[str, str | None]:
    """The attributes of the one ``tag`` element on the page carrying ``attrs``, with
    their values unescaped, so assertions ignore attribute order and line breaks."""
    parser = _StartTags()
    parser.feed(response.content.decode())
    return _one(parser.tags, tag, **attrs)


def _one(tags, tag: str, **attrs: str) -> dict[str, str | None]:
    found = [a for t, a in tags if t == tag and attrs.items() <= a.items()]
    assert len(found) == 1, found
    return found[0]


# Elements without an end tag, which therefore enclose nothing.
_VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}


class _Subtrees(HTMLParser):
    """The start tags nested inside each ``tag`` element carrying ``attrs``."""

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        super().__init__()
        self.tag = tag
        self.attrs = attrs
        self.subtrees: list[list[tuple[str, dict[str, str | None]]]] = []
        self.depth = 0  # elements open inside the current match, the match included

    def handle_starttag(self, tag, attrs) -> None:
        found = dict(attrs)
        if self.depth:
            self.subtrees[-1].append((tag, found))
        elif tag == self.tag and self.attrs.items() <= found.items():
            self.subtrees.append([])
        else:
            return
        if tag not in _VOID_ELEMENTS:
            self.depth += 1

    def handle_endtag(self, tag) -> None:
        if self.depth and tag not in _VOID_ELEMENTS:
            self.depth -= 1


def _inside(response, tag: str, **attrs: str) -> list[tuple[str, dict[str, str | None]]]:
    """The start tags nested in the one ``tag`` element on the page carrying ``attrs``."""
    parser = _Subtrees(tag, attrs)
    parser.feed(response.content.decode())
    assert len(parser.subtrees) == 1, parser.subtrees
    return parser.subtrees[0]


def _provider(result: SearchResult, *, semantic: bool = True, captured: dict | None = None):
    def search(request):
        if captured is not None:
            captured["request"] = request
        return result

    return SearchProvider(name="Stub", search=search, max_results=1000, semantic=semantic)


def _empty(degraded: bool = False) -> SearchResult:
    return SearchResult(total_count=0, total_relation="exact", documents=[], degraded=degraded)


@pytest.fixture
def two_reports(db):
    language = LanguageFactory.create(code="en")
    return [
        ReportFactory.create(document_id=f"MODE_{i}", language=language, pacs_link="http://pacs")
        for i in (1, 2)
    ]


@pytest.mark.django_db
def test_mode_fields_exist_only_with_the_capability(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=True)):
        with_fields = client.get("/search/").context["form"].fields
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=False)):
        without = client.get("/search/").context["form"].fields
    assert (
        "mode" in with_fields and "description" in with_fields and "min_similarity" in with_fields
    )
    assert "mode" not in without and "description" not in without


@pytest.mark.django_db
def test_semantic_mode_builds_a_semantic_search_and_ignores_the_keywords(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get(
            "/search/",
            {
                "mode": "semantic",
                "description": "free  air",
                "query": "ignored",
                "min_similarity": "0.4",
            },
        )
    assert response.status_code == 200
    request = captured["request"]
    assert isinstance(request, SemanticSearch)
    assert request.text == "free air"
    assert request.min_similarity == 0.4
    assert response.context["mode"] == "semantic"


@pytest.mark.django_db
def test_full_text_mode_builds_a_full_text_search_and_ignores_a_description(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        client.get("/search/", {"query": "chest", "description": "free air"})
    request = captured["request"]
    assert isinstance(request, FullTextSearch)
    assert QueryParser.unparse(request.query) == "chest"


@pytest.mark.django_db
def test_semantic_mode_without_a_description_shows_the_notice(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get("/search/", {"mode": "semantic", "query": "chest"})
    assert "request" not in captured
    assert response.context["mode_notice"] == "Semantic mode needs a description."
    assert "documents" not in response.context


@pytest.mark.django_db
def test_semantic_url_without_the_capability_runs_full_text(client: Client):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    provider = _provider(_empty(), semantic=False, captured=captured)
    with patch("radis.search.views.search_provider", provider):
        response = client.get(
            "/search/", {"mode": "semantic", "description": "x", "query": "chest"}
        )
    assert response.status_code == 200
    assert isinstance(captured["request"], FullTextSearch)


@pytest.mark.django_db
def test_page_beyond_a_short_semantic_list_is_clamped(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    result = SearchResult(total_count=2, total_relation="exact", documents=[], degraded=False)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"mode": "semantic", "description": "x", "page": "3"})
    assert response.status_code == 200
    assert response.context["page_obj"].number == 1


@pytest.mark.django_db
def test_full_text_count_line_and_scores(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest"})
    content = _text(response)
    assert "2 reports found" in content
    assert content.count("Full-text rank: 0.500") == 2
    assert "Similarity:" not in content


@pytest.mark.django_db
def test_a_full_text_search_without_hits_says_so(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"query": "chest"})
    content = _text(response)
    assert "0 reports found" in content
    assert "No results found" in content


@pytest.mark.django_db
def test_result_numbers_continue_on_later_pages(client: Client, two_reports):
    client.force_login(create_test_user_with_active_group())
    result = SearchResult(
        total_count=2, total_relation="exact", documents=[_document(two_reports[1])]
    )
    params = {"query": "chest", "page": "2", "per_page": "1"}
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", params)
    assert "Result: #2" in _text(response)


@pytest.mark.django_db
def test_count_line_when_capped(client: Client):
    client.force_login(create_test_user_with_active_group())
    result = SearchResult(total_count=10000, total_relation="at_least", documents=[])
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"query": "chest"})
    assert "More than 10000 reports found" in _text(response)


@pytest.mark.django_db
@pytest.mark.parametrize("cutoff, shown", [(0.3, "0.300"), (0.745, "0.745")])
def test_semantic_count_line_names_the_cutoff(client: Client, two_reports, settings, cutoff, shown):
    """Three decimals, as the similarities show, so a listed hit never looks below it."""
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = cutoff
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r, relevance=None, similarity=0.7, score=0.7) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert (
        f"The 2 reports most similar to your description with similarity at least {shown}"
        in content
    )
    assert "Full-text rank:" not in content
    assert "Similarity:" in content


@pytest.mark.django_db
def test_semantic_zero_hits(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert "No reports similar to your description" in content
    assert "No results found" not in content


@pytest.mark.django_db
def test_degraded_semantic_shows_only_the_error(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(degraded=True))):
        response = client.get("/search/", {"mode": "semantic", "description": "free air"})
    content = _text(response)
    assert "Semantic search is unavailable right now." in content
    assert "most similar to your description" not in content
    assert "No results found" not in content
    # An empty result's count line would read "No reports similar to your description".
    assert "similar to your description" not in content


@pytest.mark.django_db
def test_info_card_explains_the_modes_only_with_the_capability(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=True)):
        with_modes = _text(client.get("/search/"))
    with patch("radis.search.views.search_provider", _provider(_empty(), semantic=False)):
        without = _text(client.get("/search/"))
    assert "Minimum similarity" in with_modes
    assert (
        "A minus sign does not exclude" in with_modes and "A minus sign does not exclude" in without
    )
    # The cutoff label also renders outside the card; this phrase does not.
    assert "Choose a mode in the search bar" in with_modes
    assert "Choose a mode in the search bar" not in without
    not_rule = (
        "NOT in front of a term means that the term or phrase must not be present. "
        "A minus sign does not exclude."
    )
    assert not_rule in with_modes and not_rule in without


@pytest.mark.django_db
@pytest.mark.parametrize("per_page", ["0", "-1", "abc"])
def test_an_unusable_page_size_falls_back_to_the_default(client: Client, per_page):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get("/search/", {"query": "chest", "per_page": per_page})
    assert response.status_code == 200
    assert captured["request"].limit == 25
    assert response.context["paginator"].per_page == 25


@pytest.mark.django_db
def test_page_sizes_are_offered(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"query": "chest"})
    assert response.context["page_sizes"] == [10, 25, 50]


@pytest.mark.django_db
def test_a_field_error_is_shown_with_the_field_name(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"query": "a" * 201})
    assert response.status_code == 200
    content = _text(response)
    assert "Query: Ensure this value has at most 200 characters (it has 201)." in content
    assert 'class="alert alert-danger' in content


@pytest.mark.django_db
def test_a_non_field_error_is_shown(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", {"query": "chest", "age_from": "80", "age_till": "30"})
    assert response.status_code == 200
    assert "Age from must be less than age till" in _text(response)


@pytest.mark.django_db
@pytest.mark.parametrize("default, override", [(None, None), (0.3, "0")])
def test_semantic_count_line_without_a_cutoff(
    client: Client, two_reports, settings, default, override
):
    """No clause when the effective cutoff is 0 or unset; an explicit 0 beats the default."""
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = default
    client.force_login(create_test_user_with_active_group())
    documents = [_document(r, relevance=None, similarity=0.7, score=0.7) for r in two_reports]
    result = SearchResult(total_count=2, total_relation="exact", documents=documents)
    params = {"mode": "semantic", "description": "free air"}
    if override is not None:
        params["min_similarity"] = override
    with patch("radis.search.views.search_provider", _provider(result)):
        response = client.get("/search/", params)
    content = _text(response)
    assert "The 2 reports most similar to your description" in content
    assert "with similarity at least" not in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "params, notice",
    [
        ({"mode": "semantic", "query": "chest"}, "Semantic mode needs a description."),
        # Full-text mode without keywords shows the card alone, whatever else is given.
        ({"description": "free air"}, None),
        ({}, None),
    ],
)
def test_a_notice_renders_above_the_info_card(client: Client, params, notice):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get("/search/", params)
    content = _text(response)
    assert "Search Info" in content
    if notice is None:
        assert "mode_notice" not in response.context
        assert "alert-info" not in content
    else:
        assert content.index(notice) < content.index("Search Info")


def _capability_page(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        return client.get("/search/")


@pytest.mark.django_db
def test_enter_in_the_description_submits_and_shift_enter_does_not(client: Client):
    textarea = _element(_capability_page(client), "textarea", id="id_description")
    assert textarea["@keydown.enter"] == (
        "if (!$event.shiftKey) { $event.preventDefault(); $el.form.requestSubmit() }"
    )
    # Alpine 3.15 has no .exact modifier: it reads it as a second key name, so a
    # handler carrying it never fires.
    assert not any(".exact" in name for name in textarea)


@pytest.mark.django_db
def test_the_description_has_an_accessible_name(client: Client):
    textarea = _element(_capability_page(client), "textarea", id="id_description")
    assert textarea["aria-label"] == "Description of the reports you are looking for"


@pytest.mark.django_db
def test_each_mode_shows_its_own_row_and_disables_the_other(client: Client):
    """Hidden and disabled, the inactive row keeps its value for the switch back but is
    never submitted, so the URL carries only the active mode's field."""
    page = _capability_page(client)

    keyword_row = {"x-show": "mode !== 'semantic'"}
    # The server-rendered default row stays visible without JavaScript.
    assert "x-cloak" not in _element(page, "div", **keyword_row)
    keyword_tags = _inside(page, "div", **keyword_row)
    query = _one(keyword_tags, "input", name="query")
    assert query[":disabled"] == "mode === 'semantic'"
    assert query["aria-label"] == "Search terms"
    clear = _one(keyword_tags, "button", **{"@click": "clearQuery()"})
    assert clear[":disabled"] == "mode === 'semantic'"
    assert clear["aria-label"] == "Clear search terms"
    assert _one(keyword_tags, "button", type="submit")["aria-label"] == "Search"

    description_row = _element(page, "div", id="description_row")
    assert description_row["x-show"] == "mode === 'semantic'"
    assert "x-cloak" in description_row
    description_tags = _inside(page, "div", id="description_row")
    textarea = _one(description_tags, "textarea", name="description")
    assert textarea[":disabled"] == "mode !== 'semantic'"
    assert _one(description_tags, "button", type="submit")["aria-label"] == "Search"

    # The select sits beside the rows, outside both input groups.
    select = _element(page, "select", name="mode")
    assert select["aria-label"] == "Search mode"
    assert select["x-model.fill"] == "mode"
    assert not any(tag == "select" for tag, _ in keyword_tags + description_tags)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "params, request_type",
    [
        ({"mode": "semantic", "description": "free air", "query": "a" * 201}, SemanticSearch),
        ({"query": "chest", "description": "a" * 1001}, FullTextSearch),
        ({"query": "chest", "min_similarity": "5"}, FullTextSearch),
    ],
    ids=["semantic", "full-text", "full-text-cutoff"],
)
def test_an_invalid_inactive_field_does_not_stop_the_search(client: Client, params, request_type):
    client.force_login(create_test_user_with_active_group())
    captured: dict = {}
    with patch("radis.search.views.search_provider", _provider(_empty(), captured=captured)):
        response = client.get("/search/", params)
    assert response.status_code == 200
    assert isinstance(captured["request"], request_type)
    assert "alert-danger" not in _text(response)


@pytest.mark.django_db
def test_a_filter_error_is_shown_once(client: Client):
    client.force_login(create_test_user_with_active_group())
    with patch("radis.search.views.search_provider", _provider(_empty())):
        response = client.get(
            "/search/", {"mode": "semantic", "description": "free air", "min_similarity": "5"}
        )
    assert _text(response).count("Ensure this value is less than or equal to 1") == 1

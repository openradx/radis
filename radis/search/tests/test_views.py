from unittest.mock import Mock, patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.reports.factories import LanguageFactory, ModalityFactory
from radis.search.site import ReportDocument, SearchProvider, SearchResult


def create_test_user_with_active_group():
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()
    return user


def create_test_search_provider():
    def mock_search(search):
        documents = [
            ReportDocument(
                relevance=0.9,
                document_id="TEST_DOC_1",
                pacs_name="Test PACS",
                pacs_link="http://test.pacs.com",
                patient_age=45,
                patient_sex="M",
                study_description="Test Study 1",
                modalities=["CT"],
                summary="Test summary 1",
            ),
            ReportDocument(
                relevance=0.8,
                document_id="TEST_DOC_2",
                pacs_name="Test PACS",
                pacs_link="http://test.pacs.com",
                patient_age=32,
                patient_sex="F",
                study_description="Test Study 2",
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
@patch("radis.search.views.search_provider", new=create_test_search_provider())
def test_search_view_valid_query(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    language = LanguageFactory.create(code="en")
    modality = ModalityFactory.create(code="CT", filterable=True)

    search_params = {
        "query": "test query",
        "language": language.code,
        "modalities": [modality.code],
        "study_description": "test study",
        "patient_sex": "M",
        "age_from": 30,
        "age_till": 60,
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
def test_search_view_pagination(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "page": "1",
        "per_page": "10",
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch(
    "radis.search.views.search_provider",
    new=SearchProvider(
        name="Test Provider",
        search=lambda search: SearchResult(total_count=10, total_relation="exact", documents=[]),
        max_results=100,
    ),
)
def test_search_view_invalid_page(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "page": "1000",  # Very high page number
        "per_page": "25",
    }

    response = client.get("/search/", search_params)

    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
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

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
def test_search_view_empty_query(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "",
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200


@pytest.mark.django_db
@patch(
    "radis.search.views.search_provider",
    new=SearchProvider(
        name="Test Provider",
        search=lambda search: SearchResult(total_count=0, total_relation="exact", documents=[]),
        max_results=1000,
    ),
)
def test_search_view_query_with_fixes(client: Client):
    """Test search view when query parser applies fixes."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    with patch("radis.search.views.QueryParser") as mock_parser:
        mock_parser_instance = Mock()
        mock_parser.return_value = mock_parser_instance

        mock_query_node = Mock()
        mock_parser_instance.parse.return_value = (mock_query_node, ["Fixed typo: 'teh' -> 'the'"])
        mock_parser.unparse.return_value = "the brain"

        search_params = {
            "query": "teh brain",
        }

        response = client.get("/search/", search_params)
        assert response.status_code == 200
        assert "form" in response.context

        assert "fixed_query" in response.context
        assert response.context["fixed_query"] == "the brain"

        # Verify that QueryParser was called with the original query
        mock_parser_instance.parse.assert_called_once_with("teh brain")

        # Verify that unparse was called to generate the fixed query
        mock_parser.unparse.assert_called_once_with(mock_query_node)


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
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

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
def test_search_view_boundary_ages(client: Client):
    """Test search view with boundary age values."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "age_from": 0,  # Minimum age
        "age_till": 120,  # Maximum age
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200


@pytest.mark.django_db
def test_unauthenticated_access_redirects_to_login(client: Client):
    """Test that unauthenticated access redirects to login."""
    response = client.get("/search/")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=None)
def test_search_view_provider_not_found(client: Client):
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context


@pytest.mark.django_db
@patch("radis.search.views.search_provider", new=create_test_search_provider())
def test_search_view_form_validation_errors(client: Client):
    """Test search view with form validation errors."""
    user = create_test_user_with_active_group()
    client.force_login(user)

    search_params = {
        "query": "test query",
        "age_from": 80,  # Valid integer but greater than age_till
        "age_till": 30,  # Valid integer but less than age_from
    }

    response = client.get("/search/", search_params)
    assert response.status_code == 200
    assert "form" in response.context

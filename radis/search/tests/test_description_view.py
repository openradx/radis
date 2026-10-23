from unittest.mock import AsyncMock, patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client
from django.urls import reverse

HX_HEADERS = {"HX-Request": "true"}
URL_NAME = "search_description"


@pytest.fixture
def writer():
    answer = AsyncMock(return_value="Pneumothorax rechts.")
    with patch("radis.search.views.write_description", new=answer):
        yield answer


def _login(client: Client, *, with_group: bool = True):
    user = UserFactory.create(is_active=True)
    if with_group:
        group = GroupFactory.create()
        user.groups.add(group)
        user.active_group = group
        user.save()
    client.force_login(user)
    return user


def _post(client: Client, query: str, description: str = "", htmx: bool = True):
    return client.post(
        reverse(URL_NAME),
        {"query": query, "description": description},
        headers=HX_HEADERS if htmx else {},
    )


@pytest.mark.django_db
def test_anonymous_is_redirected_to_login(client: Client, writer):
    response = _post(client, "pneumothorax")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]
    writer.assert_not_called()


@pytest.mark.django_db
def test_get_is_not_allowed(client: Client, writer):
    _login(client)
    assert client.get(reverse(URL_NAME), headers=HX_HEADERS).status_code == 405


@pytest.mark.django_db
def test_non_htmx_post_is_rejected(client: Client, writer):
    _login(client)
    assert _post(client, "pneumothorax", htmx=False).status_code == 400
    writer.assert_not_called()


@pytest.mark.django_db
def test_a_user_without_an_active_group_gets_403(client: Client, writer):
    _login(client, with_group=False)
    assert _post(client, "pneumothorax").status_code == 403
    writer.assert_not_called()


@pytest.mark.django_db
def test_fills_the_textarea_with_the_positive_terms_description(client: Client, writer):
    _login(client)
    response = _post(client, "pneumothorax NOT rechts", description="old draft")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="id_description"' in content
    assert ">Pneumothorax rechts.</textarea>" in content
    assert 'id="description_notice"' in content and 'hx-swap-oob="true"' in content
    assert "could not write" not in content
    writer.assert_awaited_once_with("pneumothorax")


@pytest.mark.django_db
def test_failure_keeps_the_submitted_text_and_shows_the_notice(client: Client, writer):
    writer.return_value = None
    _login(client)
    response = _post(client, "pneumothorax", description="my draft")

    content = response.content.decode()
    assert ">my draft</textarea>" in content
    assert "The LLM could not write a description right now." in content


# '"" AND ()' keeps a dangling AND once the parser drops the empty group, so it does not parse.
@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "NOT pneumothorax", '"" AND ()'])
def test_unparseable_keywords_get_the_enter_keywords_notice(client: Client, writer, query):
    _login(client)
    response = _post(client, query, description="kept")

    content = response.content.decode()
    assert (
        "Enter search keywords first; the description is written from the positive terms."
        in content
    )
    assert ">kept</textarea>" in content
    writer.assert_not_called()


@pytest.mark.django_db
def test_an_over_long_query_gets_the_failure_notice_without_an_llm_call(client: Client, writer):
    _login(client)
    response = _post(client, "a" * 201)

    assert "The LLM could not write a description right now." in response.content.decode()
    writer.assert_not_called()


@pytest.mark.django_db
def test_the_answer_is_truncated_to_the_field_limit(client: Client, writer):
    writer.return_value = "x" * 1500
    _login(client)
    response = _post(client, "pneumothorax")

    assert (">" + "x" * 1000 + "</textarea>") in response.content.decode()


@pytest.mark.django_db
def test_the_view_touches_no_sync_database_api(client: Client, writer, monkeypatch):
    """The acceptance conftests set DJANGO_ALLOW_ASYNC_UNSAFE for the whole process,
    which would mask a sync ORM call in the async body; without it such a call raises
    SynchronousOnlyOperation under the test client exactly as it would under Daphne."""
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    _login(client)
    assert _post(client, "pneumothorax").status_code == 200

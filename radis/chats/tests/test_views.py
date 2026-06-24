"""Async tests for the chats views (the app previously had zero tests).

These cover the LLM-backed flows -- chat creation (system-prompt assembly,
title generation), multi-turn updates (history assembly via get_role_display) --
plus ownership/permission behaviour.

The async LLM is mocked at the ``openai.AsyncOpenAI`` boundary used by
``AsyncChatClient``. The mock CAPTURES every ``chat.completions.create`` call so
we can assert the assembled message history reaches the model. Each call returns
a fresh awaitable (the view calls ``chat()`` more than once per request).
"""

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from asgiref.sync import sync_to_async
from django.http import HttpResponse
from django.test import AsyncClient
from django.urls import reverse

from radis.chats.models import Chat, ChatMessage, ChatRole
from radis.reports.factories import ReportFactory

HX = {"headers": {"HX-Request": "true"}}


@contextlib.contextmanager
def _stub_render():
    """Neutralise template rendering for the success-path views.

    The chats partials use django-template-partials includes
    ("chats/chat.html#heading") that don't resolve under a plain unit-test
    render. We assert on DB side effects and the captured LLM calls instead of
    on the rendered HTML, so replace views.render with a trivial response. All
    view logic (prompt assembly, LLM calls, persistence) still runs unchanged.
    """
    with patch("radis.chats.views.render", return_value=HttpResponse("ok")):
        yield


@pytest.fixture(autouse=True)
def _disable_debug_toolbar(settings):
    """The dev env (example.env) sets FORCE_DEBUG_TOOLBAR=true, which makes the
    debug toolbar try to render on every HTML response and then fail to reverse
    its 'djdt' URLs (not in the test URLconf). Force it off for these tests --
    this mirrors radis.settings.test."""
    settings.DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}


class _AsyncCapture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def make_capturing_async_openai_mock(*contents: str) -> tuple[MagicMock, _AsyncCapture]:
    """Fake ``openai.AsyncOpenAI``.

    ``contents`` are returned in order across successive ``chat.completions.create``
    calls (the last is reused if more calls happen). Each call yields a fresh
    awaitable and records the request kwargs.
    """
    capture = _AsyncCapture()
    responses = list(contents)

    async def fake_create(**kwargs: Any) -> MagicMock:
        capture.calls.append(kwargs)
        idx = min(len(capture.calls) - 1, len(responses) - 1)
        content = responses[idx]
        return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])

    openai_mock = MagicMock()
    openai_mock.chat.completions.create = fake_create
    return openai_mock, capture


async def _login(user) -> AsyncClient:
    client = AsyncClient()
    await sync_to_async(client.force_login)(user)
    return client


# --------------------------------------------------------------------------- #
# chat_create_view
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_create_chat_general_prompt_persists_messages_and_title():
    user = await sync_to_async(UserFactory.create)(is_active=True)
    client = await _login(user)

    openai_mock, capture = make_capturing_async_openai_mock("LLM answer", "Generated Title")
    with patch("openai.AsyncOpenAI", return_value=openai_mock), _stub_render():
        resp = await client.post(
            reverse("chat_create"),
            data={"prompt": "What is pneumonia?", "report_id": ""},
            **HX,
        )

    assert resp.status_code == 200

    chat = await sync_to_async(Chat.objects.get)()
    assert chat.owner_id == user.pk
    assert chat.report_id is None
    # Title comes from the (stripped, punctuation-trimmed) second LLM response.
    assert chat.title == "Generated Title"

    # Three messages stored in order: SYSTEM, USER, ASSISTANT.
    msgs = await sync_to_async(
        lambda: list(chat.messages.order_by("id").values_list("role", "content"))
    )()
    assert [m[0] for m in msgs] == [ChatRole.SYSTEM, ChatRole.USER, ChatRole.ASSISTANT]
    assert msgs[1][1] == "What is pneumonia?"
    assert msgs[2][1] == "LLM answer"

    # Two LLM calls: one for the answer, one for the title.
    assert len(capture.calls) == 2
    # The answer call sends the general system prompt + the user prompt.
    from django.conf import settings

    answer_messages = capture.calls[0]["messages"]
    assert answer_messages[0]["content"] == settings.CHAT_GENERAL_SYSTEM_PROMPT
    assert answer_messages[1]["content"] == "What is pneumonia?"
    # The title call passes max_completion_tokens (the answer call does not).
    assert "max_completion_tokens" not in capture.calls[0]
    assert capture.calls[1]["max_completion_tokens"] == 20


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_create_chat_with_report_embeds_report_body_in_system_prompt():
    user = await sync_to_async(UserFactory.create)(is_active=True)
    report = await sync_to_async(ReportFactory.create)(body="UNIQUE-REPORT-FINDINGS-XYZ")
    client = await _login(user)

    openai_mock, capture = make_capturing_async_openai_mock("answer", "title")
    with patch("openai.AsyncOpenAI", return_value=openai_mock), _stub_render():
        resp = await client.post(
            reverse("chat_create"),
            data={"prompt": "Summarize", "report_id": str(report.pk)},
            **HX,
        )

    assert resp.status_code == 200

    chat = await sync_to_async(Chat.objects.get)()
    assert chat.report_id == report.pk

    # The report body must be substituted into the system prompt that reaches the LLM.
    system_prompt = capture.calls[0]["messages"][0]["content"]
    assert "UNIQUE-REPORT-FINDINGS-XYZ" in system_prompt

    # And it must be persisted as the stored SYSTEM message.
    system_msg = await sync_to_async(
        lambda: chat.messages.get(role=ChatRole.SYSTEM).content
    )()
    assert "UNIQUE-REPORT-FINDINGS-XYZ" in system_msg


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_create_chat_post_without_htmx_is_rejected():
    user = await sync_to_async(UserFactory.create)(is_active=True)
    client = await _login(user)

    openai_mock, _ = make_capturing_async_openai_mock("a", "t")
    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        # No HX header -> SuspiciousOperation -> 400.
        resp = await client.post(
            reverse("chat_create"), data={"prompt": "hi", "report_id": ""}
        )

    assert resp.status_code == 400
    assert await sync_to_async(Chat.objects.count)() == 0


# --------------------------------------------------------------------------- #
# chat_update_view (multi-turn history)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_update_chat_sends_full_history_and_appends_turn():
    user = await sync_to_async(UserFactory.create)(is_active=True)
    client = await _login(user)

    # Seed a chat with an existing system/user/assistant turn.
    chat = await sync_to_async(Chat.objects.create)(owner=user, title="t")
    await sync_to_async(ChatMessage.objects.create)(
        chat=chat, role=ChatRole.SYSTEM, content="SYS-PROMPT"
    )
    await sync_to_async(ChatMessage.objects.create)(
        chat=chat, role=ChatRole.USER, content="first question"
    )
    await sync_to_async(ChatMessage.objects.create)(
        chat=chat, role=ChatRole.ASSISTANT, content="first answer"
    )

    openai_mock, capture = make_capturing_async_openai_mock("second answer")
    with patch("openai.AsyncOpenAI", return_value=openai_mock), _stub_render():
        resp = await client.post(
            reverse("chat_update", args=[chat.pk]),
            data={"prompt": "second question"},
            **HX,
        )

    assert resp.status_code == 200
    assert len(capture.calls) == 1

    # History is rebuilt from stored messages (roles lowercased via
    # get_role_display) + the new user prompt -- SYSTEM is NOT excluded here.
    sent = capture.calls[0]["messages"]
    assert sent == [
        {"role": "system", "content": "SYS-PROMPT"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]

    # The new user + assistant messages were persisted.
    roles = await sync_to_async(
        lambda: list(chat.messages.order_by("id").values_list("role", "content"))
    )()
    assert roles[-2] == (ChatRole.USER, "second question")
    assert roles[-1] == (ChatRole.ASSISTANT, "second answer")


# --------------------------------------------------------------------------- #
# Ownership / permissions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_update_chat_owned_by_other_user_is_blocked():
    """Ownership is enforced on update: a non-owner cannot mutate the chat and
    the LLM is never consulted.

    NOTE (inconsistency, not asserted as a bug here): chat_update_view uses
    ``Chat.objects...aget(pk=pk, owner=request.user)`` which raises
    Chat.DoesNotExist (HTTP 500) for a non-owner, whereas chat_detail_view /
    chat_delete_view use get_object_or_404 (HTTP 404). Consider aget_object_or_404
    in chat_update_view for consistent 404 behaviour.
    """
    owner = await sync_to_async(UserFactory.create)(is_active=True)
    other = await sync_to_async(UserFactory.create)(is_active=True)
    chat = await sync_to_async(Chat.objects.create)(owner=owner, title="t")
    await sync_to_async(ChatMessage.objects.create)(
        chat=chat, role=ChatRole.SYSTEM, content="s"
    )

    client = await _login(other)
    openai_mock, capture = make_capturing_async_openai_mock("nope")
    with patch("openai.AsyncOpenAI", return_value=openai_mock):
        with pytest.raises(Chat.DoesNotExist):
            await client.post(
                reverse("chat_update", args=[chat.pk]),
                data={"prompt": "hello"},
                **HX,
            )

    # The LLM must never be consulted for a chat the user does not own.
    assert len(capture.calls) == 0
    # No new messages were appended to the victim's chat.
    assert await sync_to_async(chat.messages.count)() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_detail_view_only_returns_own_chat():
    owner = await sync_to_async(UserFactory.create)(is_active=True)
    other = await sync_to_async(UserFactory.create)(is_active=True)
    chat = await sync_to_async(Chat.objects.create)(owner=owner, title="t")

    # Owner can read it.
    owner_client = await _login(owner)
    resp_owner = await owner_client.get(reverse("chat_detail", args=[chat.pk]))
    assert resp_owner.status_code == 200

    # A different user cannot.
    other_client = await _login(other)
    resp_other = await other_client.get(reverse("chat_detail", args=[chat.pk]))
    assert resp_other.status_code == 404


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_list_requires_login():
    client = AsyncClient()
    resp = await client.get(reverse("chat_list"))
    # LoginRequired -> redirect to login.
    assert resp.status_code == 302
    assert "/login" in resp.url or "next=" in resp.url

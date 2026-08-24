import logging
import string
from string import Template

import openai
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404, get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import push_url
from django_tables2 import RequestConfig
from openai.types.chat import ChatCompletionMessageParam

from radis.chats.forms import CreateChatForm, PromptForm
from radis.chats.tables import ChatTable
from radis.core.utils.llm_client import AsyncChatClient, LLMResponseError
from radis.core.utils.rate_limit import RateLimited
from radis.reports.models import Report

from .models import Chat, ChatMessage, ChatRole

logger = logging.getLogger(__name__)


def _chat_error_message(err: Exception) -> str:
    """User-facing text for a failed chat call.

    A rate limit clears by itself, so it is worth retrying. Anything else — a provider
    rejecting a configured request parameter, a refusal, an outage — will not, and
    telling the user to try again just sends them round the same loop.
    """
    if isinstance(err, RateLimited):
        return "The LLM service is busy. Please try again in a moment."
    logger.exception("Chat request failed", exc_info=err)
    return "The LLM service could not answer this request. Please contact your administrator."


@require_GET
@login_required
def chat_list_view(request: AuthenticatedHttpRequest) -> HttpResponse:
    chats = Chat.objects.filter(owner=request.user)
    table = ChatTable(chats)
    RequestConfig(request).configure(table)

    return render(request, "chats/chat_list.html", {"table": table})


@require_POST
@login_required
def chat_clear_all(request: AuthenticatedHttpRequest) -> HttpResponse:
    Chat.objects.filter(owner=request.user).delete()
    messages.add_message(request, messages.SUCCESS, "All chats deleted successfully!")
    return redirect("chat_list")


@login_required
async def chat_create_view(request: AuthenticatedHttpRequest) -> HttpResponse:
    if request.method == "POST":
        if not request.htmx:
            raise SuspiciousOperation

        form = CreateChatForm(request.POST)
        report_id: str | None = None
        report: Report | None = None
        if form.is_valid():
            report_id = form.cleaned_data["report_id"]
            user_prompt: str = form.cleaned_data["prompt"]

            if report_id:
                report = await aget_object_or_404(Report, pk=report_id)
                instructions_system_prompt = Template(
                    settings.CHAT_REPORT_SYSTEM_PROMPT
                ).substitute({"report": report.body})
            else:
                instructions_system_prompt: str = settings.CHAT_GENERAL_SYSTEM_PROMPT

            client = AsyncChatClient("chats")

            try:
                # Generate an answer for the user prompt
                answer = await client.chat(
                    [
                        {"role": "system", "content": instructions_system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except (RateLimited, LLMResponseError, openai.APIError) as err:
                return render(
                    request,
                    "chats/_chat.html",
                    {
                        "chat": None,
                        "report": report,
                        "chat_messages": [],
                        "form": form,
                        "error": _chat_error_message(err),
                    },
                )

            # Generate a title for the chat. The title is secondary, so if it is rate-limited
            # we keep the answer and fall back to the user prompt instead of failing the request.
            title_system_prompt = Template(settings.CHAT_GENERATE_TITLE_SYSTEM_PROMPT).substitute(
                {"num_words": 6, "user_prompt": user_prompt, "assistant_response": answer}
            )
            try:
                title = await client.chat(
                    [
                        {"role": "system", "content": title_system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_completion_tokens=20,
                )
            except (RateLimited, LLMResponseError, openai.APIError):
                title = user_prompt
            title = title.strip().rstrip(string.punctuation)[:100]

            chat = await Chat.objects.acreate(owner=request.user, title=title, report=report)

            await ChatMessage.objects.acreate(
                chat=chat, role=ChatRole.SYSTEM, content=instructions_system_prompt
            )
            await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=user_prompt)
            await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=answer)

            form = PromptForm()

            response = render(
                request,
                "chats/_chat.html",
                {
                    "chat": chat,
                    "report": report,
                    "chat_messages": [
                        message
                        async for message in chat.messages.exclude(role=ChatRole.SYSTEM).order_by(
                            "id"
                        )
                    ],
                    "form": form,
                },
            )
            return push_url(response, url=reverse("chat_detail", args=[chat.pk]))

    else:
        report_id = request.GET.get("report_id", None)
        report: Report | None = None
        if report_id is None:
            form = CreateChatForm()
        else:
            active_group = request.user.active_group
            assert active_group
            report = await aget_object_or_404(Report, id=report_id, groups=active_group)
            form = CreateChatForm(initial={"report_id": report.pk})

    return render(
        request,
        "chats/chat.html",
        {"chat": None, "report": report, "chat_messages": [], "form": form},
    )


@require_GET
@login_required
def chat_detail_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    chat = get_object_or_404(Chat, pk=pk, owner=request.user)
    form = PromptForm()

    return render(
        request,
        "chats/chat.html",
        {
            "chat": chat,
            "report": chat.report,
            "chat_messages": [
                message for message in chat.messages.all() if message.role != ChatRole.SYSTEM
            ],
            "form": form,
        },
    )


@require_POST
@login_required
async def chat_update_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    if not request.htmx:
        raise SuspiciousOperation

    chat = await aget_object_or_404(
        Chat.objects.prefetch_related("report"), pk=pk, owner=request.user
    )

    form = PromptForm(request.POST)
    if form.is_valid():
        messages: list[ChatCompletionMessageParam] = []
        async for content in chat.messages.order_by("id").all():
            role = content.get_role_display().lower()
            content = content.content
            messages.append({"role": role, "content": content})  # type: ignore

        prompt = form.cleaned_data["prompt"]
        messages.append({"role": "user", "content": prompt})

        client = AsyncChatClient("chats")
        try:
            response = await client.chat(messages)
        except (RateLimited, LLMResponseError, openai.APIError) as err:
            return render(
                request,
                "chats/_chat.html",
                {
                    "chat": chat,
                    "report": chat.report,
                    "chat_messages": [
                        message
                        async for message in chat.messages.exclude(role=ChatRole.SYSTEM).order_by(
                            "id"
                        )
                    ],
                    "form": form,
                    "error": _chat_error_message(err),
                },
            )

        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=prompt)
        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=response)

        form = PromptForm()

    return render(
        request,
        "chats/_chat.html",
        {
            "chat": chat,
            "report": chat.report,
            "chat_messages": [
                message
                async for message in chat.messages.exclude(role=ChatRole.SYSTEM).order_by("id")
            ],
            "form": form,
        },
    )


@require_POST
@login_required
def chat_delete_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    chat = get_object_or_404(Chat, pk=pk, owner=request.user)
    chat.delete()

    messages.add_message(request, messages.SUCCESS, "Chat deleted successfully!")
    return redirect("chat_list")

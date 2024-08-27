import string
from string import Template

from adit_radis_shared.common.decorators import login_required_async
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404, get_object_or_404, redirect, render  # type: ignore
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import push_url
from django_tables2 import RequestConfig
from openai.types.chat import ChatCompletionMessageParam

from radis.chats.forms import CreateChatForm, PromptForm
from radis.chats.tables import ChatTable
from radis.reports.models import Report

from .models import Chat, ChatMessage, ChatRole
from .utils.chat_client import AsyncChatClient


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


@login_required_async
async def chat_create_view(request: AuthenticatedHttpRequest) -> HttpResponse:
    if request.method == "POST":
        if not request.htmx:
            raise SuspiciousOperation

        form = CreateChatForm(request.POST)
        if form.is_valid():
            report_text: str = form.cleaned_data["report"]
            user_prompt: str = form.cleaned_data["prompt"]

            if report_text:
                instructions_system_prompt = Template(
                    settings.CHAT_REPORT_QUESTION_SYSTEM_PROMPT
                ).substitute({"report": report_text})
            else:
                instructions_system_prompt: str = settings.CHAT_GENERAL_SYSTEM_PROMPT

            client = AsyncChatClient()
            answer = await client.send_messages(
                [
                    {"role": "system", "content": instructions_system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )

            title_system_prompt = Template(settings.CHAT_GENERATE_TITLE_SYSTEM_PROMPT).substitute(
                {"num_words": 6}
            )

            title = await client.send_messages(
                [
                    {"role": "system", "content": title_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=20,
            )
            title = title.strip().rstrip(string.punctuation)[:100]

            chat = await Chat.objects.acreate(owner=request.user, title=title)

            await ChatMessage.objects.acreate(
                chat=chat, role=ChatRole.SYSTEM, content=instructions_system_prompt
            )
            await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=user_prompt)
            await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=answer)

            form = PromptForm()

            response = render(
                request,
                "chats/_chat_created.html",
                {
                    "chat": chat,
                    "chat_messages": [
                        message
                        async for message in chat.messages.all()
                        if message.role != ChatRole.SYSTEM
                    ],
                    "form": form,
                },
            )
            return push_url(response, url=reverse("chat_detail", args=[chat.pk]))

    else:
        report_id = request.GET.get("report_id", None)
        if report_id is None:
            form = CreateChatForm()
        else:
            active_group = request.user.active_group
            assert active_group
            report: Report = await aget_object_or_404(Report, id=report_id, groups=active_group)
            form = CreateChatForm(initial={"report": report.body})

    return render(
        request,
        "chats/chat_create.html",
        {"chat": None, "chat_messages": [], "form": form},
    )


@require_GET
@login_required
def chat_detail_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    chat = get_object_or_404(Chat, pk=pk, owner=request.user)
    form = PromptForm()
    return render(
        request,
        "chats/chat_detail.html",
        {
            "chat": chat,
            "chat_messages": [
                message for message in chat.messages.all() if message.role != ChatRole.SYSTEM
            ],
            "form": form,
        },
    )


@require_POST
@login_required_async
async def chat_update_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    if not request.htmx:
        raise SuspiciousOperation

    chat = await Chat.objects.aget(pk=pk, owner=request.user)

    form = PromptForm(request.POST)
    if form.is_valid():
        messages: list[ChatCompletionMessageParam] = []
        async for content in chat.messages.all():
            role = content.get_role_display().lower()
            content = content.content
            messages.append({"role": role, "content": content})  # type: ignore

        prompt = form.cleaned_data["prompt"]
        messages.append({"role": "user", "content": prompt})

        client = AsyncChatClient()
        response = await client.send_messages(messages)

        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=prompt)
        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=response)

        form = PromptForm()

    return render(
        request,
        "chats/_chat.html",
        {
            "chat": chat,
            "chat_messages": [message async for message in chat.messages.all()],
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

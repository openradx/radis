import string
from string import Template

from adit_radis_shared.common.decorators import login_required_async
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
from radis.reports.models import Report

from .models import Chat, ChatMessage, ChatRole, Grammar
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
        report_id: str | None = None
        report: Report | None = None
        if form.is_valid():
            report_id = form.cleaned_data["report_id"]
            user_prompt: str = form.cleaned_data["prompt"]

            if report_id:
                report = await aget_object_or_404(Report, pk=report_id)
                instructions_system_prompt = Template(
                    settings.CHAT_REPORT_QUESTION_SYSTEM_PROMPT
                ).substitute({"report": report.body})
            else:
                instructions_system_prompt: str = settings.CHAT_GENERAL_SYSTEM_PROMPT

            client = AsyncChatClient()

            if request.POST.get("yes_no_answer"):
                grammar = await Grammar.objects.aget(name="YES_NO")
            else:
                grammar = await Grammar.objects.aget(name="FREE_TEXT")

            # Generate an answer for the user prompt
            answer = await client.send_messages(
                [
                    {"role": "system", "content": instructions_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                grammar=grammar,
            )

            # Generate a title for the chat
            title_system_prompt = Template(settings.CHAT_GENERATE_TITLE_SYSTEM_PROMPT).substitute(
                {"num_words": 6}
            )

            free_text_grammar = await Grammar.objects.aget(name="FREE_TEXT")
            title = await client.send_messages(
                [
                    {"role": "system", "content": title_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=20,
                grammar=free_text_grammar,
            )
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
                "chats/_chat_created.html",
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
        "chats/chat_create.html",
        {"chat": None, "report": report, "chat_messages": [], "form": form},
    )


@require_GET
@login_required
def chat_detail_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    chat = get_object_or_404(Chat, pk=pk, owner=request.user)
    form = PromptForm()
    print("report", chat.report)
    return render(
        request,
        "chats/chat_detail.html",
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
@login_required_async
async def chat_update_view(request: AuthenticatedHttpRequest, pk: int) -> HttpResponse:
    if not request.htmx:
        raise SuspiciousOperation

    chat = await Chat.objects.prefetch_related("report").aget(pk=pk, owner=request.user)

    form = PromptForm(request.POST)
    if form.is_valid():
        messages: list[ChatCompletionMessageParam] = []
        async for content in chat.messages.order_by("id").all():
            role = content.get_role_display().lower()
            content = content.content
            messages.append({"role": role, "content": content})  # type: ignore

        prompt = form.cleaned_data["prompt"]
        messages.append({"role": "user", "content": prompt})

        client = AsyncChatClient()
        if request.POST.get("yes_no_answer"):
            grammar = await Grammar.objects.aget(name="YES_NO")
        else:
            grammar = await Grammar.objects.aget(name="FREE_TEXT")
        response = await client.send_messages(messages, grammar=grammar)

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
